"""
Offline tests for contracts/GigEscrow.py and contracts/TalentGate.py.

No chain, no network, no `genlayer` package required -- these import the
actual contract files through genlayer_stub.py, a minimal stand-in for the
GenVM SDK, and drive them directly as plain Python objects. Every test below
exercises the real shipped logic, not a reimplementation of it.

Run with:  python3 -m unittest discover -s test -v
"""
import json
import unittest
from pathlib import Path

import genlayer_stub as stub

CONTRACTS_DIR = Path(__file__).resolve().parent.parent / "contracts"


def load_gigescrow():
	return stub.load_contract(CONTRACTS_DIR / "GigEscrow.py", "gigescrow_under_test")


def load_talentgate():
	return stub.load_contract(CONTRACTS_DIR / "TalentGate.py", "talentgate_under_test")


OWNER = stub.Address("0x1111111111111111111111111111111111111111")
CLIENT = stub.Address("0x2222222222222222222222222222222222222222")
FREELANCER = stub.Address("0x3333333333333333333333333333333333333333")
OTHER = stub.Address("0x4444444444444444444444444444444444444444")

ONE_GEN = 10**18


def send_as(sender, value=0):
	stub.message.sender_address = sender
	stub.message.value = value


def approving_verdict(verdict, score=90, reasoning="looks complete"):
	payload = json.dumps({"verdict": verdict, "score": score, "reasoning": reasoning})
	stub.NONDET_HOOKS["web_render"] = lambda url, mode: "some deliverable content"
	stub.NONDET_HOOKS["exec_prompt"] = lambda prompt, response_format: payload


