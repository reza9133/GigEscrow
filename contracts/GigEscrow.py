# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
from genlayer import *
from dataclasses import dataclass
from datetime import datetime, timezone
import json

# ============================================================================
# GigEscrow — freelance / gig-work milestone escrow with AI arbitration
# ============================================================================
#
# A client funds a job in milestones. The freelancer submits a deliverable
# (a public URL) for each milestone. The client can approve it directly (fast
# path, no AI involved) — but if either side disagrees, or the client simply
# never responds, the OTHER side can escalate to on-chain arbitration: the
# validators independently fetch the deliverable page and ask an LLM whether
# it satisfies the milestone's written specification, then reach consensus
# on exactly one thing — the verdict (APPROVED / REJECTED / INCONCLUSIVE).
#
# This is GenLayer's own stated use case ("Freelance and gig work — was the
# deliverable satisfactory? AI consensus replaces subjective back-and-forth",
# see /understand-genlayer-protocol/typical-use-cases). Unlike a parametric
# weather/price oracle, the underlying question here is genuinely subjective
# — "does this deliverable meet this spec" has no numeric threshold — so the
# Equivalence Principle used is non-strict LLM judgment with a single
# compared field, not a numeric breach test.
#
# Money-safety rule (load-bearing, not optional): a `@gl.public.write.payable`
# method must NEVER raise. `gl.vm.UserError` rolls back contract STORAGE but
# does not return the value that rode in with the call — the GEN would sit in
# the contract, unaccounted for. So every rejection of a payable call refunds
# the sender and returns a normal `{"ok": false, ...}` response instead of
# raising, and callers must read `ok`.
#
# Two-sided "nobody can trap the other side's money" guarantee:
#   - a client who goes silent after a submission does not freeze the
#     freelancer's pay forever: after SUBMIT_TIMEOUT_SECONDS the freelancer
#     may escalate to arbitration unilaterally;
#   - a freelancer who never delivers does not freeze the client's escrow
#     forever: after RECLAIM_TIMEOUT_SECONDS anyone may permissionlessly
#     return the escrowed amount to the client.
#
# There is no shared risk pool and no payout multiplier: every wei escrowed
# for a milestone is either paid to the freelancer or refunded to the client,
# minus a platform fee withheld once, up front, at funding time. That is a
# structural difference from a pooled-risk parametric insurer — this contract
# never has to check solvency, because it never promises more than it holds.

STATUS_OPEN = "OPEN"
STATUS_ACTIVE = "ACTIVE"
STATUS_COMPLETED = "COMPLETED"

MS_PENDING = "PENDING"
MS_SUBMITTED = "SUBMITTED"
MS_APPROVED = "APPROVED"
MS_REJECTED = "REJECTED"
MS_RESOLVED_APPROVED = "RESOLVED_APPROVED"
MS_RESOLVED_REJECTED = "RESOLVED_REJECTED"
MS_CANCELED = "CANCELED"
MS_RECLAIMED = "RECLAIMED"

TERMINAL_STATUSES = (MS_APPROVED, MS_RESOLVED_APPROVED, MS_RESOLVED_REJECTED,
	MS_CANCELED, MS_RECLAIMED)

VERDICT_APPROVED = "APPROVED"
VERDICT_REJECTED = "REJECTED"
VERDICT_INCONCLUSIVE = "INCONCLUSIVE"

BPS_DENOM = 10000
DEFAULT_PLATFORM_FEE_BPS = 250      # 2.5%, withheld once at funding time
MAX_PLATFORM_FEE_BPS = 1000         # 10% hard ceiling, enforced on the setter

DISPUTE_BOND = 10**16               # 0.01 GEN, required to open arbitration
MIN_MILESTONE_AMOUNT = 10**15       # 0.001 GEN
MAX_MILESTONE_AMOUNT = 50 * 10**18  # 50 GEN

MAX_MILESTONES_PER_JOB = 30
MAX_TITLE_LEN = 120
MAX_DESC_LEN = 700
MAX_URL_LEN = 400
MAX_NOTE_LEN = 400
MAX_REASON_LEN = 300
MAX_RENDER_CHARS = 6000
MAX_REASONING_CHARS = 220

# A client that never approves, rejects, or disputes leaves the freelancer
# unpaid indefinitely. After this long since submission, the freelancer may
# escalate to arbitration on their own.
SUBMIT_TIMEOUT_SECONDS = 7 * 86400

# A freelancer that never submits leaves the client's escrow locked
# indefinitely. After this long since funding, anyone may permissionlessly
# return it. Permissionless on purpose — a release path only the client can
# trigger is not a guarantee.
RECLAIM_TIMEOUT_SECONDS = 30 * 86400

# Outbound transfers apply on FINALIZATION, not acceptance, so a payout still
# sits in the on-chain balance after the receipt says it succeeded. Sweeping
# fee revenue within this window of the last transfer would risk sweeping
# money that is, for a few more blocks, still committed.
SWEEP_DELAY_SECONDS = 3600

MAX_SCAN = 200
MAX_PAGE = 40


def _clamp(value: int, low: int, high: int) -> int:
	if value < low:
		return low
	if value > high:
		return high
	return value


def _now_epoch() -> int:
	"""Seconds since epoch, taken from the transaction's own deterministic
	clock (see /developers/intelligent-contracts/features/transaction-context).
	Every validator re-executing this transaction sees the identical value.
	0 means "clock unavailable"; every caller refuses a time-based decision
	on 0 rather than reading a parse failure as 1970."""
	try:
		return int(datetime.now(timezone.utc).timestamp())
	except Exception:
		return 0


