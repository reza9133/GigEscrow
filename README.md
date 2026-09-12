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

## Production hardening

Three changes that only matter once real, occasionally-unreliable
infrastructure and real-scale usage are involved -- not correctness fixes to
what was there before, but the gap between a working demo and a contract
built to run unattended on a live network:

**Dynamic deliverables.** `_judge` renders with
`wait_after_loaded="3s"` (`RENDER_WAIT_AFTER_LOADED`). A deliverable is
frequently a client-rendered app (React/Vue/Next.js), not a static page --
without a render delay, GenVM can capture the page before its own scripts
have painted anything, and every validator would end up judging an empty
shell rather than the actual deliverable.

**Transient infrastructure failures no longer masquerade as a verdict.**
`_judge` now raises a `gl.vm.UserError`, tagged `[TRANSIENT_FETCH]`,
`[TRANSIENT_LLM]`, or `[LLM_MALFORMED]`, for anything that looks like
infrastructure trouble -- the fetch itself failing, the LLM call itself
failing, or the LLM responding with something that will not parse as the
requested JSON shape -- instead of quietly returning an `INCONCLUSIVE`
verdict for all of them. `INCONCLUSIVE` is now reserved for the one case
that is actually a fact about the deliverable: the page loaded without error
and had nothing readable on it. `dispute_milestone` and `preview_dispute`
catch the raised error and respond with `{"ok": true, "verdict":
"RETRY_LATER", ...}` -- the bond is returned, the milestone's status and
`ai_verdict` are left exactly as they were, and nothing about the failure is
written to the permanent record. `_adjudicate`'s `validator_fn` also
classifies these the same way a validator running independently would, so
that two nodes hitting the same class of outage agree with each other
rather than needlessly burning a leader-rotation round on an outage that
will likely recur immediately with the next leader too.

**O(1) job completion.** `_mark_settled` replaces the old
`_maybe_complete_job`, which re-scanned every milestone on a job each time
any single one of them settled -- harmless at one milestone, wasteful at
`MAX_MILESTONES_PER_JOB` (30), where every approval, dispute resolution,
cancellation, or reclaim on that job would re-read the whole set. A job now
carries `settled_milestone_count`, incremented exactly once per milestone at
the moment it reaches a terminal status; comparing it to `milestone_count`
is equivalent to the old scan without ever performing it. An `INCONCLUSIVE`
or `RETRY_LATER` outcome deliberately does not advance the counter, since
neither one is a terminal settlement.

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

`GigEscrow.py` gained a new storage field (`settled_milestone_count` on
`Job`) as part of the production-hardening changes above, so an address
already deployed from an earlier version of this file is running the old
storage layout and logic -- editing the source does not change what is
already on-chain. A new version needs a fresh deployment (a new address),
not an in-place upgrade. If a `TalentGate` was deployed pointing at the old
`GigEscrow` address, point it at the new one with `set_oracle` (owner-only)
instead of redeploying it -- `TalentGate.py` itself did not change.

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

## Live deployment (Studionet)

| Contract | Address |
|---|---|
| `GigEscrow` | `0x17f4999A6bDA76456D3D136C4F4675Df3840388d` |
| `TalentGate` | `0x89ad52F5235118329e987B549c6965eD2A76BEB9` |

I have no network access to Studionet from the environment these contracts
were written in, so these two addresses are unverified on my end -- they are
exactly what was reported after deployment. Sanity-check them yourself with:

```bash
node deploy/interact.mjs config
node deploy/interact.mjs stats
node deploy/interact.mjs tiers
```

### `deploy/interact.mjs`

A small command-line client wired to the two addresses above, built because
the `genlayer` CLI's `write` command has no `--value` option -- it cannot
sign `fund_milestone` or `dispute_milestone`, the two calls that actually
move GEN. Every field and return shape it relies on (`writeContract`'s
`value: bigint` parameter, `waitForTransactionReceipt`'s `txExecutionResultName`
and `statusName`, `createAccount`'s optional private-key argument, the
`studionet` chain export) was checked directly against the installed
`genlayer-js` package's type declarations rather than assumed.

```bash
cd deploy
npm install
export PRIVATE_KEY=0x...   # the funded wallet you deployed with

node interact.mjs                       # lists every command
node interact.mjs config
node interact.mjs create-job 0xFREELANCER... "Landing page redesign"
node interact.mjs jobs-by-client $(node interact.mjs whoami | tail -1)
node interact.mjs fund-milestone 1 "Ship a responsive landing page" 0.05
node interact.mjs accept-job 1          # freelancer's wallet
node interact.mjs submit-milestone 1 0 https://example.com/deliverable "done"
node interact.mjs approve-milestone 1 0
node interact.mjs dispute-milestone 1 0 0.01
node interact.mjs eligible 0xFREELANCER... SENIOR
```

Every write command is followed automatically by a read of the resulting
state (e.g. `fund-milestone` prints the job's milestone list right after),
since a write transaction's own receipt exposes what went *in*
(`function_name`/`function_args`) rather than what the contract method
*returned* -- reading state back with a separate view call afterward is the
documented pattern, not a workaround.

## Files

| | |
|---|---|
| `contracts/GigEscrow.py` | the escrow contract -- milestone funding, submission, approval, and AI arbitration |
| `contracts/TalentGate.py` | the composable consumer -- a live reputation gate for hiring |
| `deploy/interact.mjs` | genlayer-js command-line client wired to the live Studionet addresses |
| `test/test_gigescrow.py` | offline unit tests for the pure logic and bond-resolution rules |
| `test/test_no_raise_in_payable.py` | structural test enforcing the payable-never-raises invariant |
