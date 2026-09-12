# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
from genlayer import *
from dataclasses import dataclass
from datetime import datetime, timezone
import json

# ============================================================================
# TalentGate — a hiring gate that reads reputation LIVE from GigEscrow
# ============================================================================
#
# A consumer contract demonstrating GenLayer composability: GigEscrow is the
# oracle of record for "how many milestones has this address actually
# completed", and TalentGate is a separate contract that decides whether that
# is enough to grant a role, without GigEscrow needing to know TalentGate
# exists. A second staffing platform could read the same oracle and set a
# completely different bar.
#
# TalentGate stores no eligibility decision at all: every call re-reads
# GigEscrow's `has_reputation` view live, at call time, via a normal
# synchronous IC-to-IC view() call. There is nothing cached and therefore
# nothing that can go stale — eligibility is exactly as current as the oracle
# it reads, with no separate revoke/lapse step required to keep it honest.

TIER_JUNIOR = "JUNIOR"
TIER_MID = "MID"
TIER_SENIOR = "SENIOR"
TIER_LEAD = "LEAD"
TIERS = [TIER_JUNIOR, TIER_MID, TIER_SENIOR, TIER_LEAD]

DEFAULT_THRESHOLDS = {TIER_JUNIOR: 0, TIER_MID: 3, TIER_SENIOR: 10, TIER_LEAD: 25}

MAX_ROLE_LEN = 60
MAX_SCAN = 200
MAX_PAGE = 40


@gl.contract_interface
class _GigEscrow:
	"""Typed stub for the oracle. Purely for IDE/type-checking convenience —
	at runtime this behaves identically to gl.get_contract_at()."""
	class View:
		def has_reputation(self, address: str, min_completed: int) -> bool: ...
		def get_reputation(self, address: str) -> str: ...

	class Write:
		pass


def _now_epoch() -> int:
	try:
		return int(datetime.now(timezone.utc).timestamp())
	except Exception:
		return 0


@allow_storage
@dataclass
class Assignment:
	assignment_id: u32
	freelancer: Address
	role: str
	required_completed: u32
	granted_epoch: u64