def _clean_json(text: str):
	"""LLMs sometimes wrap JSON in markdown fences or add stray prose around
	it even when response_format='json' is requested. Extract the outermost
	{...} span and parse just that."""
	first = text.find("{")
	last = text.rfind("}")
	if first == -1 or last == -1 or last < first:
		return None
	try:
		return json.loads(text[first:last + 1])
	except Exception:
		return None


# ── Nondeterministic work. Module level, and captures only plain primitives
# passed in as arguments — never `self`, never a storage-backed dataclass.
# A closure over a storage object drags it into pickling for the sub-VM and
# fails the leader before any fetch even happens.

def _judge(description: str, deliverable_url: str, note: str) -> dict:
	"""Fetch the deliverable and ask an LLM whether it satisfies the spec.

	Returns a small, fully self-describing dict. Everything in it is stored
	as evidence, but — critically — only the `verdict` field is ever compared
	between the leader and a validator (see `_adjudicate`). Two independent
	LLM calls will almost never produce identical `reasoning` text even when
	they agree completely on the outcome; comparing the whole object would
	make every arbitration land UNDETERMINED regardless of how obvious the
	deliverable is.
	"""
	out = {"ok": False, "verdict": VERDICT_INCONCLUSIVE, "score": 0,
		"reasoning": "", "fetch_note": "", "content_len": 0}
	try:
		content = gl.nondet.web.render(deliverable_url, mode="text")
	except Exception as e:
		out["fetch_note"] = "fetch failed: " + str(e)[:150]
		return out
	if not isinstance(content, str):
		content = str(content)
	out["content_len"] = len(content)
	if len(content) == 0:
		out["fetch_note"] = "deliverable page returned no readable text"
		return out
	if len(content) > MAX_RENDER_CHARS:
		content = content[:MAX_RENDER_CHARS]

	prompt = (
		"You are an impartial reviewer for a freelance-work escrow platform.\n"
		"Decide whether the fetched deliverable content satisfies the milestone\n"
		"specification below. Judge only what is actually present in the fetched\n"
		"content; do not assume or invent anything about it, and do not reward\n"
		"vague or off-topic content that merely mentions similar keywords.\n\n"
		"MILESTONE SPECIFICATION:\n" + description[:MAX_DESC_LEN] + "\n\n"
		"FREELANCER'S SUBMISSION NOTE:\n" + note[:MAX_NOTE_LEN] + "\n\n"
		"FETCHED DELIVERABLE CONTENT:\n" + content + "\n\n"
		"Respond with APPROVED only if the content substantially and verifiably\n"
		"satisfies the specification. Respond with REJECTED if the content\n"
		"clearly fails to satisfy it. Respond with INCONCLUSIVE only if the\n"
		"content is empty, broken, paywalled, or unrelated to the specification\n"
		"in a way that makes judgment impossible either way.\n\n"
		"Respond as JSON only, with exactly these keys:\n"
		"{\"verdict\": \"APPROVED\" or \"REJECTED\" or \"INCONCLUSIVE\",\n"
		" \"score\": integer from 0 to 100,\n"
		" \"reasoning\": short string, under 200 characters}"
	)
	try:
		raw = gl.nondet.exec_prompt(prompt, response_format="json")
	except Exception as e:
		out["fetch_note"] = "llm call failed: " + str(e)[:150]
		return out

	data = raw if isinstance(raw, dict) else _clean_json(str(raw))
	if not isinstance(data, dict):
		out["fetch_note"] = "unparseable llm response"
		return out

	verdict = str(data.get("verdict", "")).strip().upper()
	if verdict not in (VERDICT_APPROVED, VERDICT_REJECTED, VERDICT_INCONCLUSIVE):
		verdict = VERDICT_INCONCLUSIVE

	try:
		score = int(data.get("score", 0))
	except Exception:
		score = 0
	score = _clamp(score, 0, 100)

	reasoning = str(data.get("reasoning", ""))
	if len(reasoning) > MAX_REASONING_CHARS:
		reasoning = reasoning[:MAX_REASONING_CHARS]

	out["ok"] = True
	out["verdict"] = verdict
	out["score"] = score
	out["reasoning"] = reasoning
	return out


def _coherent(obs) -> bool:
	"""Is the leader's own report internally consistent with itself?

	Validators compare exactly one field (`verdict`) between their own run
	and the leader's — comparing anything else would turn ordinary LLM
	wording variance into constant UNDETERMINED rounds. That leaves a gap: a
	leader could report VERDICT_APPROVED with a `score` of -50 or a
	`reasoning` string a kilobyte long, and no validator would ever notice
	because those fields are never compared. This check closes that gap
	without adding a cross-validator comparison: it is a pure function of the
	leader's OWN calldata, so every validator computes the identical answer
	from it and it can reject a malformed report without ever itself being a
	source of disagreement.
	"""
	if not isinstance(obs, dict):
		return False
	if not obs.get("ok"):
		return True
	if obs.get("verdict") not in (VERDICT_APPROVED, VERDICT_REJECTED, VERDICT_INCONCLUSIVE):
		return False
	score = obs.get("score")
	if not isinstance(score, int) or score < 0 or score > 100:
		return False
	reasoning = obs.get("reasoning")
	if not isinstance(reasoning, str) or len(reasoning) > MAX_REASONING_CHARS:
		return False
	return True


@gl.evm.contract_interface
class _Wallet:
	"""Sending value to an EOA is an external message through the ghost
	contract; this empty interface is the documented way to address one."""
	class View:
		pass

	class Write:
		pass


