# GigEscrow — freelance milestone escrow with AI arbitration on GenLayer

An Intelligent Contract pair for GenLayer: a client funds work in milestones,
a freelancer delivers, and if the two sides disagree — or one side simply
goes silent — the network itself settles it. Validators independently fetch
the delivered page and ask an LLM whether it satisfies the agreed
specification, then reach consensus on a single verdict.

## Why AI arbitration instead of a threshold check

Most oracle-style Intelligent Contracts answer an objective question: did a
number cross a line. "Was this deliverable satisfactory" has no such
threshold — it is a judgment call. So the Equivalence Principle used here is
not a numeric comparison; it is a **non-strict LLM judgment with exactly one
compared field**. The leader fetches the deliverable, asks an LLM to produce
`{"verdict", "score", "reasoning"}`, and every validator independently
re-fetches and re-prompts. Only `verdict` (`APPROVED` / `REJECTED` /
`INCONCLUSIVE`) is compared between leader and validator — `score` and
`reasoning` are stored as evidence but never compared, because two
independent LLM calls will almost never produce identical wording even when
they agree completely on the outcome. Comparing the whole object would make
every arbitration land `UNDETERMINED` regardless of how obvious the
deliverable is.

A separate `_coherent` check validates the leader's own report against
itself (verdict is one of the three allowed values, score is an integer
0–100, reasoning is under the length cap) — a pure function of the leader's
own calldata, so every validator computes the identical answer from it and
it can reject a malformed report without ever itself being a source of
disagreement.

## Two contracts

- **`contracts/GigEscrow.py`** — the escrow and arbitrator. A client creates
  a job and funds milestones with real GEN. The freelancer submits a
  deliverable (a public URL) per milestone. The client can approve directly
  (fast path, no AI involved) or reject; whichever side disagrees with the
  outcome — or whose counterpart has gone silent — can escalate to on-chain
  arbitration via `dispute_milestone`.

- **`contracts/TalentGate.py`** — a composability example. It gates hiring
  roles on a minimum completed-milestone count, read **live** from
  `GigEscrow` on every call via a synchronous IC-to-IC `view()` call
  (`gl.get_contract_at(oracle).view().has_reputation(...)`). Nothing is
  cached, so nothing needs to be revoked or expired to stay accurate.

Both pass `genvm-lint check` — AST-level safety checks plus full semantic
validation against the real GenVM SDK (v0.2.16):

```
contracts/GigEscrow.py   -> Lint passed (3 checks), Validation passed  -- 24 methods (10 view, 14 write)
contracts/TalentGate.py  -> Lint passed (3 checks), Validation passed  -- 10 methods (5 view, 5 write)
```

A structural test (`test/test_no_raise_in_payable.py`) also confirms that
neither `@gl.public.write.payable` method (`fund_milestone`,
`dispute_milestone`) contains a `raise` anywhere in its body — the critical
money-safety rule for GenVM: `gl.vm.UserError` rolls back contract *storage*
but does **not** return the value that rode in with the call, so a payable
method that raises on a rejection would strand the sender's GEN in the
contract, unaccounted for. Every rejection instead refunds the sender and
returns a successful `{"ok": false, "reason": ..., "refunded": ...}` —
callers must read `ok`.

## Two-sided protection against a stuck counterparty

- A client who goes silent after a submission does not freeze the
  freelancer's pay forever: after `SUBMIT_TIMEOUT_SECONDS` the freelancer may
  escalate to arbitration unilaterally.
- A freelancer who never delivers does not freeze the client's escrow
  forever: after `RECLAIM_TIMEOUT_SECONDS`, anyone may permissionlessly
  return the escrowed amount to the client — permissionless on purpose, since
  a release path only one party can trigger is not a guarantee.
- `set_paused` only gates `fund_milestone` (new money entering). Submission,
  approval, rejection, arbitration, cancellation and reclaiming all keep
  working while paused — an owner who could freeze settlement of money
  already escrowed would have the same leverage as one who could deny a
  payout outright, just through a slower route.

## Money model

There is no shared risk pool and no payout multiplier. Every wei escrowed
for a milestone is either paid to the freelancer or refunded to the client,
minus a platform fee withheld once, up front, at funding time
(`platform_fee_bps`, capped at `MAX_PLATFORM_FEE_BPS`). Because a milestone
never promises more than it holds, the contract never has to run a solvency
check the way a pooled-risk product would.