class TalentGate(gl.Contract):
	owner: Address
	oracle: Address

	tier_thresholds: TreeMap[str, u32]

	assignments: TreeMap[u32, Assignment]
	next_assignment_id: u32
	freelancer_assignments: TreeMap[Address, DynArray[u32]]

	count_granted: u32
	count_denied: u32

	def __init__(self, oracle: str):
		self.owner = gl.message.sender_address
		self.oracle = Address(str(oracle))
		for tier in TIERS:
			self.tier_thresholds[tier] = u32(DEFAULT_THRESHOLDS[tier])
		self.next_assignment_id = u32(0)
		self.count_granted = u32(0)
		self.count_denied = u32(0)

	# ── internals ────────────────────────────────────────────────────────

	def _require_owner(self) -> None:
		if str(gl.message.sender_address) != str(self.owner):
			raise gl.vm.UserError("owner only")

	def _threshold_for(self, role: str) -> int:
		found = self.tier_thresholds.get(str(role))
		if found is None:
			raise gl.vm.UserError("unknown role/tier: " + str(role))
		return int(found)

	def _oracle(self):
		return _GigEscrow(self.oracle)

	def _eligible(self, freelancer: str, role: str) -> tuple:
		"""Returns (eligible: bool, threshold: int). A live read, every
		time — see the module docstring for why that removes the need for
		any lapse/revocation machinery."""
		threshold = self._threshold_for(role)
		ok = self._oracle().view().has_reputation(str(freelancer), threshold)
		return (bool(ok), threshold)

	# ── writes ───────────────────────────────────────────────────────────

	@gl.public.write
	def request_assignment(self, freelancer: str, role: str) -> str:
		"""REVERTS when the freelancer does not currently meet the tier's
		reputation bar. The caller wanted a staffing decision, and a silent
		false they forget to check is worse than a stopped transaction. It
		is safe to revert here because this contract holds no funds — it
		grants staffing eligibility, not money."""
		r = str(role)
		if len(r) == 0 or len(r) > MAX_ROLE_LEN:
			raise gl.vm.UserError("role must be 1.." + str(MAX_ROLE_LEN) + " characters")
		ok, threshold = self._eligible(freelancer, r)
		if not ok:
			self.count_denied = u32(int(self.count_denied) + 1)
			raise gl.vm.UserError(str(freelancer) + " does not meet the " + r
				+ " bar of " + str(threshold) + " completed GigEscrow milestones")
		aid = int(self.next_assignment_id) + 1
		self.next_assignment_id = u32(aid)
		f = Address(str(freelancer))
		rec = self.assignments.get_or_insert_default(u32(aid))
		rec.assignment_id = u32(aid)
		rec.freelancer = f
		rec.role = r
		rec.required_completed = u32(threshold)
		rec.granted_epoch = u64(_now_epoch())
		self.freelancer_assignments.get_or_insert_default(f).append(u32(aid))
		self.count_granted = u32(int(self.count_granted) + 1)
		return json.dumps({"ok": True, "assignment_id": aid,
			"freelancer": str(f), "role": r, "required_completed": threshold})

	@gl.public.write
	def preview_eligibility(self, freelancer: str, role: str) -> str:
		"""Never reverts — degrades and explains instead, so a caller can
		check before committing to request_assignment. Both this and
		request_assignment call the identical _eligible(), so nobody learns
		the rule by having a transaction reverted on them."""
		r = str(role)
		if len(r) == 0 or len(r) > MAX_ROLE_LEN:
			return json.dumps({"ok": False, "reason": "role must be 1.."
				+ str(MAX_ROLE_LEN) + " characters"})
		if self.tier_thresholds.get(r) is None:
			return json.dumps({"ok": False, "reason": "unknown role/tier: " + r})
		ok, threshold = self._eligible(freelancer, r)
		return json.dumps({"ok": True, "eligible": ok, "freelancer": str(freelancer),
			"role": r, "required_completed": threshold})

	@gl.public.write
	def set_tier_threshold(self, role: str, min_completed: int) -> str:
		self._require_owner()
		r = str(role)
		if len(r) == 0 or len(r) > MAX_ROLE_LEN:
			raise gl.vm.UserError("role must be 1.." + str(MAX_ROLE_LEN) + " characters")
		m = int(min_completed)
		if m < 0:
			raise gl.vm.UserError("min_completed cannot be negative")
		self.tier_thresholds[r] = u32(m)
		# Assignments already granted are a historical record, not a live
		# credential — raising a bar here never revokes anything already
		# recorded, it only changes what request_assignment allows next.
		return json.dumps({"ok": True, "role": r, "min_completed": m})

	@gl.public.write
	def set_oracle(self, new_oracle: str) -> str:
		self._require_owner()
		self.oracle = Address(str(new_oracle))
		return json.dumps({"ok": True, "oracle": str(new_oracle)})

	@gl.public.write
	def transfer_ownership(self, new_owner: str) -> str:
		self._require_owner()
		self.owner = Address(str(new_owner))
		return json.dumps({"ok": True, "owner": str(new_owner)})

	# ── views ────────────────────────────────────────────────────────────

	@gl.public.view
	def is_eligible(self, freelancer: str, role: str) -> bool:
		ok, _ = self._eligible(freelancer, str(role))
		return ok

	@gl.public.view
	def get_reputation_snapshot(self, freelancer: str) -> str:
		"""Pass-through to the oracle's own view, so a UI never has to know
		GigEscrow's address to show a candidate's track record."""
		return self._oracle().view().get_reputation(str(freelancer))

	@gl.public.view
	def get_tier_thresholds(self) -> str:
		out = {}
		for tier in TIERS:
			found = self.tier_thresholds.get(tier)
			out[tier] = int(found) if found is not None else 0
		return json.dumps(out)

	@gl.public.view
	def get_assignments_by_freelancer(self, freelancer: str) -> str:
		bucket = self.freelancer_assignments.get(Address(str(freelancer)))
		rows = []
		if bucket is not None:
			n = len(bucket)
			i = n - 1
			seen = 0
			while i >= 0 and seen < MAX_SCAN and len(rows) < MAX_PAGE:
				seen += 1
				rec = self.assignments.get(bucket[i])
				i -= 1
				if rec is not None:
					rows.append({"assignment_id": int(rec.assignment_id),
						"role": str(rec.role),
						"required_completed": int(rec.required_completed),
						"granted_epoch": int(rec.granted_epoch)})
		return json.dumps({"freelancer": str(freelancer), "count": len(rows),
			"assignments": rows})

	@gl.public.view
	def get_oracle_config(self) -> str:
		return json.dumps({"owner": str(self.owner), "oracle": str(self.oracle),
			"granted": int(self.count_granted), "denied": int(self.count_denied)})
