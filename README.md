# GenLayer Fact-Check Bounty Registry

Intelligent Contracts that escrow GEN against a checkable real-world claim, pay a
reporter only when their TRUE/FALSE report matches a **validator-derived** truth,
and refund the poster when nothing verifiable ever happened.

Contracts only — no frontend, no backend.

- Network: **GenLayer studionet**, chainId `61999`
- Runner: pinned by content hash on line 1 of both contract files
  (`py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6`)
- Live deployment:
  [`0x116DE8851D2101D583b84EBFEEadc9d2f10Ae012`](https://explorer-studio.genlayer.com/address/0x116DE8851D2101D583b84EBFEEadc9d2f10Ae012)
  — every transaction in this README is linked and independently verified in
  [EVIDENCE: live studionet run](#evidence-live-studionet-run) and
  [EVIDENCE: the integration suite's own transactions](#evidence-the-integration-suites-own-transactions).

---

## The claim type that was chosen

> **"Does the public GitHub repository `<owner>/<name>` have at least
> `<threshold>` stars?"**
> checked against `https://api.github.com/repos/<owner>/<name>`.

One claim type, chosen because it is:

- **publicly checkable** by any independent party with no API key;
- **binary** — the contract only ever needs `TRUE` / `FALSE`, never a number;
- **stable enough to reach consensus on** — `stargazers_count` drifts, but a
  threshold like `>= 10` on an established repository flips on a timescale of
  days, not seconds, so an independent validator fetch microseconds later
  derives the same verdict;
- **distinguishable between "false" and "unreachable"** — HTTP 404 is
  reproducible evidence that the claim cites a repository that does not exist,
  while a 403 rate-limit or a 5xx is a transient failure and must never become a
  verdict.

## Layout

```
contracts/
  BountyRegistry.py    factory + index contract (embeds the child source, see below)
  BountyClaim.py       one verification/escrow state machine per bounty
tests/
  direct/              51 Direct Mode tests (real pinned runner, in-process, no consensus)
  integration/         13 live studionet tests (real leader + 5 validators)
                       shared helpers live in direct_harness.py / netconfig.py,
                       not in conftest.py — see note 21 below
scripts/
  build_bundle.py            regenerates the embedded child source inside BountyRegistry.py
  e2e_studionet.py           drives a full bounty lifecycle against the deployed registry
  verify_explorer_evidence.py checks every tx/address against the public explorer API
  audit_payout_ledger.py     checks that every emitted value transfer actually credited
  collect_integration_evidence.py  re-derives the integration suite's txs from chain + explorer
evidence/              JSON output produced by the scripts above
.gitattributes         keeps the contract bytes byte-identical to the deployment
```

## Architecture: why a factory

`BountyRegistry.create_bounty(...)` **deploys a new `BountyClaim` contract for
every bounty** via `gl.deploy_contract(...)` and indexes it:

```python
child_address = gl.deploy_contract(
    code=bytes.fromhex(_CHILD_SOURCE_HEX),
    args=[identifier, repo_full_name, u256(int(threshold)), deadline_at,
          poster, reward, gl.message.contract_address],
    salt_nonce=self.next_nonce,
    value=reward,            # the locked GEN travels with the child deployment
    on="finalized",
)
self.bounty_index[identifier] = child_address   # TreeMap[str, Address]
self.bounty_ids.append(identifier)              # DynArray[str] for enumeration
```

Why not one contract holding every bounty:

- **Escrow isolation.** A child can only ever move the GEN sitting in its own
  balance. A shared contract holding N escrows has to keep accounting that can
  cross-contaminate them; here that class of bug is unrepresentable.
- **Verification isolation.** The expensive, appealable, non-deterministic
  verification state machine of one bounty can never interfere with another's.
- **The child address _is_ the receipt** for the bounty — publicly addressable,
  independently readable, and independently verifiable on the explorer.

The parent keeps the indexes and provides the roll-up. `aggregate_statuses()`
reads **every child synchronously** rather than mirroring their state:

```python
for identifier in self.bounty_ids:
    child = gl.get_contract_at(self.bounty_index[identifier])
    try:
        status = child.view().get_status()
    except Exception:
        by_status["UNREADABLE"] = ...   # reported, never hidden
```

so the roll-up is always the children's truth, and an unreadable child is an
explicit `UNREADABLE` bucket instead of a silently vanished bounty.

### Child source transport (a real constraint, not a style choice)

The single-file `py-genlayer` runner maps exactly one uploaded file to
`/contract.py`, so the documented `open("/contract/BountyClaim.py")` factory
idiom requires the `py-genlayer-multi` runner — which the deployed `genlayer`
CLI (0.39.2) cannot package. The child source is therefore embedded in
`BountyRegistry.py` as `_CHILD_SOURCE_HEX` and passed to `gl.deploy_contract` as
bytes. `scripts/build_bundle.py` regenerates that block from
`contracts/BountyClaim.py`, and the Direct Mode test
`test_embedded_child_source_still_matches_the_canonical_file` asserts the
embedded copy is byte-identical to the canonical file, so the two cannot drift.

**Run `py -3.12 scripts/build_bundle.py` after editing `BountyClaim.py`** (or
`--check` to fail when the block is stale — the Direct Mode test does the same on
every run).

Because the payload is the child file's **bytes**, line endings are part of the
deployed artifact: the bundle was built on Windows, so the on-chain child carries
CRLF. `.gitattributes` therefore opts `*.py` out of line-ending normalisation, so
that a checkout of this repository still reproduces the deployed bytes. The
upshot is a property rather than a disclaimer: `verify_explorer_evidence.py`
compares the source the explorer stores against the files in this tree, and they
match byte-for-byte — see
[The repository is the deployment](#the-repository-is-the-deployment).

## Equivalence principle: consensus on derived verdicts, never on raw pages

The non-deterministic work is a single function, executed _identically_ by the
leader and by every validator, each with its **own** network fetch:

```python
def _derive_fact(url, expected_repo, threshold) -> dict:
    response = gl.nondet.web.get(url, headers={"Accept": "application/vnd.github+json"})
    ...
    return {"reachable": ..., "repo_ok": ..., "met": ...}
```

It deliberately returns **three stable booleans and nothing else** — not the
body, not the star count, not a timestamp, not an etag:

| field       | meaning                                                           |
| ----------- | ----------------------------------------------------------------- |
| `reachable` | the source answered at all (403 / 5xx / exception ⇒ `False`)      |
| `repo_ok`   | the source describes the repository the claim cites (`full_name`) |
| `met`       | the stable field satisfied the asserted threshold                 |

and the comparison is _comparative_, over that derivation:

```python
def validator_fn(leaders_res: gl.vm.Result) -> bool:
    if not isinstance(leaders_res, gl.vm.Return):
        return False                      # leader blew up -> disagree, rotate
    try:
        mine = _derive_fact(url, repo, threshold)
    except Exception:
        return False                      # a validator fetch failure is a
                                          # disagreement, never a pass
    return _same_derivation(leaders_res.calldata, mine)

return gl.vm.run_nondet_unsafe(leader_fn, validator_fn)
```

Consequences of that shape:

- **Nobody's raw page fetch is trusted** — not the leader's (a validator that
  derives something different votes `False`), and not any single AI's (the
  verdict is a `bool` triple that five independent LLM-driven executions must
  reproduce from the same JSON field).
- **`strict_eq` is never applied to fetched text**, so ordinary page drift
  cannot break consensus; only a drift that flips a verdict can. Direct Mode
  test `test_volatile_drift_that_keeps_the_verdict_does_not_break_consensus`
  pins that behaviour.
- **Ambiguity terminates instead of looping.** `reachable == False` for the whole
  ring maps the bounty to `UNRESOLVED`, which is reclaimable by the poster after
  the deadline — no infinite appeal on a rate-limited endpoint.
- **The star count itself is never stored.** `evidence` is a bounded summary
  string such as
  `https://api.github.com/repos/genlayerlabs/skills stargazers_count >= 10`;
  `test_derived_fact_check_is_recorded_not_raw_page` asserts it contains no
  document body and stays under 200 characters.

## State machine

```
OPEN ──submit_report──▶ REPORTED ──verify──▶ PAID        (report == derived truth, escrow -> reporter)
   │                       │                   └─▶ REJECTED   (report != truth, escrow stays)
   │                       └─────────────────────▶ UNRESOLVED (source unreachable for the ring)
   └──────────── reclaim_after_timeout ───────────▶ REFUNDED  (poster gets the escrow back)
```

`REJECTED`, `UNRESOLVED` and never-reported `OPEN` bounties are all reclaimable
by the poster once `deadline_at` has passed. **A bounty nobody ever reported is
reclaimed with no AI verification at all** — `reclaim_after_timeout()` never
touches `_fact_check`, which is the whole point of requirement 7.

## Storage schema

GenLayer storage supports `TreeMap` / `DynArray` / primitives, **not** `Enum`;
money is `u256` atto-GEN, never `float`. Status is a plain `str`.

`BountyRegistry` (append-only, in slot order):

| slot            | type                    | notes                                                                        |
| --------------- | ----------------------- | ---------------------------------------------------------------------------- |
| `owner`         | `Address`               | set to the deployer in `__init__`, transferable via `set_owner`              |
| `bounty_index`  | `TreeMap[str, Address]` | bounty_id -> child contract address                                          |
| `bounty_ids`    | `DynArray[str]`         | insertion-ordered enumeration                                                |
| `next_nonce`    | `u256`                  | CREATE2 `salt_nonce`, so child addresses are unpredictable-but-deterministic |
| `total_created` | `u256`                  | monotonic counter, never decremented                                         |

`BountyClaim` — immutables fixed at factory deployment, then mutable state:

| slot               | type        | notes                                               |
| ------------------ | ----------- | --------------------------------------------------- |
| `bounty_id`        | `str`       | unique within the registry                          |
| `poster`           | `Address`   | who locked the reward, who may reclaim              |
| `repo_full_name`   | `str`       | normalised lower-case `owner/name`                  |
| `source_url`       | `str`       | derived in the constructor; never caller-controlled |
| `threshold`        | `u256`      | asserted minimum star count                         |
| `reward_atto`      | `u256`      | GEN actually attached to the creating call          |
| `deadline_at`      | `str`       | normalised `YYYY-MM-DDTHH:MM:SS` UTC                |
| `created_at`       | `str`       | from `gl.message_raw["datetime"]`                   |
| `registry`         | `Address`   | the parent that deployed this child                 |
| `status`           | `str`       | `OPEN/REPORTED/PAID/REJECTED/UNRESOLVED/REFUNDED`   |
| `reporter`         | `str`       | hex address, `""` until a report                    |
| `reported_verdict` | `str`       | `TRUE` / `FALSE`                                    |
| `truth_verdict`    | `str`       | validator-derived verdict                           |
| `last_fact_check`  | `FactCheck` | `@allow_storage @dataclass` of the three bools      |
| `evidence`         | `str`       | bounded human-readable summary                      |

Escrow is **not** a storage field: the payable balance the child holds _is_ the
escrow, read through `self.balance`. That removes the entire class of bugs where
a bookkeeping balance drifts from the real one.

Field ordering is append-only. `FactCheck`'s docstring states the rule: new
fields go at the **end** with a default, because inserting one mid-struct shifts
every subsequent storage slot and silently corrupts deployed state.

## Public API

`BountyRegistry`

| method                                                                        | kind                  | notes                                                                                      |
| ----------------------------------------------------------------------------- | --------------------- | ------------------------------------------------------------------------------------------ |
| `create_bounty(bounty_id, repo_full_name, threshold, deadline_at) -> Address` | `write` **`payable`** | locks `gl.message.value` as the reward, deploys and indexes the child, returns its address |
| `set_owner(new_owner)`                                                        | `write`               | owner-gated                                                                                |
| `list_bounties()`                                                             | `view`                | `{count, total_created, bounties[]}`                                                       |
| `get_bounty_address(bounty_id)`                                               | `view`                | rejects an unknown id instead of returning zero                                            |
| `get_bounty_status(bounty_id)`                                                | `view`                | delegates the read to the child                                                            |
| `aggregate_statuses()`                                                        | `view`                | synchronous cross-contract roll-up                                                         |

`BountyClaim`

| method                           | kind    | notes                                                                         |
| -------------------------------- | ------- | ----------------------------------------------------------------------------- |
| `submit_report(verdict) -> str`  | `write` | `TRUE`/`FALSE`, first report wins, **poster may not report their own bounty** |
| `verify() -> str`                | `write` | poster or reporter only; runs the consensus fact-check and settles            |
| `reclaim_after_timeout() -> str` | `write` | poster only, deadline-gated, single-use, never after `PAID`                   |
| `get_status()`                   | `view`  | full detail including live `escrow_atto`                                      |
| `get_evidence()`                 | `view`  | verification artefacts only, no money fields                                  |

Every sender check reads `gl.message.sender_address` and raises
`gl.vm.UserError` prefixed with `[EXPECTED]` — never a bare Python exception.

## Getting started

```powershell
npm install -g genlayer                       # CLI 0.39.2 used here
py -3.12 -m pip install genlayer-py genvm-linter gltest
```

`.env` (git-ignored, and checked that way before every commit):

```
GENLAYER_PRIVATE_KEY=0x...   # dedicated, throwaway studionet-only account from the faucet
```

The key is read from the environment by the scripts — it is never hardcoded in a
source file, never printed, never committed.

Lint both contracts (required after any edit):

```powershell
py -3.12 scripts/build_bundle.py                       # re-embed the child source
genvm-lint check contracts/BountyClaim.py
genvm-lint check contracts/BountyRegistry.py
```

## Testing

### Direct Mode — fast, in-process, no consensus

```powershell
py -3.12 -m pytest tests/direct -q            # 51 passed
```

Direct Mode runs the **real pinned runner** against the real contract source —
real `calldata` codec, real `TreeMap`/`DynArray`, real `run_nondet_unsafe`
boundary — but **only the leader path**. It cannot prove consensus:
`run_nondet_unsafe` calls `leader_fn()` and merely _captures_ `validator_fn`, so
`tests/direct` drives the captured validator by hand with a hand-made leader
result to assert agreement and each disagreement mode. **Direct Mode therefore
does not exercise leader/validator agreement itself — `tests/integration` does.**

Direct Mode also never enforces `payable` and never moves money: the VM's own
dispatcher answers neither `PostMessage` nor `EthSend`, so the harness installs a
`_gl_call_hook` that records the emission instead. Both limits are stated in the
tests rather than papered over.

### Integration — live studionet consensus

```powershell
py -3.12 -m pytest tests/integration -m integration -v -s
```

Excluded by default (`addopts = -m "not integration"` in `pytest.ini`) because
each test performs real transactions that a leader and five validators execute.
Nothing is mocked: the fixtures create real bounties, submit real reports and run
real `verify()` calls, then assert on the finalized receipts — vote breakdown,
`result_name`, the execution modes that actually ran, and the resulting on-chain
state including the recipients' GEN balances.

Point them at a specific registry with `GENLAYER_REGISTRY_ADDRESS` (defaults to
the deployment recorded above) and bound a round with
`GENLAYER_CONSENSUS_TIMEOUT` (default 1200 s).

Their output is verified independently too — see
[EVIDENCE: the integration suite's own transactions](#evidence-the-integration-suites-own-transactions).

## Deploying

```powershell
genlayer network set studionet
genlayer deploy --contract contracts/BountyRegistry.py
```

The CLI prints the deployment transaction hash and the contract address.
`BountyRegistry.__init__` takes no constructor arguments, so `--args` is unused.

Note that the CLI exposes **no way to attach value to a method call**, so
value-bearing writes (`create_bounty`) go through the Python SDK
(`client.write_contract(..., value=...)`) as `scripts/e2e_studionet.py` does.

Then exercise and verify a real lifecycle:

```powershell
py -3.12 scripts/e2e_studionet.py --registry 0x116DE8851D2101D583b84EBFEEadc9d2f10Ae012
py -3.12 scripts/verify_explorer_evidence.py --registry 0x116DE8851D2101D583b84EBFEEadc9d2f10Ae012
py -3.12 scripts/audit_payout_ledger.py
```

## EVIDENCE: live studionet run

Every hash below was fetched back from the **public explorer API**
(`https://explorer-studio.genlayer.com/api/...`) by
`scripts/verify_explorer_evidence.py`, which touches nothing but the explorer —
never the RPC that ran the consensus — and exits non-zero if any record is
missing or is not `FINALIZED`. Its machine-readable output is
[`evidence/explorer-verification.json`](evidence/explorer-verification.json); the
driver's own record is
[`evidence/studionet-e2e.json`](evidence/studionet-e2e.json).

### The repository is the deployment

Statuses alone cannot tell you that the contract someone reads on the explorer is
the contract in the tree, so the verifier also compares the source the explorer
stores against the local files — byte-for-byte:

```
  registry   CONTRACT tx_count=18 pin=1jb45aa8ynh2a9c9.. 0x116DE8851D2101D583b84EBFEEadc9d2f10Ae012
             deployed source vs BountyRegistry.py: bytes=True, modulo line endings=True
  child_A    CONTRACT tx_count=4  pin=1jb45aa8ynh2a9c9.. 0x2f9F9bF9EDB0054d3a45Da31aEE51DcE27cE0717
             deployed source vs BountyClaim.py: bytes=True, modulo line endings=True
  child_B    CONTRACT tx_count=3  pin=1jb45aa8ynh2a9c9.. 0x8f366497E7D5C14a144d0AE782850D568fF7DF15
             deployed source vs BountyClaim.py: bytes=True, modulo line endings=True
```

The same holds for all four integration-run children (`bytes=True` each). A
mismatch is a reported problem, not a silent pass.

This check earned its keep: it was added after an editor auto-reformat had
quietly changed `contracts/BountyClaim.py` relative to what was deployed, which
the drift test caught (`assert embedded == CLAIM_SOURCE.read_bytes()`) but which
no status field would ever have revealed. The files in this commit are the files
the chain has.

Actors

| role                                              | address                                                                                                                                 |
| ------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| `BountyRegistry` (deployed)                       | [`0x116DE8851D2101D583b84EBFEEadc9d2f10Ae012`](https://explorer-studio.genlayer.com/address/0x116DE8851D2101D583b84EBFEEadc9d2f10Ae012) |
| poster (pays, verifies, reclaims)                 | `0xE6E7a635EA247D8E8dA6126F7ccbDBb78fB63b4a`                                                                                            |
| reporter (independent EOA)                        | [`0xc5ae5Cc2D8198449981e09D1152Da68E0C9dE0Fc`](https://explorer-studio.genlayer.com/address/0xc5ae5Cc2D8198449981e09D1152Da68E0C9dE0Fc) |
| child `BountyClaim` A — `skills-ge-10-1790465224` | [`0x2f9F9bF9EDB0054d3a45Da31aEE51DcE27cE0717`](https://explorer-studio.genlayer.com/address/0x2f9F9bF9EDB0054d3a45Da31aEE51DcE27cE0717) |
| child `BountyClaim` B — `timeout-1790465224`      | [`0x8f366497E7D5C14a144d0AE782850D568fF7DF15`](https://explorer-studio.genlayer.com/address/0x8f366497E7D5C14a144d0AE782850D568fF7DF15) |

Registry deploy tx:
[`0x86c85f347cc12dd335c977becbac23b317093d58b176dfb9837b808fe69f9fd6`](https://explorer-studio.genlayer.com/tx/0x86c85f347cc12dd335c977becbac23b317093d58b176dfb9837b808fe69f9fd6)

Flow 1 — a correct report reaches consensus and gets paid

| step                                     | explorer status | value                 | tx                                                                                                                                  |
| ---------------------------------------- | --------------- | --------------------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| `create_bounty` locking 1 GEN            | `FINALIZED`     | `1000000000000000000` | [`0xf0e78acc…b76705ff`](https://explorer-studio.genlayer.com/tx/0xf0e78acc4b80ec496e225826e6686a7d7748be81320fa7b63612bf40b76705ff) |
| ↳ child deployed by the factory          | `FINALIZED`     | `1000000000000000000` | [`0x6a894140…5c8fbb8b`](https://explorer-studio.genlayer.com/tx/0x6a8941402e66509cf84bcc1470a4fe28869e195659f885899b317e425c8fbb8b) |
| `submit_report("TRUE")` by the reporter  | `FINALIZED`     | 0                     | [`0x00731b0d…aa0c4cbf`](https://explorer-studio.genlayer.com/tx/0x00731b0dcb209465dc1c77fd5e0ad2f04e85fb498685a60efe793c08aa0c4cbf) |
| `verify()` — leader + 5 validators fetch | `FINALIZED`     | 0                     | [`0xc6f3a247…5088a477`](https://explorer-studio.genlayer.com/tx/0xc6f3a2477ae9a84982c43c7d80f71b1fb74149f189ffc66543e04f7b5088a477) |
| ↳ payout emitted to the reporter         | `FINALIZED`     | `1000000000000000000` | [`0xec27b732…be90f06b`](https://explorer-studio.genlayer.com/tx/0xec27b732b060972fccf2b9ae8af3f15689f8cfde0ad0fc34b4bc69a4be90f06b) |

Settled state read back from the child: `status=PAID`, `truth_verdict=TRUE`,
`reported_verdict=TRUE`, `escrow_atto=0`,
`evidence="https://api.github.com/repos/genlayerlabs/skills stargazers_count >= 10"`.
Consensus: `MAJORITY_AGREE`, one round, votes `agree/agree/agree/idle/idle` from
five distinct validators, `result_name` recorded on the finalized receipt.

Flow 2 — an unreported bounty is reclaimed after its deadline, with no AI at all

| step                                                     | explorer status | value                | tx                                                                                                                                  |
| -------------------------------------------------------- | --------------- | -------------------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| `create_bounty`, deadline `2020-01-01T00:00:00`, 0.5 GEN | `FINALIZED`     | `500000000000000000` | [`0x8da1ea3f…399323e1`](https://explorer-studio.genlayer.com/tx/0x8da1ea3fcd3e947d8911124254357166da3b3b80161023a7ff2000a9399323e1) |
| ↳ child deployed by the factory                          | `FINALIZED`     | `500000000000000000` | [`0xeab3ff8b…25423a20`](https://explorer-studio.genlayer.com/tx/0xeab3ff8b7ee9ef458062b62d5e4fb90f1f088ed4502735010d9f8fce25423a20) |
| `reclaim_after_timeout()` by the poster                  | `FINALIZED`     | 0                    | [`0x2416f4c9…45151bb1`](https://explorer-studio.genlayer.com/tx/0x2416f4c9facf0cc92f61aa1e01f48f3573d8b802f2121320e4c7741345151bb1) |
| ↳ refund emitted to the poster                           | `FINALIZED`     | `500000000000000000` | [`0x256c8da0…79a896ac`](https://explorer-studio.genlayer.com/tx/0x256c8da0056af8b4a4f45fa2d1bd38225620a6e8c0cc90de7c10e93779a896ac) |

Settled state: `status=REFUNDED`, `escrow_atto=0`. No `verify()` was ever called
on this bounty, so no validator spent a single LLM call on it.

Parent reading children (synchronous roll-up, one view call)

```
aggregate_statuses()  -> by_status {OPEN: 1, PAID: 2, REFUNDED: 1}, escrow_total_atto 500000000000000000
list_bounties()       -> count 4, total_created 4
```

The `OPEN` entry and the extra `PAID` belong to an earlier partially-completed
run against the same registry (see _Re-running after an interrupted run_ in
`scripts/e2e_studionet.py`); the roll-up reports all four bounties because the
registry indexes every bounty ever created against it, not just this run's.

Ledger audit — `PAID` checked against money, not against a status field

`scripts/audit_payout_ledger.py` walks every emitted message to its own
transaction and re-reads the balances
([`evidence/payout-audit.json`](evidence/payout-audit.json)):

```
create_bounty_A          emits  1.000 GEN -> child A   FINALIZED / MAJORITY_AGREE  credited=True
verify_A                 emits  1.000 GEN -> reporter   FINALIZED                   credited=True
create_bounty_B          emits  0.500 GEN -> child B   FINALIZED / MAJORITY_AGREE  credited=True
reclaim_after_timeout_B  emits  0.500 GEN -> poster    FINALIZED                   credited=True

poster   52.5000 GEN     reporter  3.0000 GEN     child_A  0.0     child_B  0.0
```

Those balances reconcile exactly: the poster paid `1 + 1 + 0.5 + 0.5` GEN into
escrow across the runs and got `0.5` back; the reporter started the session with
`1.0` GEN and received `1.0` twice. Every contract's escrow is drained to zero
only through a transfer that the ledger confirms.

## EVIDENCE: the integration suite's own transactions

```
py -3.12 -m pytest tests/integration -m integration -v -s
======================= 13 passed in 601.22s (0:10:01) ========================
```

Console capture (excerpt): [`evidence/integration-pytest-run.txt`](evidence/integration-pytest-run.txt).

A suite that asserts on receipts fetched from the same RPC that ran the consensus
is only half-verified, and a console capture is not evidence at all. So the
bounties that run created are re-derived **from the deployed registry**
(`list_bounties`, ids prefixed `it-`) and every associated transaction is
re-fetched from the explorer:

```powershell
py -3.12 scripts/collect_integration_evidence.py
py -3.12 scripts/verify_explorer_evidence.py `
    --evidence evidence/integration-evidence.json --audit none `
    --registry 0x116DE8851D2101D583b84EBFEEadc9d2f10Ae012 `
    --out evidence/explorer-verification-integration.json
```

Result: **18 transactions, all `FINALIZED`; registry + 4 children, all
`type=CONTRACT`, each storing the content-hash runner pin** — exit code 0
([`evidence/explorer-verification-integration.json`](evidence/explorer-verification-integration.json)).
The four `create_bounty` hashes the collector found on-chain match the four the
suite printed, which is the cross-check that the run being verified is the run
that happened.

Bounty 1 — `it-465605650` — the two authorization guards. Child
[`0x412829AA…cA74B1dD`](https://explorer-studio.genlayer.com/address/0x412829AA72fd44851adE05C1D77d719AcA74B1dD),
still `OPEN` with its full `1 GEN` escrow:

| step                                                  | value                 | tx                                                                                                                                  |
| ----------------------------------------------------- | --------------------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| `create_bounty` (payable)                             | `1000000000000000000` | [`0x84a783a7…fa9fb190`](https://explorer-studio.genlayer.com/tx/0x84a783a7707b6c29012dedccc0ac1096557c13895afc4fa4b34111c2fa9fb190) |
| ↳ child deployed by the factory, funded               | `1000000000000000000` | [`0xe121fc19…b09b5b6b`](https://explorer-studio.genlayer.com/tx/0xe121fc19a924fd41e1a429bcc7a82c35a2875b89d96a5e6d4170ae66b09b5b6b) |
| `submit_report` **by the poster** — refused           | 0                     | [`0x76b69ad7…180b063b`](https://explorer-studio.genlayer.com/tx/0x76b69ad75628e7332ac35b414ad0f37cb0aa1d4b1c9a8c52b86d6cc7180b063b) |
| `reclaim_after_timeout` **by a non-poster** — refused | 0                     | [`0x60dc9f74…83bae5c2`](https://explorer-studio.genlayer.com/tx/0x60dc9f74f96af8191143a977ab46293cf43fb2faabee21f631e8673483bae5c2) |

Both guard transactions are `FINALIZED` — the _transaction_ succeeded, the _call_
reverted with `[EXPECTED] …`. That is exactly what it means for a sender check to
work, and the child's own state is the proof: no report was ever accepted, the
escrow never moved.

Bounty 2 — `it-465691969` — correct report, consensus-paid. Child
[`0x50F75080…3a7a9673`](https://explorer-studio.genlayer.com/address/0x50F75080da59f7a718545e84234fE0d83a7a9673),
`PAID`, `reported_verdict=TRUE`, `truth_verdict=TRUE`, escrow now `0`:

| step                                       | value                 | tx                                                                                                                                  |
| ------------------------------------------ | --------------------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| `create_bounty` (payable)                  | `1000000000000000000` | [`0x84be800b…c08a3d99`](https://explorer-studio.genlayer.com/tx/0x84be800b44b9475b7e6f7a216eef15a92f1f60d324b2f05834e04611c08a3d99) |
| ↳ child deployed by the factory, funded    | `1000000000000000000` | [`0x326c79a8…f1c19f1d`](https://explorer-studio.genlayer.com/tx/0x326c79a821cdf572898943319ab35b73558d057e2ee80ca47cd90fa4f1c19f1d) |
| `submit_report("TRUE")` by the reporter    | 0                     | [`0x0df2828c…b6c21c1b`](https://explorer-studio.genlayer.com/tx/0x0df2828cac578b77ae994b365150be8349584d29ede23ad48029ea56b6c21c1b) |
| `verify()` — leader + 5 validators fetched | 0                     | [`0x61149707…c1fd1466`](https://explorer-studio.genlayer.com/tx/0x61149707e1a41d6130c164edd026cda114c5b9aef67f12aa00f826f9c1fd1466) |
| ↳ payout emitted to the reporter           | `1000000000000000000` | [`0x1f04ac9a…3b43bb92`](https://explorer-studio.genlayer.com/tx/0x1f04ac9a30bc75b79d736bad10246cd7bfbe551ee187cddcc1a61e923b43bb92) |

`test_payout_moves_the_escrow` does not accept the `PAID` field as proof: it
reads the reporter's balance before and after and requires the difference to
equal the reward. That assertion is what caught the EOA-transfer bug documented
in item 5 below.

Bounty 3 — `it-465825012` — wrong report, rejected, nothing paid. Child
[`0xa48a4366…BFf00499`](https://explorer-studio.genlayer.com/address/0xa48a4366bDD4787dbEf2b6075e82E246BFf00499),
`REJECTED`, `reported_verdict=FALSE` against `truth_verdict=TRUE`:

| step                                    | value                 | tx                                                                                                                                  |
| --------------------------------------- | --------------------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| `create_bounty` (payable)               | `1000000000000000000` | [`0x53844747…a543f908`](https://explorer-studio.genlayer.com/tx/0x5384474726609e65a76bc4c5d9f6561916e1e1f27feb5b6dd9c7e860a543f908) |
| ↳ child deployed by the factory, funded | `1000000000000000000` | [`0x49d7e824…917ee698`](https://explorer-studio.genlayer.com/tx/0x49d7e824473cc4d055f2a6103d1e5e3fc5b1b8e9e0dab5eed99f2726917ee698) |
| `submit_report("FALSE")`                | 0                     | [`0xfb5defcb…24bd93a7`](https://explorer-studio.genlayer.com/tx/0xfb5defcb85b772e4a6bc4e93d95e5d73a9db3f23c4668b305bc83bdb24bd93a7) |
| `verify()`                              | 0                     | [`0xb3a28660…23e259e8`](https://explorer-studio.genlayer.com/tx/0xb3a28660db50afc17408c2cb599ae29e30e3c80958bb60823bf132fb23e259e8) |

The child's explorer balance is still `1000000000000000000` and it emitted **no**
value transaction — a rejected report is a settled state, not a payout with a
status label attached.

Bounty 4 — `it-465960907` — an absent repository yields a verdict, not a hang.
Child [`0xa36cd793…E01A1aBF`](https://explorer-studio.genlayer.com/address/0xa36cd793125f1856D538e0C2da6c22C1E01A1aBF),
claim `genlayerlabs/definitely-not-a-real-repo-9f3a1c >= 10` stars. A reproducible
HTTP 404 derives `repo_ok=False`, so the truth is `FALSE` and the reporter who
said `FALSE` is paid: `PAID`, `fact_check_repo_ok=false`, escrow `0`.

| step                                    | value                 | tx                                                                                                                                  |
| --------------------------------------- | --------------------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| `create_bounty` (payable)               | `1000000000000000000` | [`0xf1b98140…fe7c2789`](https://explorer-studio.genlayer.com/tx/0xf1b98140945a9e819246d2dc8c4a0b9b2d14a7346be4696d015f0cfdfe7c2789) |
| ↳ child deployed by the factory, funded | `1000000000000000000` | [`0x931cee17…085e078e`](https://explorer-studio.genlayer.com/tx/0x931cee172af69e80636a161120f44ac432a633271f2ddb949122c809085e078e) |
| `submit_report("FALSE")`                | 0                     | [`0xdd632f7c…bf069c8b`](https://explorer-studio.genlayer.com/tx/0xdd632f7c8c5f68324f29b758814a15467f81f0560123f49197723d9bbf069c8b) |
| `verify()`                              | 0                     | [`0x14b39c65…edcaa19d`](https://explorer-studio.genlayer.com/tx/0x14b39c653692d0cb6b23b260af579134509d369d793ed38325355f7fedcaa19d) |
| ↳ payout emitted to the reporter        | `1000000000000000000` | [`0x024f16be…d1b74c4f`](https://explorer-studio.genlayer.com/tx/0x024f16be68a2d086dada5c1a336a2bf4193a0bfcc31bc90f8c7cf428d1b74c4f) |

The `verify()` receipts for these bounties carry `result_name = MAJORITY_AGREE`
with `num_of_initial_validators = 5`; the suite asserts the vote breakdown rather
than trusting the call's return value
(`test_validators_actually_ran_and_agreed`).

## What the real installed SDK did differently from the docs

Documented here because every one of these cost a debugging cycle, and because
"the docs say X" was not sufficient evidence for any of them. The rule applied
throughout: **believe the pinned runner and the installed `genlayer_py`, then fix
the contract, then re-lint and re-run on-chain.**

Contract side (pinned `py-genlayer` runner, std lib `11rhn002yfajawsz7fai6mykznbxkxs6l91iskj5cm82c92qhy3v`):

1. **Message sender is `gl.message.sender_address`** — an `Address` object,
   compared via `.as_hex`. There is no `sender_account`.
2. **`@gl.public.write.payable` is real and enforced — by the VM, not by Direct
   Mode.** The runner rejects value on a non-payable write
   (`_genlayer_runner.py:92`), while Direct Mode ignores `__gl_payable__`
   entirely. A passing Direct Mode "payable test" proves nothing; only the
   finalized, value-carrying `create_bounty` above does.
3. **The error type is `gl.vm.UserError`**, not `gl.UserError`.
4. **There is no timestamp attribute.** The transaction time arrives as
   `gl.message_raw["datetime"]`, e.g. `"2026-09-26T20:06:34.271991Z"`. The
   contracts normalise its fixed-width 19-character prefix and compare it
   lexicographically, so no `datetime` parsing happens inside the consensus path.
5. **A value transfer to an EOA is not `gl.get_contract_at(eoa).emit_transfer(...)`.**
   That call produces an _internal_ IC→IC message; against an address holding no
   Intelligent Contract the ring settles it with
   `last_leader = "contract_not_found_handler"`, `result_name = "NO_MAJORITY"`,
   `value_credited = false`. The escrow left the child and **nobody was paid** —
   while the contract's own `status` said `PAID`. Sending GEN to an EOA is an
   _external_ message and must go through a declared
   `@gl.evm.contract_interface` recipient (which emits `EthSend` with empty
   calldata). This was caught by asserting on balances instead of on statuses;
   the whole history is preserved in
   [`evidence/studionet-e2e-pre-payout-fix.json`](evidence/studionet-e2e-pre-payout-fix.json)
   and
   [`evidence/payout-audit-pre-payout-fix.json`](evidence/payout-audit-pre-payout-fix.json).
   Note that external transfers report `NO_MAJORITY` with an empty leader _and_
   `value_credited = true`: validators do not re-execute a chain-layer credit, so
   `result_name` is not a proxy for "did the money move" — `value_credited` and
   the balance are.
6. **`open("/contract/Child.py")` in the factory example needs `py-genlayer-multi`**,
   which the deployed CLI cannot package — hence the embedded hex bundle
   described above.
7. **Storage has no `Enum`**, and `gl.message.value` is a `u256` in atto units.
8. **A factory child is not readable the instant its parent finalizes.** It is
   deployed `on="finalized"`, so its own deploy tx runs _after_ the parent's;
   reading it too early fails with `-32001 Contract ... not found`. Both the
   driver and the test fixtures poll until the child answers `get_status()`.
9. **Direct Mode's VM implements neither `PostMessage` nor `EthSend`** — the
   trace literally reads `Unknown gl_call request type`. Cross-contract calls
   only work if the test installs `vm._gl_call_hook`.

Client side (`genlayer-py` 0.16.3, `genlayer` CLI 0.39.2):

10. **The CLI has no way to attach value to a method call.** `genlayer account
send <to> <amount>` exits 0, prints nothing and moves no GEN. Value-bearing
    writes and plain transfers both need the Python SDK.
11. **`client.w3.eth.send_transaction` is unusable on studionet**: web3's gas
    middleware calls `eth_estimateGas`, which the provider rejects with
    `-32602 Too many parameters provided`. Funding is done with a locally signed
    raw transaction carrying explicit `gas`/`gasPrice` (studionet's gas price is
    `0`).
12. **`studionet.rpc_urls` is nested** (`{'default': {'http': [...]}}`), not the
    flat list the type hints suggest — a plain `["http"]` lookup raises `KeyError`.
13. **`generate_private_key()` returns `HexBytes`.** `str()` of it is a `b'...'`
    repr, which makes `create_account` die inside `binascii`.
14. **`TransactionStatus` is a str-keyed enum**, so the numeric `status: 7` the
    RPC returns cannot be coerced through it. `status_name` is the authoritative
    field and is what every assertion here uses.
15. **Several receipt fields come back as strings, not structures**
    (`consensus_data`, `messages`, `leader_receipt`, `validators`), so decoding
    is defensive throughout the scripts.
16. **The explorer's HTML pages are an empty SPA shell** — fetching
    `/tx/<hash>` over HTTP returns the page skeleton for any hash at all, known
    or not. Only `GET /api/transactions/<hash>` and `GET /api/address/<addr>`
    honestly report existence, so the evidence verifier uses the JSON API and
    treats a missing record as a failure rather than a blank page.
17. **Both hosted endpoints intermittently return a Cloudflare `502 Bad gateway`
    / `503 Service Unavailable` HTML page mid-poll.** On the RPC it surfaces as
    `eth_getTransactionReceipt returned invalid JSON`; on the explorer it surfaces
    as an `HTTP 503` for an address that is indexed a moment later. Re-running the
    driver is safe (documented in its docstring) and the explorer fetcher retries
    5xx before reporting a problem — a genuinely absent record still fails with
    `404` and is reported.
18. **`genvm-lint` crashes on a cp1252 console** while printing its checkmark
    (`UnicodeEncodeError`), which looks like a lint failure but is not. Set
    `PYTHONIOENCODING=utf-8`.
19. Tooling nits that cost time and are worth knowing up front: `gltest` 0.29.2's
    artifact URL 404s and its Direct Mode loader raises `WinError 32` on Windows
    because it `os.unlink`s a temp file still held on stdin (deferred in
    `tests/direct/conftest.py`); `pytest-timeout` is not installed, so
    `--timeout` is an unknown option.
20. **Line endings are part of the deployed artifact, and Python's default
    newline translation edits them silently.** The child source is embedded as
    _bytes_, and `Path.write_text(...)` on Windows turns every `\n` into `\r\n`
    unless you pass `newline="\n"` — so re-running the bundle generator can grow
    the factory's payload by one byte per line and produce a child that is **not**
    the one that was deployed, while every transaction status still reads
    `FINALIZED`. Nothing in the SDK warns you. Handling: `build_bundle.py` writes
    with explicit `\n`, `.gitattributes` opts the contract files out of git's
    line-ending normalisation, and `verify_explorer_evidence.py` reports the
    byte-exact and modulo-line-endings comparisons separately instead of picking
    one and hiding the other.
21. **`conftest.py` is not a safe place to put things tests import by name.**
    pytest registers a non-package `conftest.py` in `sys.modules` under exactly
    that bare name, so with two test directories (`tests/direct`,
    `tests/integration`) a `from conftest import ...` inside a test resolves to
    whichever conftest was collected _last_. Running `pytest -q` from the repo
    root therefore failed 4 direct-mode tests with `ImportError: cannot import
name 'CLAIM_SOURCE' from 'conftest' (... tests\integration\conftest.py)`,
    while `pytest tests/direct -q` passed 51/51 — a harness bug that only
    appeared in the exact command a reviewer is most likely to type. Fix: the
    shared constants and helpers live in `tests/direct/direct_harness.py` and
    `tests/integration/netconfig.py` (unique module names), and `conftest.py`
    keeps only fixtures and hooks, which pytest resolves per directory anyway.

## Requirement checklist

| #   | requirement                                                                                      | where it is proven                                                                                                                                                                                      |
| --- | ------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | pinned runner header, line 1, both files, lint-confirmed                                         | line 1 of both files; `test_both_contracts_pin_the_runner_by_hash_and_never_by_tag`; and read back **from the chain** by `verify_explorer_evidence.py` (stored source begins with the content-hash pin) |
| 2   | `TreeMap`/`DynArray` only, `u256` atto amounts, `str` status                                     | schema tables above; `test_neither_contract_uses_a_banned_pattern`                                                                                                                                      |
| 3   | equivalence principle on derived verdicts                                                        | `_derive_fact` / `validator_fn`; 8 Direct Mode tests incl. volatile-drift-tolerance and validator-fetch-failure; live `MAJORITY_AGREE` receipt on `verify()`                                            |
| 4   | real payable decorator, verified by lint **and** a value-bearing call                            | `FINALIZED`, `value=1000000000000000000`, `value_credited=True` on `create_bounty_A`, explorer-confirmed                                                                                                |
| 5   | `create_bounty` / `list_bounties` / `get_bounty_status`                                          | public API table + `test_registry_aggregates_by_reading_children`                                                                                                                                       |
| 6   | `submit_report` / `verify` / `reclaim_after_timeout` + views, real sender field, real error type | `BountyClaim` API table; `test_child_uses_the_real_message_sender_field`, authorization tests in both suites                                                                                            |
| 7   | timeout reclaim with no AI verification                                                          | Flow 2 above; `test_poster_reclaims_a_never_reported_bounty_without_any_verification` registers **no** web mock at all                                                                                  |
| 8   | append-only dataclass discipline                                                                 | `FactCheck` docstring + field order in both contracts                                                                                                                                                   |

## Honest limitations

- **Direct Mode proves leader-path semantics only.** Agreement, appeal and
  rotation behaviour is asserted live in `tests/integration`, not in Direct Mode.
- **`aggregate_statuses()` is O(bounties)** in synchronous cross-contract reads.
  Correct and cheap at the scale of a registry of fact-checks; it would need a
  paginated or incrementally-maintained variant at a much larger scale.
- **A reachable-but-lying source is out of scope.** The contract verifies the
  claim against the URL the poster itself chose and recorded on-chain; it cannot
  decide whether that URL is a trustworthy authority. That is a design boundary
  of a single-source claim type, not an oversight.
- **Thresholds near the current star count narrow the consensus window.** A
  bounty at `>= 30` on a repo sitting at 31 could legitimately flip mid-appeal;
  `test_validator_disagrees_when_the_derived_verdict_flips` pins the resulting
  behaviour (rotation, not silent acceptance).
- **The `UNRESOLVED` path depends on transient failures actually occurring.** It
  is exercised deterministically in Direct Mode (a 403/5xx mock settles
  `UNRESOLVED` and stays reclaimable); it was not manufactured on studionet,
  because forcing a GitHub rate limit is not reproducible evidence.
- **`total_created` never decreases** and orphan bounties from interrupted driver
  runs remain indexed. That is the append-only design working as intended, but it
  means `count` reflects every bounty ever created against a registry, not just
  the last run's.

## License

MIT — see [LICENSE](LICENSE).