@allow_storage
@dataclass
class Job:
	job_id: u32
	client: Address
	freelancer: Address
	title: str
	status: str
	milestone_count: u32
	funded_total: u128
	released_total: u128
	refunded_total: u128
	created_epoch: u64
	completed_epoch: u64


@allow_storage
@dataclass
class Milestone:
	job_id: u32
	index: u32
	description: str
	amount: u128
	escrowed_amount: u128
	status: str
	deliverable_url: str
	deliverable_note: str
	reject_reason: str
	funded_epoch: u64
	submitted_epoch: u64
	decided_epoch: u64
	ai_verdict: str
	ai_score: u32
	ai_reasoning: str
	ai_fetch_note: str
	disputed_by: Address
	dispute_bond: u128


@allow_storage
@dataclass
class Reputation:
	completed_count: u32
	ai_win_count: u32
	ai_loss_count: u32
	total_earned: u128


class GigEscrow(gl.Contract):
	owner: Address
	paused: bool

	jobs: TreeMap[u32, Job]
	job_ids: DynArray[u32]
	next_job_id: u32

	# Keyed by "job_id:index" rather than a packed integer — TreeMap keys
	# must be one of the plain comparable scalar types, and a composite
	# string key is the straightforward way to combine two ids into one.
	milestones: TreeMap[str, Milestone]
	job_milestone_ids: TreeMap[u32, DynArray[str]]

	client_jobs: TreeMap[Address, DynArray[u32]]
	freelancer_jobs: TreeMap[Address, DynArray[u32]]
	freelancer_reputation: TreeMap[Address, Reputation]

	platform_fee_bps: u32
	platform_fees_accrued: u128
	platform_fees_withdrawn: u128

	escrow_locked: u128
	total_released: u128
	total_refunded: u128
	total_bonds_forfeited: u128
	last_out_epoch: u64

	count_jobs: u32
	count_milestones: u32
	count_ai_approved: u32
	count_ai_rejected: u32
	count_ai_inconclusive: u32

	def __init__(self, platform_fee_bps: int = DEFAULT_PLATFORM_FEE_BPS):
		self.owner = gl.message.sender_address
		self.paused = False
		self.next_job_id = u32(0)
		self.platform_fee_bps = u32(_clamp(int(platform_fee_bps), 0, MAX_PLATFORM_FEE_BPS))
		self.platform_fees_accrued = u128(0)
		self.platform_fees_withdrawn = u128(0)
		self.escrow_locked = u128(0)
		self.total_released = u128(0)
		self.total_refunded = u128(0)
		self.total_bonds_forfeited = u128(0)
		self.last_out_epoch = u64(0)
		self.count_jobs = u32(0)
		self.count_milestones = u32(0)
		self.count_ai_approved = u32(0)
		self.count_ai_rejected = u32(0)
		self.count_ai_inconclusive = u32(0)

	# ── internals ────────────────────────────────────────────────────────

	def _now(self) -> int:
		return _now_epoch()

	def _pay(self, to: Address, amount: int) -> None:
		"""The single outbound choke point. last_out_epoch cannot be
		forgotten at a call site because there is only one call site."""
		if amount <= 0:
			return
		_Wallet(Address(str(to))).emit_transfer(value=u256(int(amount)))
		self.last_out_epoch = u64(self._now())

	def _reject(self, sender: Address, value: int, reason: str) -> str:
		"""Refund a payable call and RETURN — never raise from a payable
		path. See the module docstring: a UserError here would roll back
		storage but not the incoming value, stranding it in the contract."""
		if value > 0:
			self._pay(sender, value)
		return json.dumps({"ok": False, "reason": reason, "refunded": str(value)})

	def _mkey(self, job_id: int, index: int) -> str:
		return str(int(job_id)) + ":" + str(int(index))

	def _get_job(self, job_id: int) -> Job:
		found = self.jobs.get(u32(int(job_id)))
		if found is None:
			raise gl.vm.UserError("unknown job_id")
		return found

	def _get_ms(self, job_id: int, index: int) -> Milestone:
		found = self.milestones.get(self._mkey(job_id, index))
		if found is None:
			raise gl.vm.UserError("unknown milestone index for this job")
		return found

	def _bump_reputation(self, freelancer: Address, earned: int, ai_win: bool,
			ai_loss: bool) -> None:
		rep = self.freelancer_reputation.get_or_insert_default(freelancer)
		rep.completed_count = u32(int(rep.completed_count) + (1 if earned > 0 else 0))
		if ai_win:
			rep.ai_win_count = u32(int(rep.ai_win_count) + 1)
		if ai_loss:
			rep.ai_loss_count = u32(int(rep.ai_loss_count) + 1)
		if earned > 0:
			rep.total_earned = u128(int(rep.total_earned) + earned)

	def _maybe_complete_job(self, job: Job) -> None:
		ids = self.job_milestone_ids.get(int(job.job_id))
		if ids is None:
			return
		if int(job.milestone_count) == 0:
			return
		n = len(ids)
		i = 0
		while i < n:
			ms = self.milestones.get(ids[i])
			i += 1
			if ms is None:
				return
			if str(ms.status) not in TERMINAL_STATUSES:
				return
		job.status = STATUS_COMPLETED
		job.completed_epoch = u64(self._now())

	def _adjudicate(self, description: str, deliverable_url: str,
			note: str) -> dict:
		"""Fetch + judge, and reach consensus on exactly one field.

		Every caller — dispute_milestone and preview_dispute — goes through
		this one function, so a preview can never differ from a real
		arbitration in anything but whether its result is written into a
		binding decision.
		"""
		def leader_fn() -> dict:
			return _judge(description, deliverable_url, note)

		def validator_fn(leaders_res: gl.vm.Result) -> bool:
			if not isinstance(leaders_res, gl.vm.Return):
				return False
			theirs = leaders_res.calldata
			if not _coherent(theirs):
				return False
			mine = leader_fn()
			return str(mine.get("verdict")) == str(theirs.get("verdict"))

		return gl.vm.run_nondet_unsafe(leader_fn, validator_fn)

	# ── writes: job & milestone lifecycle ───────────────────────────────

	@gl.public.write
	def create_job(self, freelancer: str, title: str) -> str:
		"""Open a job naming a specific freelancer. No money moves here —
		fund_milestone is what escrows GEN, one milestone at a time."""
		sender = gl.message.sender_address
		f = Address(str(freelancer))
		if str(f) == str(sender):
			raise gl.vm.UserError("client and freelancer must differ")
		t = str(title)
		if len(t) == 0 or len(t) > MAX_TITLE_LEN:
			raise gl.vm.UserError("title must be 1.." + str(MAX_TITLE_LEN) + " characters")
		jid = int(self.next_job_id) + 1
		self.next_job_id = u32(jid)
		job = self.jobs.get_or_insert_default(u32(jid))
		job.job_id = u32(jid)
		job.client = sender
		job.freelancer = f
		job.title = t
		job.status = STATUS_OPEN
		job.milestone_count = u32(0)
		job.funded_total = u128(0)
		job.released_total = u128(0)
		job.refunded_total = u128(0)
		job.created_epoch = u64(self._now())
		job.completed_epoch = u64(0)
		self.job_ids.append(u32(jid))
		self.client_jobs.get_or_insert_default(sender).append(u32(jid))
		self.freelancer_jobs.get_or_insert_default(f).append(u32(jid))
		self.count_jobs = u32(int(self.count_jobs) + 1)
		return json.dumps({"ok": True, "job_id": jid, "client": str(sender),
			"freelancer": str(f), "status": STATUS_OPEN})

	@gl.public.write
	def accept_job(self, job_id: int) -> str:
		"""The named freelancer opts in. Nobody can be assigned work they
		never agreed to."""
		sender = gl.message.sender_address
		job = self._get_job(job_id)
		if str(job.freelancer) != str(sender):
			raise gl.vm.UserError("only the named freelancer can accept this job")
		if str(job.status) != STATUS_OPEN:
			raise gl.vm.UserError("job is " + str(job.status) + ", not OPEN")
		if int(job.milestone_count) == 0:
			raise gl.vm.UserError("job has no funded milestones yet")
		job.status = STATUS_ACTIVE
		return json.dumps({"ok": True, "job_id": int(job_id), "status": STATUS_ACTIVE})

	def _fund_problem(self, job: Job, sender: Address, value: int,
			description: str) -> str:
		if self.paused:
			return "GigEscrow is paused for new funding"
		if str(job.client) != str(sender):
			return "only the client who created this job can fund a milestone"
		if str(job.status) not in (STATUS_OPEN, STATUS_ACTIVE):
			return "job is " + str(job.status) + ", not open for funding"
		if int(job.milestone_count) >= MAX_MILESTONES_PER_JOB:
			return "job already has the maximum of " + str(MAX_MILESTONES_PER_JOB) + " milestones"
		d = str(description)
		if len(d) == 0 or len(d) > MAX_DESC_LEN:
			return "description must be 1.." + str(MAX_DESC_LEN) + " characters"
		if value < MIN_MILESTONE_AMOUNT:
			return "amount below minimum of " + str(MIN_MILESTONE_AMOUNT) + " wei"
		if value > MAX_MILESTONE_AMOUNT:
			return "amount above maximum of " + str(MAX_MILESTONE_AMOUNT) + " wei"
		if self._now() <= 0:
			return "clock unavailable; cannot open a milestone"
		return ""

	@gl.public.write.payable
	def fund_milestone(self, job_id: int, description: str) -> str:
		"""Add and fund the next milestone on a job in one call. The amount
		is whatever GEN value is sent with the transaction.

		Payable, so this NEVER raises: a rejected call refunds the sender
		and returns {"ok": false, ...} as a successful transaction. Callers
		must read `ok`.
		"""
		sender = gl.message.sender_address
		value = int(gl.message.value)
		jid = int(job_id)
		job = self.jobs.get(u32(jid))
		if job is None:
			return self._reject(sender, value, "unknown job_id")
		problem = self._fund_problem(job, sender, value, description)
		if problem != "":
			return self._reject(sender, value, problem)

		index = int(job.milestone_count)
		fee = (value // BPS_DENOM) * int(self.platform_fee_bps)
		escrowed = value - fee
		now = self._now()

		ms = self.milestones.get_or_insert_default(self._mkey(jid, index))
		ms.job_id = u32(jid)
		ms.index = u32(index)
		ms.description = str(description)[:MAX_DESC_LEN]
		ms.amount = u128(value)
		ms.escrowed_amount = u128(escrowed)
		ms.status = MS_PENDING
		ms.deliverable_url = ""
		ms.deliverable_note = ""
		ms.reject_reason = ""
		ms.funded_epoch = u64(now)
		ms.submitted_epoch = u64(0)
		ms.decided_epoch = u64(0)
		ms.ai_verdict = ""
		ms.ai_score = u32(0)
		ms.ai_reasoning = ""
		ms.ai_fetch_note = ""
		ms.disputed_by = Address("0x0000000000000000000000000000000000000000")
		ms.dispute_bond = u128(0)

		ids = self.job_milestone_ids.get_or_insert_default(jid)
		ids.append(self._mkey(jid, index))
		job.milestone_count = u32(index + 1)
		job.funded_total = u128(int(job.funded_total) + value)

		self.escrow_locked = u128(int(self.escrow_locked) + escrowed)
		self.platform_fees_accrued = u128(int(self.platform_fees_accrued) + fee)
		self.count_milestones = u32(int(self.count_milestones) + 1)
		return json.dumps({"ok": True, "job_id": jid, "milestone_index": index,
			"amount": str(value), "fee": str(fee), "escrowed": str(escrowed),
			"status": MS_PENDING})

	@gl.public.write
	def submit_milestone(self, job_id: int, index: int, deliverable_url: str,
			note: str) -> str:
		sender = gl.message.sender_address
		job = self._get_job(job_id)
		ms = self._get_ms(job_id, index)
		if str(job.freelancer) != str(sender):
			raise gl.vm.UserError("only the assigned freelancer can submit")
		if str(ms.status) != MS_PENDING:
			raise gl.vm.UserError("milestone is " + str(ms.status) + ", not PENDING")
		url = str(deliverable_url)
		if len(url) == 0 or len(url) > MAX_URL_LEN:
			raise gl.vm.UserError("deliverable_url must be 1.." + str(MAX_URL_LEN) + " characters")
		now = self._now()
		if now <= 0:
			raise gl.vm.UserError("clock unavailable")
		ms.deliverable_url = url
		ms.deliverable_note = str(note)[:MAX_NOTE_LEN]
		ms.status = MS_SUBMITTED
		ms.submitted_epoch = u64(now)
		return json.dumps({"ok": True, "job_id": int(job_id), "index": int(index),
			"status": MS_SUBMITTED})

	@gl.public.write
	def approve_milestone(self, job_id: int, index: int) -> str:
		"""The fast path: the client is satisfied and pays without invoking
		any AI arbitration at all."""
		sender = gl.message.sender_address
		job = self._get_job(job_id)
		ms = self._get_ms(job_id, index)
		if str(job.client) != str(sender):
			raise gl.vm.UserError("only the client can approve")
		if str(ms.status) != MS_SUBMITTED:
			raise gl.vm.UserError("milestone is " + str(ms.status) + ", not SUBMITTED")
		now = self._now()
		paid = int(ms.escrowed_amount)
		self.escrow_locked = u128(int(self.escrow_locked) - paid if int(self.escrow_locked) >= paid else 0)
		job.released_total = u128(int(job.released_total) + paid)
		self.total_released = u128(int(self.total_released) + paid)
		ms.status = MS_APPROVED
		ms.decided_epoch = u64(now)
		ms.ai_verdict = ""
		self._bump_reputation(job.freelancer, paid, False, False)
		self._pay(job.freelancer, paid)
		self._maybe_complete_job(job)
		return json.dumps({"ok": True, "job_id": int(job_id), "index": int(index),
			"status": MS_APPROVED, "paid": str(paid)})

	@gl.public.write
	def reject_milestone(self, job_id: int, index: int, reason: str) -> str:
		"""The client's unilateral, informal rejection. It pays nothing and
		refunds nothing by itself — it only records a disagreement. The
		freelancer decides whether to accept it or escalate to arbitration
		with dispute_milestone."""
		sender = gl.message.sender_address
		job = self._get_job(job_id)
		ms = self._get_ms(job_id, index)
		if str(job.client) != str(sender):
			raise gl.vm.UserError("only the client can reject")
		if str(ms.status) != MS_SUBMITTED:
			raise gl.vm.UserError("milestone is " + str(ms.status) + ", not SUBMITTED")
		now = self._now()
		ms.status = MS_REJECTED
		ms.reject_reason = str(reason)[:MAX_REASON_LEN]
		ms.decided_epoch = u64(now)
		return json.dumps({"ok": True, "job_id": int(job_id), "index": int(index),
			"status": MS_REJECTED})

	def _dispute_problem(self, job: Job, ms: Milestone, sender: Address,
			now: int, bond: int) -> str:
		st = str(ms.status)
		if st == MS_SUBMITTED:
			if str(sender) != str(job.client) and str(sender) != str(job.freelancer):
				return "only the client or freelancer on this job may open arbitration"
			if str(sender) == str(job.client):
				pass  # client may request a neutral ruling at any time
			else:
				elapsed = now - int(ms.submitted_epoch)
				if elapsed < SUBMIT_TIMEOUT_SECONDS:
					return ("freelancer may escalate an unanswered submission after "
						+ str(SUBMIT_TIMEOUT_SECONDS) + "s; only "
						+ str(elapsed) + "s have passed")
		elif st == MS_REJECTED:
			if str(sender) != str(job.freelancer):
				return "only the freelancer may dispute a rejection"
		else:
			return "milestone is " + st + ", not open to arbitration"
		if now <= 0:
			return "clock unavailable"
		if bond < DISPUTE_BOND:
			return "dispute bond of " + str(DISPUTE_BOND) + " wei is required"
		return ""

	@gl.public.write.payable
	def dispute_milestone(self, job_id: int, index: int) -> str:
		"""Escalate to on-chain AI arbitration. Payable, so this never
		raises — every rejection refunds the bond and returns {"ok": false}.

		Resolution:
		  APPROVED     -> freelancer is paid, bond returned to whoever filed.
		  REJECTED     -> client is refunded. If the freelancer was
		                  challenging the client's own REJECTED verdict and
		                  lost, the bond compensates the client for a failed
		                  challenge; in every other case the bond is simply
		                  returned, because requesting a neutral ruling on an
		                  undecided SUBMITTED milestone is not a bet against
		                  anyone.
		  INCONCLUSIVE -> nothing is paid or refunded, the bond is returned,
		                  and the milestone's status is left exactly as it
		                  was so arbitration can be retried later. "The
		                  content could not be judged" is not evidence
		                  against either side.
		"""
		sender = gl.message.sender_address
		bond = int(gl.message.value)
		jid = int(job_id)
		idx = int(index)
		job = self.jobs.get(u32(jid))
		if job is None:
			return self._reject(sender, bond, "unknown job_id")
		ms = self.milestones.get(self._mkey(jid, idx))
		if ms is None:
			return self._reject(sender, bond, "unknown milestone index for this job")
		now = self._now()
		problem = self._dispute_problem(job, ms, sender, now, bond)
		if problem != "":
			return self._reject(sender, bond, problem)

		challenging_rejection = str(ms.status) == MS_REJECTED
		description = str(ms.description)
		url = str(ms.deliverable_url)
		note = str(ms.deliverable_note)
		res = self._adjudicate(description, url, note)
		obs = res if isinstance(res, dict) else {}
		verdict = str(obs.get("verdict", VERDICT_INCONCLUSIVE))

		ms.ai_verdict = verdict
		ms.ai_score = u32(int(obs.get("score", 0)))
		ms.ai_reasoning = str(obs.get("reasoning", ""))[:MAX_REASONING_CHARS]
		ms.ai_fetch_note = str(obs.get("fetch_note", ""))[:200]
		ms.disputed_by = sender
		ms.dispute_bond = u128(bond)
		ms.decided_epoch = u64(now)

		paid = 0
		refunded = 0
		if verdict == VERDICT_APPROVED:
			paid = int(ms.escrowed_amount)
			self.escrow_locked = u128(int(self.escrow_locked) - paid if int(self.escrow_locked) >= paid else 0)
			job.released_total = u128(int(job.released_total) + paid)
			self.total_released = u128(int(self.total_released) + paid)
			ms.status = MS_RESOLVED_APPROVED
			self.count_ai_approved = u32(int(self.count_ai_approved) + 1)
			self._bump_reputation(job.freelancer, paid, True, False)
			self._pay(job.freelancer, paid)
			self._pay(sender, bond)
		elif verdict == VERDICT_REJECTED:
			refunded = int(ms.escrowed_amount)
			self.escrow_locked = u128(int(self.escrow_locked) - refunded if int(self.escrow_locked) >= refunded else 0)
			job.refunded_total = u128(int(job.refunded_total) + refunded)
			self.total_refunded = u128(int(self.total_refunded) + refunded)
			ms.status = MS_RESOLVED_REJECTED
			self.count_ai_rejected = u32(int(self.count_ai_rejected) + 1)
			self._bump_reputation(job.freelancer, 0, False, True)
			self._pay(job.client, refunded)
			if challenging_rejection:
				self._pay(job.client, bond)
				self.total_bonds_forfeited = u128(int(self.total_bonds_forfeited) + bond)
			else:
				self._pay(sender, bond)
		else:
			self.count_ai_inconclusive = u32(int(self.count_ai_inconclusive) + 1)
			self._pay(sender, bond)

		self._maybe_complete_job(job)
		return json.dumps({"ok": True, "job_id": jid, "index": idx,
			"verdict": verdict, "status": str(ms.status),
			"paid_to_freelancer": str(paid), "refunded_to_client": str(refunded),
			"bond_forfeited": bool(verdict == VERDICT_REJECTED and challenging_rejection),
			"score": int(ms.ai_score), "reasoning": str(ms.ai_reasoning),
			"fetch_note": str(ms.ai_fetch_note)})

	@gl.public.write
	def preview_dispute(self, job_id: int, index: int) -> str:
		"""Run the identical arbitration with no bond and no binding effect.
		Anyone may call it — the verdict is a judgment about a public URL,
		not a secret — so either side can see how arbitration would likely
		go before actually staking a bond on it."""
		job = self._get_job(job_id)
		ms = self._get_ms(job_id, index)
		if str(ms.status) not in (MS_SUBMITTED, MS_REJECTED):
			raise gl.vm.UserError("milestone is " + str(ms.status) + ", not open to arbitration")
		res = self._adjudicate(str(ms.description), str(ms.deliverable_url),
			str(ms.deliverable_note))
		obs = res if isinstance(res, dict) else {}
		verdict = str(obs.get("verdict", VERDICT_INCONCLUSIVE))
		return json.dumps({"ok": True, "job_id": int(job_id), "index": int(index),
			"preview_verdict": verdict, "score": int(obs.get("score", 0)),
			"reasoning": str(obs.get("reasoning", ""))[:MAX_REASONING_CHARS],
			"fetch_note": str(obs.get("fetch_note", ""))[:200],
			"binding": False})

	@gl.public.write
	def cancel_milestone(self, job_id: int, index: int) -> str:
		"""The client gives up on a milestone before any work was submitted.
		Refuses once SUBMITTED — canceling after delivery to dodge payment
		is exactly the trap this contract is built to prevent."""
		sender = gl.message.sender_address
		job = self._get_job(job_id)
		ms = self._get_ms(job_id, index)
		if str(job.client) != str(sender):
			raise gl.vm.UserError("only the client can cancel")
		if str(ms.status) != MS_PENDING:
			raise gl.vm.UserError("milestone is " + str(ms.status) + ", not PENDING")
		refund = int(ms.escrowed_amount)
		self.escrow_locked = u128(int(self.escrow_locked) - refund if int(self.escrow_locked) >= refund else 0)
		job.refunded_total = u128(int(job.refunded_total) + refund)
		self.total_refunded = u128(int(self.total_refunded) + refund)
		ms.status = MS_CANCELED
		ms.decided_epoch = u64(self._now())
		self._pay(sender, refund)
		self._maybe_complete_job(job)
		return json.dumps({"ok": True, "job_id": int(job_id), "index": int(index),
			"status": MS_CANCELED, "refunded": str(refund)})

	@gl.public.write
	def reclaim_milestone(self, job_id: int, index: int) -> str:
		"""Permissionless. If a freelancer never submits, the client's
		escrow should not be locked forever just because only the client
		could otherwise release it — a release path only one party can
		trigger is not a guarantee."""
		job = self._get_job(job_id)
		ms = self._get_ms(job_id, index)
		if str(ms.status) != MS_PENDING:
			raise gl.vm.UserError("milestone is " + str(ms.status) + ", not PENDING")
		now = self._now()
		if now <= 0:
			raise gl.vm.UserError("clock unavailable")
		elapsed = now - int(ms.funded_epoch)
		if elapsed < RECLAIM_TIMEOUT_SECONDS:
			raise gl.vm.UserError("reclaimable after " + str(RECLAIM_TIMEOUT_SECONDS)
				+ "s of no submission; only " + str(elapsed) + "s have passed")
		refund = int(ms.escrowed_amount)
		self.escrow_locked = u128(int(self.escrow_locked) - refund if int(self.escrow_locked) >= refund else 0)
		job.refunded_total = u128(int(job.refunded_total) + refund)
		self.total_refunded = u128(int(self.total_refunded) + refund)
		ms.status = MS_RECLAIMED
		ms.decided_epoch = u64(now)
		self._pay(job.client, refund)
		self._maybe_complete_job(job)
		return json.dumps({"ok": True, "job_id": int(job_id), "index": int(index),
			"status": MS_RECLAIMED, "refunded": str(refund)})

	# ── owner ────────────────────────────────────────────────────────────

	def _require_owner(self) -> None:
		if str(gl.message.sender_address) != str(self.owner):
			raise gl.vm.UserError("owner only")

	@gl.public.write
	def set_paused(self, value: bool) -> str:
		"""Pause gates fund_milestone and NOTHING else — submission,
		approval, rejection, arbitration, cancellation and reclaiming all
		keep working while paused. An owner who could freeze settlement of
		money already escrowed would have the same leverage as one who could
		deny a payout outright, just through a slower route."""
		self._require_owner()
		self.paused = bool(value)
		return json.dumps({"ok": True, "paused": bool(value)})

	@gl.public.write
	def set_platform_fee_bps(self, bps: int) -> str:
		self._require_owner()
		b = int(bps)
		if b < 0 or b > MAX_PLATFORM_FEE_BPS:
			raise gl.vm.UserError("fee must be 0.." + str(MAX_PLATFORM_FEE_BPS) + " bps")
		self.platform_fee_bps = u32(b)
		# Fee is withheld once, at funding time, and stored on the milestone
		# — changing the rate here can never touch escrow already funded.
		return json.dumps({"ok": True, "platform_fee_bps": b})

	@gl.public.write
	def transfer_ownership(self, new_owner: str) -> str:
		self._require_owner()
		self.owner = Address(str(new_owner))
		return json.dumps({"ok": True, "owner": str(new_owner)})

	@gl.public.write
	def withdraw_platform_fees(self, to: str, amount: int) -> str:
		"""Only fees actually accrued and not yet withdrawn, and never
		within SWEEP_DELAY_SECONDS of the last outbound transfer: outbound
		transfers apply on finalization, not acceptance, so a transfer can
		still be in flight when its receipt already says it succeeded."""
		self._require_owner()
		now = self._now()
		if now <= 0:
			raise gl.vm.UserError("clock unavailable")
		last = int(self.last_out_epoch)
		if last > 0 and now - last < SWEEP_DELAY_SECONDS:
			raise gl.vm.UserError("wait " + str(SWEEP_DELAY_SECONDS - (now - last))
				+ "s for outbound transfers to finalize")
		available = int(self.platform_fees_accrued) - int(self.platform_fees_withdrawn)
		on_chain_free = int(self.balance) - int(self.escrow_locked)
		if on_chain_free < available:
			available = on_chain_free
		if available <= 0:
			raise gl.vm.UserError("no withdrawable fees right now")
		take = int(amount)
		if take <= 0 or take > available:
			take = available
		self.platform_fees_withdrawn = u128(int(self.platform_fees_withdrawn) + take)
		self._pay(Address(str(to)), take)
		return json.dumps({"ok": True, "withdrawn": str(take)})

	# ── views ────────────────────────────────────────────────────────────

	def _job_json(self, job: Job) -> dict:
		return {"job_id": int(job.job_id), "client": str(job.client),
			"freelancer": str(job.freelancer), "title": str(job.title),
			"status": str(job.status), "milestone_count": int(job.milestone_count),
			"funded_total": str(int(job.funded_total)),
			"released_total": str(int(job.released_total)),
			"refunded_total": str(int(job.refunded_total)),
			"created_epoch": int(job.created_epoch),
			"completed_epoch": int(job.completed_epoch)}

	def _ms_json(self, ms: Milestone) -> dict:
		return {"job_id": int(ms.job_id), "index": int(ms.index),
			"description": str(ms.description), "amount": str(int(ms.amount)),
			"escrowed_amount": str(int(ms.escrowed_amount)),
			"status": str(ms.status), "deliverable_url": str(ms.deliverable_url),
			"deliverable_note": str(ms.deliverable_note),
			"reject_reason": str(ms.reject_reason),
			"funded_epoch": int(ms.funded_epoch),
			"submitted_epoch": int(ms.submitted_epoch),
			"decided_epoch": int(ms.decided_epoch),
			"ai_verdict": str(ms.ai_verdict), "ai_score": int(ms.ai_score),
			"ai_reasoning": str(ms.ai_reasoning),
			"ai_fetch_note": str(ms.ai_fetch_note),
			"disputed_by": str(ms.disputed_by),
			"dispute_bond": str(int(ms.dispute_bond))}

	@gl.public.view
	def get_job(self, job_id: int) -> str:
		job = self.jobs.get(u32(int(job_id)))
		if job is None:
			return json.dumps({"found": False})
		out = self._job_json(job)
		out["found"] = True
		return json.dumps(out)

	@gl.public.view
	def get_milestone(self, job_id: int, index: int) -> str:
		ms = self.milestones.get(self._mkey(job_id, index))
		if ms is None:
			return json.dumps({"found": False})
		out = self._ms_json(ms)
		out["found"] = True
		return json.dumps(out)

	@gl.public.view
	def get_job_milestones(self, job_id: int) -> str:
		ids = self.job_milestone_ids.get(int(job_id))
		rows = []
		if ids is not None:
			n = len(ids)
			i = 0
			while i < n and i < MAX_PAGE:
				ms = self.milestones.get(ids[i])
				if ms is not None:
					rows.append(self._ms_json(ms))
				i += 1
		return json.dumps({"job_id": int(job_id), "count": len(rows), "milestones": rows})

	def _jobs_for(self, bucket) -> list:
		out = []
		if bucket is None:
			return out
		n = len(bucket)
		i = n - 1
		seen = 0
		while i >= 0 and seen < MAX_SCAN and len(out) < MAX_PAGE:
			seen += 1
			job = self.jobs.get(bucket[i])
			i -= 1
			if job is not None:
				out.append(self._job_json(job))
		return out

	@gl.public.view
	def get_jobs_by_client(self, address: str) -> str:
		rows = self._jobs_for(self.client_jobs.get(Address(str(address))))
		return json.dumps({"address": str(address), "count": len(rows), "jobs": rows})

	@gl.public.view
	def get_jobs_by_freelancer(self, address: str) -> str:
		rows = self._jobs_for(self.freelancer_jobs.get(Address(str(address))))
		return json.dumps({"address": str(address), "count": len(rows), "jobs": rows})

	@gl.public.view
	def get_reputation(self, address: str) -> str:
		rep = self.freelancer_reputation.get(Address(str(address)))
		if rep is None:
			return json.dumps({"address": str(address), "completed_count": 0,
				"ai_win_count": 0, "ai_loss_count": 0, "total_earned": "0"})
		return json.dumps({"address": str(address),
			"completed_count": int(rep.completed_count),
			"ai_win_count": int(rep.ai_win_count),
			"ai_loss_count": int(rep.ai_loss_count),
			"total_earned": str(int(rep.total_earned))})

	@gl.public.view
	def has_reputation(self, address: str, min_completed: int) -> bool:
		"""Cheap bool for cross-contract gating, mirroring the composability
		surface a consumer contract (e.g. TalentGate) needs."""
		rep = self.freelancer_reputation.get(Address(str(address)))
		if rep is None:
			return int(min_completed) <= 0
		return int(rep.completed_count) >= int(min_completed)

	@gl.public.view
	def require_reputation(self, address: str, min_completed: int) -> str:
		if not self.has_reputation(str(address), int(min_completed)):
			raise gl.vm.UserError(str(address) + " has fewer than "
				+ str(int(min_completed)) + " completed milestones")
		return json.dumps({"ok": True, "address": str(address)})

	@gl.public.view
	def get_platform_stats(self) -> str:
		fees_available = int(self.platform_fees_accrued) - int(self.platform_fees_withdrawn)
		return json.dumps({"escrow_locked": str(int(self.escrow_locked)),
			"on_chain_balance": str(int(self.balance)),
			"platform_fees_accrued": str(int(self.platform_fees_accrued)),
			"platform_fees_withdrawn": str(int(self.platform_fees_withdrawn)),
			"platform_fees_available": str(fees_available),
			"total_released": str(int(self.total_released)),
			"total_refunded": str(int(self.total_refunded)),
			"total_bonds_forfeited": str(int(self.total_bonds_forfeited)),
			"jobs": int(self.count_jobs), "milestones": int(self.count_milestones),
			"ai_approved": int(self.count_ai_approved),
			"ai_rejected": int(self.count_ai_rejected),
			"ai_inconclusive": int(self.count_ai_inconclusive)})

	@gl.public.view
	def get_config(self) -> str:
		return json.dumps({"owner": str(self.owner), "paused": bool(self.paused),
			"platform_fee_bps": int(self.platform_fee_bps),
			"max_platform_fee_bps": MAX_PLATFORM_FEE_BPS,
			"dispute_bond": str(DISPUTE_BOND),
			"min_milestone_amount": str(MIN_MILESTONE_AMOUNT),
			"max_milestone_amount": str(MAX_MILESTONE_AMOUNT),
			"max_milestones_per_job": MAX_MILESTONES_PER_JOB,
			"submit_timeout_seconds": SUBMIT_TIMEOUT_SECONDS,
			"reclaim_timeout_seconds": RECLAIM_TIMEOUT_SECONDS})