class GigEscrowTestCase(unittest.TestCase):
	def setUp(self):
		stub.reset()
		self.mod = load_gigescrow()
		send_as(OWNER)
		self.gig = self.mod.GigEscrow(250)  # 2.5% platform fee

	# -- construction & config -------------------------------------------

	def test_constructor_clamps_fee_and_sets_owner(self):
		send_as(OWNER)
		g = self.mod.GigEscrow(999999)
		cfg = json.loads(g.get_config())
		self.assertEqual(cfg["owner"], str(OWNER))
		self.assertEqual(cfg["platform_fee_bps"], self.mod.MAX_PLATFORM_FEE_BPS)

	# -- fee-splitting arithmetic -----------------------------------------

	def test_fund_milestone_splits_fee_and_escrow_correctly(self):
		send_as(CLIENT)
		job = json.loads(self.gig.create_job(str(FREELANCER), "Landing page"))
		jid = job["job_id"]

		amount = 4 * ONE_GEN
		send_as(CLIENT, amount)
		res = json.loads(self.gig.fund_milestone(jid, "Ship a responsive landing page"))
		self.assertTrue(res["ok"])
		expected_fee = (amount // self.mod.BPS_DENOM) * 250
		expected_escrow = amount - expected_fee
		self.assertEqual(int(res["fee"]), expected_fee)
		self.assertEqual(int(res["escrowed"]), expected_escrow)

		stats = json.loads(self.gig.get_platform_stats())
		self.assertEqual(int(stats["escrow_locked"]), expected_escrow)
		self.assertEqual(int(stats["platform_fees_accrued"]), expected_fee)
		self.assertEqual(stub.PAYMENTS, [])  # nothing paid out yet, money just escrowed

	# -- payable-never-raises, end to end ---------------------------------

	def test_fund_milestone_below_minimum_refunds_instead_of_raising(self):
		send_as(CLIENT)
		job = json.loads(self.gig.create_job(str(FREELANCER), "Logo design"))
		jid = job["job_id"]

		too_small = self.mod.MIN_MILESTONE_AMOUNT - 1
		send_as(CLIENT, too_small)
		res = json.loads(self.gig.fund_milestone(jid, "Deliver a logo"))
		self.assertFalse(res["ok"])
		self.assertEqual(int(res["refunded"]), too_small)
		self.assertIn((str(CLIENT), too_small), stub.PAYMENTS)

		stats = json.loads(self.gig.get_platform_stats())
		self.assertEqual(int(stats["escrow_locked"]), 0)

	def test_fund_milestone_by_non_client_refunds_instead_of_raising(self):
		send_as(CLIENT)
		job = json.loads(self.gig.create_job(str(FREELANCER), "API integration"))
		jid = job["job_id"]

		send_as(OTHER, ONE_GEN)
		res = json.loads(self.gig.fund_milestone(jid, "Wire up the API"))
		self.assertFalse(res["ok"])
		self.assertIn((str(OTHER), ONE_GEN), stub.PAYMENTS)

	def test_dispute_milestone_below_bond_refunds_instead_of_raising(self):
		jid = self._fund_and_submit()
		send_as(CLIENT)
		self.gig.approve_milestone(jid, 0)  # not disputable once approved
		send_as(FREELANCER, self.mod.DISPUTE_BOND - 1)
		res = json.loads(self.gig.dispute_milestone(jid, 0))
		self.assertFalse(res["ok"])
		self.assertIn((str(FREELANCER), self.mod.DISPUTE_BOND - 1), stub.PAYMENTS)

	# -- helpers ------------------------------------------------------------

	def _create_job(self):
		send_as(CLIENT)
		job = json.loads(self.gig.create_job(str(FREELANCER), "Website redesign"))
		return job["job_id"]

	def _fund(self, jid, amount=2 * ONE_GEN, description="Deliver the homepage mockup"):
		send_as(CLIENT, amount)
		res = json.loads(self.gig.fund_milestone(jid, description))
		self.assertTrue(res["ok"], res)
		return res

	def _fund_and_submit(self, url="https://example.com/deliverable", note="done"):
		jid = self._create_job()
		self._fund(jid)
		send_as(FREELANCER)
		self.gig.accept_job(jid)
		res = json.loads(self.gig.submit_milestone(jid, 0, url, note))
		self.assertTrue(res["ok"], res)
		return jid

	# -- happy path: direct approval, no AI --------------------------------

	def test_direct_approval_pays_freelancer_and_completes_job(self):
		jid = self._fund_and_submit()
		ms_before = json.loads(self.gig.get_milestone(jid, 0))

		send_as(CLIENT)
		res = json.loads(self.gig.approve_milestone(jid, 0))
		self.assertTrue(res["ok"])
		self.assertEqual(res["status"], self.mod.MS_APPROVED)
		self.assertEqual(int(res["paid"]), int(ms_before["escrowed_amount"]))
		self.assertIn((str(FREELANCER), int(ms_before["escrowed_amount"])), stub.PAYMENTS)

		job = json.loads(self.gig.get_job(jid))
		self.assertEqual(job["status"], self.mod.STATUS_COMPLETED)

		rep = json.loads(self.gig.get_reputation(str(FREELANCER)))
		self.assertEqual(rep["completed_count"], 1)
		self.assertEqual(int(rep["total_earned"]), int(ms_before["escrowed_amount"]))

	def test_cancel_milestone_only_while_pending(self):
		jid = self._create_job()
		self._fund(jid, amount=1 * ONE_GEN)   # milestone 0
		self._fund(jid, amount=1 * ONE_GEN)   # milestone 1 -- keeps the job open

		send_as(CLIENT)
		res = json.loads(self.gig.cancel_milestone(jid, 0))
		self.assertTrue(res["ok"])
		self.assertEqual(res["status"], self.mod.MS_CANCELED)
		self.assertIn((str(CLIENT), int(res["refunded"])), stub.PAYMENTS)

		job = json.loads(self.gig.get_job(jid))
		self.assertEqual(job["status"], self.mod.STATUS_OPEN)  # milestone 1 still pending

		# milestone 1, once submitted, must refuse to cancel
		send_as(FREELANCER)
		self.gig.accept_job(jid)
		self.gig.submit_milestone(jid, 1, "https://example.com/x", "note")
		send_as(CLIENT)
		with self.assertRaises(stub.UserError):
			self.gig.cancel_milestone(jid, 1)

	def test_reclaim_requires_timeout_elapsed(self):
		jid = self._create_job()
		self._fund(jid, amount=1 * ONE_GEN)

		with self.assertRaises(stub.UserError):
			self.gig.reclaim_milestone(jid, 0)  # far too early

		ms_key = self.gig._mkey(jid, 0)
		self.gig.milestones[ms_key].funded_epoch = 0  # simulate the timeout elapsing
		res = json.loads(self.gig.reclaim_milestone(jid, 0))
		self.assertTrue(res["ok"])
		self.assertEqual(res["status"], self.mod.MS_RECLAIMED)
		self.assertIn((str(CLIENT), int(res["refunded"])), stub.PAYMENTS)

	# -- arbitration: APPROVED ---------------------------------------------

	def test_dispute_approved_pays_freelancer_and_returns_bond(self):
		jid = self._fund_and_submit()
		send_as(CLIENT)
		self.gig.reject_milestone(jid, 0, "not what I asked for")

		approving_verdict(self.mod.VERDICT_APPROVED)
		ms_before = json.loads(self.gig.get_milestone(jid, 0))
		send_as(FREELANCER, self.mod.DISPUTE_BOND)
		res = json.loads(self.gig.dispute_milestone(jid, 0))

		self.assertTrue(res["ok"])
		self.assertEqual(res["verdict"], self.mod.VERDICT_APPROVED)
		self.assertEqual(res["status"], self.mod.MS_RESOLVED_APPROVED)
		self.assertFalse(res["bond_forfeited"])
		self.assertIn((str(FREELANCER), int(ms_before["escrowed_amount"])), stub.PAYMENTS)
		self.assertIn((str(FREELANCER), self.mod.DISPUTE_BOND), stub.PAYMENTS)

		rep = json.loads(self.gig.get_reputation(str(FREELANCER)))
		self.assertEqual(rep["ai_win_count"], 1)

	# -- arbitration: REJECTED, challenging a rejection -> bond forfeited --

	def test_dispute_rejected_challenging_rejection_forfeits_bond(self):
		jid = self._fund_and_submit()
		send_as(CLIENT)
		self.gig.reject_milestone(jid, 0, "incomplete")

		approving_verdict(self.mod.VERDICT_REJECTED)
		ms_before = json.loads(self.gig.get_milestone(jid, 0))
		send_as(FREELANCER, self.mod.DISPUTE_BOND)
		res = json.loads(self.gig.dispute_milestone(jid, 0))

		self.assertTrue(res["ok"])
		self.assertEqual(res["verdict"], self.mod.VERDICT_REJECTED)
		self.assertEqual(res["status"], self.mod.MS_RESOLVED_REJECTED)
		self.assertTrue(res["bond_forfeited"])
		# client gets both the refund AND the forfeited bond
		self.assertIn((str(CLIENT), int(ms_before["escrowed_amount"])), stub.PAYMENTS)
		self.assertIn((str(CLIENT), self.mod.DISPUTE_BOND), stub.PAYMENTS)
		self.assertNotIn((str(FREELANCER), self.mod.DISPUTE_BOND), stub.PAYMENTS)

		stats = json.loads(self.gig.get_platform_stats())
		self.assertEqual(int(stats["total_bonds_forfeited"]), self.mod.DISPUTE_BOND)

		rep = json.loads(self.gig.get_reputation(str(FREELANCER)))
		self.assertEqual(rep["ai_loss_count"], 1)

	# -- arbitration: REJECTED on a fresh SUBMITTED (neutral request) ------
	# -- bond must be returned, never forfeited, since nobody had yet made
	# -- a call for this to "challenge".

	def test_dispute_rejected_on_neutral_submitted_request_returns_bond(self):
		jid = self._fund_and_submit()
		approving_verdict(self.mod.VERDICT_REJECTED)

		send_as(CLIENT, self.mod.DISPUTE_BOND)
		res = json.loads(self.gig.dispute_milestone(jid, 0))

		self.assertTrue(res["ok"])
		self.assertFalse(res["bond_forfeited"])
		self.assertIn((str(CLIENT), self.mod.DISPUTE_BOND), stub.PAYMENTS)
		stats = json.loads(self.gig.get_platform_stats())
		self.assertEqual(int(stats["total_bonds_forfeited"]), 0)

	# -- arbitration: INCONCLUSIVE -------------------------------------------

	def test_dispute_inconclusive_leaves_status_unchanged_and_refunds_bond(self):
		jid = self._fund_and_submit()
		send_as(CLIENT)
		self.gig.reject_milestone(jid, 0, "unclear")

		stub.NONDET_HOOKS["web_render"] = lambda url, mode: ""  # empty fetch -> inconclusive
		stub.NONDET_HOOKS["exec_prompt"] = lambda prompt, response_format: "{}"

		send_as(FREELANCER, self.mod.DISPUTE_BOND)
		res = json.loads(self.gig.dispute_milestone(jid, 0))

		self.assertTrue(res["ok"])
		self.assertEqual(res["verdict"], self.mod.VERDICT_INCONCLUSIVE)
		self.assertEqual(res["status"], self.mod.MS_REJECTED)  # unchanged, retryable
		self.assertIn((str(FREELANCER), self.mod.DISPUTE_BOND), stub.PAYMENTS)

		stats = json.loads(self.gig.get_platform_stats())
		self.assertEqual(stats["ai_inconclusive"], 1)

	# -- timing gates ---------------------------------------------------------

	def test_freelancer_cannot_dispute_submitted_before_client_silence_timeout(self):
		jid = self._fund_and_submit()
		send_as(FREELANCER, self.mod.DISPUTE_BOND)
		res = json.loads(self.gig.dispute_milestone(jid, 0))
		self.assertFalse(res["ok"])
		self.assertIn((str(FREELANCER), self.mod.DISPUTE_BOND), stub.PAYMENTS)

	def test_freelancer_can_escalate_after_client_silence_timeout(self):
		jid = self._fund_and_submit()
		ms_key = self.gig._mkey(jid, 0)
		self.gig.milestones[ms_key].submitted_epoch = 0  # simulate the timeout elapsing
		approving_verdict(self.mod.VERDICT_APPROVED)
		send_as(FREELANCER, self.mod.DISPUTE_BOND)
		res = json.loads(self.gig.dispute_milestone(jid, 0))
		self.assertTrue(res["ok"])

	def test_client_can_dispute_submitted_immediately(self):
		jid = self._fund_and_submit()
		approving_verdict(self.mod.VERDICT_APPROVED)
		send_as(CLIENT, self.mod.DISPUTE_BOND)
		res = json.loads(self.gig.dispute_milestone(jid, 0))
		self.assertTrue(res["ok"])

	# -- transient infrastructure failures never raise from a payable path --

	def test_dispute_web_render_failure_returns_retry_later_and_refunds_bond(self):
		jid = self._fund_and_submit()
		send_as(CLIENT)
		self.gig.reject_milestone(jid, 0, "not sure yet")

		def _boom(url, mode):
			raise TimeoutError("upstream timed out")
		stub.NONDET_HOOKS["web_render"] = _boom

		ms_before = json.loads(self.gig.get_milestone(jid, 0))
		send_as(FREELANCER, self.mod.DISPUTE_BOND)
		res = json.loads(self.gig.dispute_milestone(jid, 0))

		self.assertTrue(res["ok"])
		self.assertEqual(res["verdict"], "RETRY_LATER")
		self.assertEqual(res["status"], ms_before["status"])  # left exactly as it was
		self.assertIn((str(FREELANCER), self.mod.DISPUTE_BOND), stub.PAYMENTS)
		self.assertIn(self.mod.ERR_TRANSIENT_FETCH, res["reason"])

		stats = json.loads(self.gig.get_platform_stats())
		self.assertEqual(stats["ai_approved"], 0)
		self.assertEqual(stats["ai_rejected"], 0)
		self.assertEqual(stats["ai_inconclusive"], 0)  # a retry is not a verdict either

	def test_dispute_exec_prompt_failure_returns_retry_later_and_refunds_bond(self):
		jid = self._fund_and_submit()
		stub.NONDET_HOOKS["web_render"] = lambda url, mode: "some real content"

		def _boom(prompt, response_format):
			raise ConnectionError("provider unreachable")
		stub.NONDET_HOOKS["exec_prompt"] = _boom

		# The client may request arbitration on a SUBMITTED milestone at
		# any time; the freelancer would need SUBMIT_TIMEOUT_SECONDS to
		# pass first (covered separately in the timing-gate tests below).
		send_as(CLIENT, self.mod.DISPUTE_BOND)
		res = json.loads(self.gig.dispute_milestone(jid, 0))
		self.assertTrue(res["ok"])
		self.assertEqual(res["verdict"], "RETRY_LATER")
		self.assertIn(self.mod.ERR_TRANSIENT_LLM, res["reason"])
		self.assertIn((str(CLIENT), self.mod.DISPUTE_BOND), stub.PAYMENTS)

	def test_dispute_malformed_llm_output_returns_retry_later_not_a_verdict(self):
		"""A response that does not even parse as the requested JSON shape
		is provider misbehaviour, not evidence about the deliverable -- it
		must never silently become an INCONCLUSIVE verdict on the record."""
		jid = self._fund_and_submit()
		stub.NONDET_HOOKS["web_render"] = lambda url, mode: "some real content"
		stub.NONDET_HOOKS["exec_prompt"] = lambda prompt, response_format: "not json at all"

		send_as(CLIENT, self.mod.DISPUTE_BOND)
		res = json.loads(self.gig.dispute_milestone(jid, 0))
		self.assertTrue(res["ok"])
		self.assertEqual(res["verdict"], "RETRY_LATER")
		self.assertIn(self.mod.ERR_LLM_MALFORMED, res["reason"])

		stats = json.loads(self.gig.get_platform_stats())
		self.assertEqual(stats["ai_inconclusive"], 0)

	def test_preview_dispute_surfaces_retry_later_instead_of_raising(self):
		jid = self._fund_and_submit()
		stub.NONDET_HOOKS["web_render"] = lambda url, mode: (_ for _ in ()).throw(
			TimeoutError("upstream timed out"))
		res = json.loads(self.gig.preview_dispute(jid, 0))
		self.assertTrue(res["ok"])
		self.assertEqual(res["preview_verdict"], "RETRY_LATER")
		self.assertFalse(res["binding"])

	def test_render_call_requests_a_load_wait_for_dynamic_pages(self):
		"""Deliverables are frequently client-rendered apps; without a
		render delay the fetch can capture a blank shell before the page's
		own scripts have painted anything. The stub's NONDET_HOOKS
		deliberately drops extra kwargs (see genlayer_stub.py), so this
		patches _Web.render directly to see exactly what the contract
		passed."""
		jid = self._fund_and_submit()
		seen = {}

		import genlayer_stub as _s
		original = _s._Web.render

		def render_capture(self, url, mode="text", **kwargs):
			seen["kwargs"] = kwargs
			return "rendered content"

		_s._Web.render = render_capture
		try:
			stub.NONDET_HOOKS["exec_prompt"] = lambda prompt, response_format: json.dumps(
				{"verdict": "APPROVED", "score": 80, "reasoning": "ok"})
			send_as(CLIENT, self.mod.DISPUTE_BOND)
			self.gig.dispute_milestone(jid, 0)
		finally:
			_s._Web.render = original

		self.assertEqual(seen["kwargs"].get("wait_after_loaded"),
			self.mod.RENDER_WAIT_AFTER_LOADED)

	# -- O(1) job completion, exercised across several milestones -----------

	def test_job_completes_only_once_every_milestone_is_settled(self):
		send_as(CLIENT)
		job = json.loads(self.gig.create_job(str(FREELANCER), "Multi-phase build"))
		jid = job["job_id"]
		self._fund(jid, amount=1 * ONE_GEN, description="phase 1")
		self._fund(jid, amount=1 * ONE_GEN, description="phase 2")
		self._fund(jid, amount=1 * ONE_GEN, description="phase 3")

		send_as(FREELANCER)
		self.gig.accept_job(jid)
		self.gig.submit_milestone(jid, 0, "https://example.com/1", "n")
		self.gig.submit_milestone(jid, 1, "https://example.com/2", "n")

		send_as(CLIENT)
		self.gig.approve_milestone(jid, 0)
		self.assertEqual(json.loads(self.gig.get_job(jid))["status"], self.mod.STATUS_ACTIVE)
		self.gig.approve_milestone(jid, 1)
		self.assertEqual(json.loads(self.gig.get_job(jid))["status"], self.mod.STATUS_ACTIVE)

		# phase 3 was never submitted -- cancel it instead, which is also
		# a terminal settlement and should complete the job.
		self.gig.cancel_milestone(jid, 2)
		job_after = json.loads(self.gig.get_job(jid))
		self.assertEqual(job_after["status"], self.mod.STATUS_COMPLETED)
		self.assertEqual(job_after["settled_milestone_count"], 3)

	def test_inconclusive_dispute_does_not_advance_the_settled_counter(self):
		jid = self._fund_and_submit()
		send_as(CLIENT)
		self.gig.reject_milestone(jid, 0, "unclear")
		stub.NONDET_HOOKS["web_render"] = lambda url, mode: ""  # empty -> business INCONCLUSIVE

		send_as(FREELANCER, self.mod.DISPUTE_BOND)
		self.gig.dispute_milestone(jid, 0)
		job = json.loads(self.gig.get_job(jid))
		self.assertEqual(job["settled_milestone_count"], 0)
		self.assertEqual(job["status"], self.mod.STATUS_ACTIVE)

	# -- pure helpers ---------------------------------------------------------

	def test_clean_json_extracts_object_from_markdown_fence(self):
		text = "```json\n{\"verdict\": \"APPROVED\", \"score\": 80, \"reasoning\": \"ok\"}\n```"
		parsed = self.mod._clean_json(text)
		self.assertEqual(parsed["verdict"], "APPROVED")
		self.assertEqual(parsed["score"], 80)

	def test_clean_json_returns_none_for_garbage(self):
		self.assertIsNone(self.mod._clean_json("not json at all"))

	def test_coherent_accepts_well_formed_report(self):
		obs = {"verdict": "APPROVED", "score": 90, "reasoning": "fine"}
		self.assertTrue(self.mod._coherent(obs))

	def test_coherent_accepts_well_formed_inconclusive_report(self):
		"""_judge no longer tags a dict with an "ok" flag -- a genuine
		empty-content INCONCLUSIVE report is just a well-formed dict like
		any other verdict, and _coherent treats it identically."""
		obs = {"verdict": "INCONCLUSIVE", "score": 0, "reasoning": ""}
		self.assertTrue(self.mod._coherent(obs))

	def test_coherent_rejects_out_of_range_score(self):
		obs = {"verdict": "APPROVED", "score": 150, "reasoning": "fine"}
		self.assertFalse(self.mod._coherent(obs))

	def test_coherent_rejects_unknown_verdict(self):
		obs = {"verdict": "MAYBE", "score": 50, "reasoning": "fine"}
		self.assertFalse(self.mod._coherent(obs))

	def test_coherent_rejects_oversized_reasoning(self):
		obs = {"verdict": "APPROVED", "score": 50,
			"reasoning": "x" * (self.mod.MAX_REASONING_CHARS + 1)}
		self.assertFalse(self.mod._coherent(obs))

	# -- owner controls ---------------------------------------------------------

	def test_only_owner_can_pause(self):
		send_as(OTHER)
		with self.assertRaises(stub.UserError):
			self.gig.set_paused(True)

	def test_paused_still_allows_settlement_but_not_new_funding(self):
		jid = self._fund_and_submit()
		send_as(OWNER)
		self.gig.set_paused(True)

		send_as(CLIENT, ONE_GEN)
		res = json.loads(self.gig.fund_milestone(jid, "extra milestone"))
		self.assertFalse(res["ok"])

		send_as(CLIENT)
		res = json.loads(self.gig.approve_milestone(jid, 0))
		self.assertTrue(res["ok"])  # settlement of already-escrowed money keeps working


class TalentGateTestCase(unittest.TestCase):
	def setUp(self):
		stub.reset()
		self.mod = load_talentgate()
		self.oracle_addr = stub.Address("0x5555555555555555555555555555555555555555")
		send_as(OWNER)
		self.gate = self.mod.TalentGate(str(self.oracle_addr))

	class _FakeOracle:
		def __init__(self, reputable_addresses, min_completed=3):
			self._addrs = set(str(a) for a in reputable_addresses)
			self._min = min_completed

		def view(self):
			return self

		def has_reputation(self, address, min_completed):
			if str(address) not in self._addrs:
				return False
			return self._min >= int(min_completed)

		def get_reputation(self, address):
			return json.dumps({"address": str(address), "completed_count": self._min})

	def test_request_assignment_reverts_when_not_eligible(self):
		stub.CONTRACT_REGISTRY[str(self.oracle_addr)] = self._FakeOracle([])
		with self.assertRaises(stub.UserError):
			self.gate.request_assignment(str(FREELANCER), "SENIOR")

	def test_request_assignment_succeeds_and_records_history_when_eligible(self):
		stub.CONTRACT_REGISTRY[str(self.oracle_addr)] = self._FakeOracle([FREELANCER], min_completed=15)
		res = json.loads(self.gate.request_assignment(str(FREELANCER), "SENIOR"))
		self.assertTrue(res["ok"])
		self.assertEqual(res["required_completed"], 10)  # default SENIOR threshold

		history = json.loads(self.gate.get_assignments_by_freelancer(str(FREELANCER)))
		self.assertEqual(history["count"], 1)
		self.assertEqual(history["assignments"][0]["role"], "SENIOR")

	def test_preview_eligibility_never_raises_for_unknown_role(self):
		res = json.loads(self.gate.preview_eligibility(str(FREELANCER), "ARCHITECT"))
		self.assertFalse(res["ok"])

	def test_preview_and_request_agree(self):
		stub.CONTRACT_REGISTRY[str(self.oracle_addr)] = self._FakeOracle([FREELANCER], min_completed=1)
		preview = json.loads(self.gate.preview_eligibility(str(FREELANCER), "MID"))
		self.assertFalse(preview["eligible"])  # MID needs 3, freelancer has 1
		with self.assertRaises(stub.UserError):
			self.gate.request_assignment(str(FREELANCER), "MID")

	def test_only_owner_can_change_tier_threshold(self):
		send_as(OTHER)
		with self.assertRaises(stub.UserError):
			self.gate.set_tier_threshold("MID", 1)

	def test_live_lookup_reflects_oracle_changes_without_any_revoke_call(self):
		oracle = self._FakeOracle([], min_completed=0)
		stub.CONTRACT_REGISTRY[str(self.oracle_addr)] = oracle
		self.assertFalse(self.gate.is_eligible(str(FREELANCER), "LEAD"))

		oracle._addrs.add(str(FREELANCER))
		oracle._min = 25
		# Same gate, same method call, no state ever written to TalentGate
		# itself -- eligibility for a high tier is now true purely because
		# the oracle it reads answers differently this time.
		self.assertTrue(self.gate.is_eligible(str(FREELANCER), "LEAD"))


if __name__ == "__main__":
	unittest.main()