Dispute bonds (`DISPUTE_BOND`) are required to open arbitration and are
returned to whoever filed it in every outcome **except** one: a freelancer
who disputes a client's `REJECTED` verdict and loses (the AI also says
`REJECTED`) forfeits the bond to the client, as compensation for a failed
challenge to a decision the client had already made. Requesting a neutral
ruling on an undecided `SUBMITTED` milestone is never treated as a bet
against anyone, so it is never subject to forfeiture — the bond simply comes
back regardless of the verdict.

## Milestone lifecycle

```
                    fund_milestone (payable)
                            |
                        PENDING ---- cancel_milestone ----> CANCELED   (client, pre-submission, full refund)
                            |
                            |   freelancer never submits, RECLAIM_TIMEOUT_SECONDS elapses:
                            |   reclaim_milestone (permissionless) ----> RECLAIMED
                            v
                    submit_milestone
                            |
                        SUBMITTED ---- approve_milestone ----> APPROVED      (fast path, no AI)
                            |
                            +---- reject_milestone ----> REJECTED
                            |                                |
                            |                                +-- dispute_milestone (freelancer) --+
                            |                                                                       |
                            +---- dispute_milestone (either side on SUBMITTED,                       |
                                   or freelancer after client silence) -------------------------------+
                                                                                                       v
                                                                                        on-chain arbitration (LLM + web)
                                                                                     +----------+----------+--------------+
                                                                                     v          v          v
                                                                              RESOLVED_    RESOLVED_    unchanged status
                                                                              APPROVED     REJECTED     (INCONCLUSIVE,
                                                                              freelancer   client       bond returned,
                                                                              paid         refunded     retryable later)
```

## Testing

`test/test_gigescrow.py` is an offline, dependency-free test suite (stdlib
only -- no chain, no network, no `genlayer` package required) built around a
minimal in-memory storage stub. It exercises the pure logic directly:
constants and bounds, the JSON-cleanup helper for LLM output, the
`_coherent` self-consistency check across malformed and well-formed reports,
fee-splitting arithmetic, and every branch of the arbitration bond-resolution
rule (`APPROVED` / `REJECTED` challenging a rejection / `REJECTED` on a fresh
submission / `INCONCLUSIVE`).

`test/test_no_raise_in_payable.py` is a structural test: it parses both
contract files with Python's `ast` module and asserts that no
`@gl.public.write.payable` method contains a `raise` statement anywhere in
its body -- the money-safety invariant described above, enforced
mechanically rather than by review.

Run everything with:

```bash
python3 -m unittest discover -s test -v
```

## Deploying to Studionet

```bash
npm install -g genlayer
genlayer network set studionet

# 1) Deploy GigEscrow -- constructor arg: platform_fee_bps (250 = 2.5%)
genlayer deploy --contract contracts/GigEscrow.py --args 250
# note the deployed address, e.g. 0xGIG...

# 2) Deploy TalentGate -- constructor arg: the GigEscrow address as oracle
genlayer deploy --contract contracts/TalentGate.py --args 0xGIG...
```

Or through [studio.genlayer.com](https://studio.genlayer.com) directly (no
install needed, has a built-in faucet): upload the file under Contracts,
supply the constructor argument in Run and Debug, and deploy.

### Interacting afterwards

```bash
genlayer call 0xGIG... get_config
genlayer write 0xGIG... create_job --args "0xFREELANCER..." "Landing page redesign"
genlayer call 0xGIG... get_job --args 1
genlayer call 0xGIG... get_platform_stats
genlayer call 0xTALENT... get_tier_thresholds
```

Payable methods (`fund_milestone`, `dispute_milestone`) need a GEN value
attached to the call, which the `genlayer write` CLI command does not
support (`--fee-value` only covers the fee deposit, not `--value`) -- those
calls need to be signed with `genlayer-js` instead.

## Files

| | |
|---|---|
| `contracts/GigEscrow.py` | the escrow contract -- milestone funding, submission, approval, and AI arbitration |
| `contracts/TalentGate.py` | the composable consumer -- a live reputation gate for hiring |
| `test/test_gigescrow.py` | offline unit tests for the pure logic and bond-resolution rules |
| `test/test_no_raise_in_payable.py` | structural test enforcing the payable-never-raises invariant |
