# GenLayer Fact-Check Bounty Registry

Intelligent Contracts that escrow GEN against a checkable real-world claim, pay a
reporter only when their TRUE/FALSE report matches a **validator-derived** truth,
and refund the poster when nothing verifiable ever happened.

Contracts only — no frontend, no backend.

- Network: **GenLayer studionet**, chainId `61999`
- Runner: pinned by content hash on line 1 of both contract files
  (`py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6`)
- Live deployment (corrected resubmit):
  [`0x07B36f7A9CfE15eF8baFfCd66dA951E1d3125dA0`](https://explorer-studio.genlayer.com/address/0x07B36f7A9CfE15eF8baFfCd66dA951E1d3125dA0)
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
  direct/              76 Direct Mode tests (real pinned runner, in-process, no consensus)
  integration/         18 live studionet tests (real leader + 5 validators)
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
deployed artifact. The corrected contracts were bundled and redeployed from
Linux, so both `BountyRegistry.py` and the embedded child now carry LF. The
deployed source is uploaded verbatim, and `.gitattributes` opts `*.py` out of
line-ending normalisation so a checkout of this repository still reproduces those
exact bytes. The upshot is a property rather than a disclaimer:
`verify_explorer_evidence.py` compares the source the explorer stores against the
files in this tree, and they match byte-for-byte — see
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

A bounty is only opened once `create_bounty` has validated its inputs — a
well-formed `owner/name` repository, a strictly positive `threshold`, and a
`deadline_at` that normalises to a well-formed UTC stamp **strictly after the
chain's current time**. Any failure raises `[EXPECTED]` and reverts *before* the
finalized child deployment is scheduled, so a malformed claim can never spend a
deploy.

```
                       (on-time report only: now <= deadline_at)
OPEN ─────────────────────────────────────────────▶ REPORTED ──verify──▶ PAID     (report == derived truth, escrow -> reporter)
   │  ▲                                                │                   └─▶ REJECTED   (report != truth, escrow stays)
   │  │ (late report refused: now > deadline_at)        └─────────────────────▶ UNRESOLVED (source unreachable for the ring)
   │  └─────────────────────────────────...
   │
   └──────────── reclaim_after_timeout (from OPEN / REJECTED / UNRESOLVED only, after the deadline) ───────────▶ REFUNDED
```

Two time gates and one state gate now define the lifecycle:

- **Reports are deadline-gated.** `submit_report` reads the chain timestamp and
  refuses (`now > deadline_at`) any report filed after expiry, so a late claim
  never becomes state and verification is never scheduled for it. A report in
  the final second (`now == deadline_at`) is still accepted.
- **Reclaim is state-gated against `REPORTED`.** `reclaim_after_timeout()`
  succeeds only from `OPEN` (never reported), or from the terminal `REJECTED` /
  `UNRESOLVED` states a verification has already reached — **never** from
  `REPORTED`. A timely report awaiting consensus cannot be undercut by a refund;
  the poster who wants their GEN back triggers `verify()` themselves to settle
  the pending report first, and only then may reclaim.

`REJECTED`, `UNRESOLVED` and never-reported `OPEN` bounties are all reclaimable
by the poster once `deadline_at` has passed. **A bounty nobody ever reported is
reclaimed with no AI verification at all** — `reclaim_after_timeout()` never
touches `_fact_check`, which is the whole point of requirement 7. Verification
itself is deliberately *not* deadline-gated, so the poster's way to unlock a
blocked reclaim (settle the pending report) always works.

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
| `create_bounty(bounty_id, repo_full_name, threshold, deadline_at) -> Address` | `write` **`payable`** | **validates** repo shape / positive threshold / strictly-future well-formed deadline, then locks `gl.message.value`, deploys and indexes the child, returns its address |
| `set_owner(new_owner)`                                                        | `write`               | owner-gated                                                                                |
| `list_bounties()`                                                             | `view`                | `{count, total_created, bounties[]}`                                                       |
| `get_bounty_address(bounty_id)`                                               | `view`                | rejects an unknown id instead of returning zero                                            |
| `get_bounty_status(bounty_id)`                                                | `view`                | delegates the read to the child                                                            |
| `aggregate_statuses()`                                                        | `view`                | synchronous cross-contract roll-up                                                         |

`BountyClaim`

| method                           | kind    | notes                                                                         |
| -------------------------------- | ------- | ----------------------------------------------------------------------------- |
| `submit_report(verdict) -> str`  | `write` | `TRUE`/`FALSE`, first **on-time** report wins (refused once `deadline_at` has passed), **poster may not report their own bounty** |
| `verify() -> str`                | `write` | poster or reporter only; runs the consensus fact-check and settles; **not** deadline-gated so a pending report can always be settled |
| `reclaim_after_timeout() -> str` | `write` | poster only, deadline-gated, single-use; allowed from `OPEN`/`REJECTED`/`UNRESOLVED` **but never `REPORTED`** (a pending report blocks the refund until verified) |
| `get_status()`                   | `view`  | full detail including live `escrow_atto`                                      |
| `get_evidence()`                 | `view`  | verification artefacts only, no money fields                                  |

Every sender check reads `gl.message.sender_address` and raises
`gl.vm.UserError` prefixed with `[EXPECTED]` — never a bare Python exception.

## Getting started

```powershell
npm install -g genlayer                       # CLI 0.39.2 used here
py -3.12 -m pip install genlayer-py==0.16.3 genlayer-test==0.29.2 genvm-linter==0.11.0
```

The Direct Mode harness is the PyPI package **`genlayer-test`** (it imports as
`gltest`); there is no `gltest` distribution to install.

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
py -3.12 -m pytest tests/direct -q            # 76 passed
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

The CLI decrypts the active account's keystore with an interactive passphrase.
For this corrected resubmit the redeploy was driven through the SDK primitive the
CLI itself wraps -- `genlayer_py.deploy_contract(code=<BountyRegistry.py text>)`
-- via `scripts/deploy_registry.py`, reading the raw key from the git-ignored
`.env`. The uploaded source is verbatim, so the on-chain artifact is
byte-for-byte this file; the explorer verification below confirms it.

Note that the CLI exposes **no way to attach value to a method call**, so
value-bearing writes (`create_bounty`) go through the Python SDK
(`client.write_contract(..., value=...)`) as `scripts/e2e_studionet.py` does.

Then exercise and verify a real lifecycle against the new registry:

```powershell
py -3.12 scripts/e2e_studionet.py --registry 0x07B36f7A9CfE15eF8baFfCd66dA951E1d3125dA0
py -3.12 scripts/audit_payout_ledger.py
py -3.12 scripts/verify_explorer_evidence.py --registry 0x07B36f7A9CfE15eF8baFfCd66dA951E1d3125dA0
py -3.12 scripts/collect_integration_evidence.py --registry 0x07B36f7A9CfE15eF8baFfCd66dA951E1d3125dA0 --out evidence/integration-evidence.json
py -3.12 scripts/verify_explorer_evidence.py --evidence evidence/integration-evidence.json --registry 0x07B36f7A9CfE15eF8baFfCd66dA951E1d3125dA0 --audit none --out evidence/explorer-verification-integration.json
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
  registry   CONTRACT tx_count=20 pin=1jb45aa8ynh2a9c9.. 0x07B36f7A9CfE15eF8baFfCd66dA951E1d3125dA0
             deployed source vs BountyRegistry.py: bytes=True, modulo line endings=True
  child_A    CONTRACT tx_count=4  pin=1jb45aa8ynh2a9c9.. 0x27d88ae26A0D2A36bf19613e5C3F6b09478FF08e
             deployed source vs BountyClaim.py: bytes=True, modulo line endings=True
  child_B    CONTRACT tx_count=4  pin=1jb45aa8ynh2a9c9.. 0xEf76F49C20926a79cceB725c33AD5184057cC336
             deployed source vs BountyClaim.py: bytes=True, modulo line endings=True
```

The same holds for all five integration-run children (`bytes=True` each). A
mismatch is a reported problem, not a silent pass. Because both contracts were
redeployed from Linux this time, the on-chain bytes carry LF and still match the
tree exactly -- the line-ending story is now identical for parent and children.

This check earned its keep: it was added after an editor auto-reformat had
quietly changed `contracts/BountyClaim.py` relative to what was deployed, which
the drift test caught (`assert embedded == CLAIM_SOURCE.read_bytes()`) but which
no status field would ever have revealed. The files in this commit are the files
the chain has.

Actors

| role                                              | address                                                                                                                                 |
| ------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| `BountyRegistry` (deployed)                       | [`0x07B36f7A9CfE15eF8baFfCd66dA951E1d3125dA0`](https://explorer-studio.genlayer.com/address/0x07B36f7A9CfE15eF8baFfCd66dA951E1d3125dA0) |
| poster (pays, verifies, reclaims; SDK deployer)   | `0x3de43AA2f7162c80af98abe78222aE0Cdf83c506`                                                                                            |
| reporter (independent EOA)                        | [`0xc8F1305Fa9E86ff8Fab8affA2133356290Ae69a6`](https://explorer-studio.genlayer.com/address/0xc8F1305Fa9E86ff8Fab8affA2133356290Ae69a6) |
| child `BountyClaim` A — `skills-ge-10-1790529196` | [`0x27d88ae26A0D2A36bf19613e5C3F6b09478FF08e`](https://explorer-studio.genlayer.com/address/0x27d88ae26A0D2A36bf19613e5C3F6b09478FF08e) |
| child `BountyClaim` B — `timeout-1790529196`      | [`0xEf76F49C20926a79cceB725c33AD5184057cC336`](https://explorer-studio.genlayer.com/address/0xEf76F49C20926a79cceB725c33AD5184057cC336) |

Registry deploy tx:
[`0x6307f71d0f4072dfa4e783d46c8daea18750e68dd5dee950fd20fab489940e33`](https://explorer-studio.genlayer.com/tx/0x6307f71d0f4072dfa4e783d46c8daea18750e68dd5dee950fd20fab489940e33)

Flow 1 — a valid bounty passes the new input gate, is reported, and gets paid

| step                                     | explorer status | value                 | tx                                                                                                                                  |
| ---------------------------------------- | --------------- | --------------------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| `create_bounty` locking 1 GEN            | `FINALIZED`     | `1000000000000000000` | [`0xe82381e3…599cc3c0`](https://explorer-studio.genlayer.com/tx/0xe82381e3261f7efb9a260870dd122a0068c53f52632e1eca281b9c3c599cc3c0) |
| ↳ child deployed by the factory          | `FINALIZED`     | `1000000000000000000` | [`0x6aa3e1eb…fc8e0b5b`](https://explorer-studio.genlayer.com/tx/0x6aa3e1ebf5e6626635ef080705c6456ef19186a397cedf15f429a1f0fc8e0b5b) |
| `submit_report("TRUE")` by the reporter  | `FINALIZED`     | 0                     | [`0xa5b8a6c6…5cc41dbf`](https://explorer-studio.genlayer.com/tx/0xa5b8a6c6e8f3b7082a6898cda4d68fa2d612ba5b3d32370c77796d8e5cc41dbf) |
| `verify()` — leader + 5 validators fetch | `FINALIZED`     | 0                     | [`0x40076a3d…b699f303`](https://explorer-studio.genlayer.com/tx/0x40076a3db17c3e0dbe99f71df93d322daf8e57f760ce765c4b421dedb699f303) |
| ↳ payout emitted to the reporter         | `FINALIZED`     | `1000000000000000000` | [`0xbfa8ca0f…37349c50`](https://explorer-studio.genlayer.com/tx/0xbfa8ca0f29e19db47c11d6609acbb3f3d5e9df2547bd42c1fc0b25ad37349c50) |

Settled state read back from the child: `status=PAID`, `truth_verdict=TRUE`,
`reported_verdict=TRUE`, `escrow_atto=0`,
`evidence="https://api.github.com/repos/genlayerlabs/skills stargazers_count >= 10"`.
Consensus: `MAJORITY_AGREE`, votes `agree/agree/agree/idle/idle` from five
distinct validators. `scripts/audit_payout_ledger.py` follows the emitted payout
to its own transaction and confirms the reporter's ledger balance rose a full
1 GEN (`value_credited=True`; 5.0 → 6.0 GEN across the run).

Flow 2 — an expired bounty refuses a late report, then reclaims with no AI at all

The timeout bounty is now created with a **near-future** deadline
(`2026-09-27T17:17:53`) because `create_bounty` rejects a non-future one; the
driver waits for it to pass, attempts a late report, and only then reclaims.

| step                                                              | explorer status | value                | tx                                                                                                                                  |
| ----------------------------------------------------------------- | --------------- | -------------------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| `create_bounty`, future deadline `2026-09-27T17:17:53`, 0.5 GEN    | `FINALIZED`     | `500000000000000000` | [`0x3e003ebb…e949bdfd`](https://explorer-studio.genlayer.com/tx/0x3e003ebb8e65f376659363631c0f97a2fe5fc6859af196479107fec6e949bdfd) |
| ↳ child deployed by the factory                                    | `FINALIZED`     | `500000000000000000` | [`0xce0f4aa5…1381b3bb`](https://explorer-studio.genlayer.com/tx/0xce0f4aa5add57c4c67dd05343aa1dc432d927eb3fcf9f32ffa11189d1381b3bb) |
| `submit_report("TRUE")` **after** the deadline — refused, stays `OPEN` | `FINALIZED`     | 0                    | [`0xc77a77b2…f54c9d80`](https://explorer-studio.genlayer.com/tx/0xc77a77b233c070e03b0e6ce0c42c655aa524e42429c9a934200a0884f54c9d80) |
| `reclaim_after_timeout()` by the poster                            | `FINALIZED`     | 0                    | [`0x0290f3e1…1518bdee`](https://explorer-studio.genlayer.com/tx/0x0290f3e1fd5134bf58d18a629d7c1f28536f55c346b8973f132b96121518bdee) |
| ↳ refund emitted to the poster                                      | `FINALIZED`     | `500000000000000000` | [`0x3d6430f5…07aa58dbc`](https://explorer-studio.genlayer.com/tx/0x3d6430f51693dea19a3e557dca02972e1776c9cc19626da695ec90b07aa58dbc) |

The post-expiry report finalized as a **contract-level rejection**: the bounty
was still `OPEN` when re-read, so the late verdict became no state and scheduled
no verification — and the poster's reclaim then settled to `REFUNDED`
(`escrow_atto=0`) exactly as if no report had ever arrived. No `verify()` ran on
this bounty, so no validator spent an LLM call on it.

Parent reading children (synchronous roll-up, one view call)

```
aggregate_statuses()  -> by_status {OPEN: 1, PAID: 3, REPORTED: 1, REJECTED: 1, REFUNDED: 1}, escrow_total_atto 3000000000000000000
list_bounties()       -> count 7, total_created 7
```

The registry indexes every bounty ever created against it, so the roll-up counts
this E2E run's two children plus the five the live integration suite left behind —
including a `REPORTED` child (`0x6736f2B2…`) that a poster `reclaim_after_timeout`
attempt is recorded against yet never left `REPORTED` (see below), which is the
race guard working on-chain.

Ledger audit — `PAID` checked against money, not against a status field

`scripts/audit_payout_ledger.py` walks every emitted message to its own
transaction and re-reads the balances
([`evidence/payout-audit.json`](evidence/payout-audit.json)):

```
create_bounty_A          emits  1.000 GEN -> child A   FINALIZED / MAJORITY_AGREE  credited=True
verify_A                 emits  1.000 GEN -> reporter   FINALIZED / NO_MAJORITY     credited=True
create_bounty_B          emits  0.500 GEN -> child B   FINALIZED / MAJORITY_AGREE  credited=True
reclaim_after_timeout_B  emits  0.500 GEN -> poster    FINALIZED / NO_MAJORITY     credited=True

poster   8.0000 GEN     reporter  6.0000 GEN     child_A  0.0     child_B  0.0
```

Those balances reconcile exactly for this run: the poster locked `1.0 + 0.5` GEN
into escrow across the two children and recovered `0.5` on the expired one, a net
`1.0` that is precisely the reporter's `5.0 → 6.0` GEN rise. The `NO_MAJORITY`
labels on the two transfers are the expected EOA-credit shape (validators do not
re-execute a chain-layer credit) — `credited=True` and the balance delta are the
authority, per item 5 below. Every contract's escrow is drained to zero only
through a transfer that the ledger confirms.

## EVIDENCE: the integration suite's own transactions

```
py -3.12 -m pytest tests/integration -m integration -v -s
======================== 18 passed in 834.80s (0:13:54) ========================
```

Console capture: [`evidence/integration-pytest-run.txt`](evidence/integration-pytest-run.txt).

A suite that asserts on receipts fetched from the same RPC that ran the consensus
is only half-verified, and a console capture is not evidence at all. So the
bounties that run created are re-derived **from the deployed registry**
(`list_bounties`, ids prefixed `it-`) and every associated transaction is
re-fetched from the explorer:

```powershell
py -3.12 scripts/collect_integration_evidence.py \
    --registry 0x07B36f7A9CfE15eF8baFfCd66dA951E1d3125dA0 \
    --out evidence/integration-evidence.json
py -3.12 scripts/verify_explorer_evidence.py \
    --evidence evidence/integration-evidence.json --audit none \
    --registry 0x07B36f7A9CfE15eF8baFfCd66dA951E1d3125dA0 \
    --out evidence/explorer-verification-integration.json
```

Result: **22 transactions, all `FINALIZED`; the registry + 5 children, all
`type=CONTRACT`, each storing the content-hash runner pin and byte-matching the
tree** — exit code 0
([`evidence/explorer-verification-integration.json`](evidence/explorer-verification-integration.json)).
The five `create_bounty` hashes the collector found on-chain match the five the
suite printed (its closing `bounty txs created by this run` block), which is the
cross-check that the run being verified is the run that happened. The four
`TestCreateBountyValidation` rejections created **no** child and never enter
`list_bounties` — their `it-reject-*` ids appear only as reverted transactions.

Bounty 1 — `it-528060352` — the two authorization guards. Child
[`0xF17B4CB9…3fa33cD2`](https://explorer-studio.genlayer.com/address/0xF17B4CB917Ad8F857b6b1b491f13cb1b3fa33cD2),
still `OPEN` with its full `1 GEN` escrow:

| step                                                  | value                 | tx                                                                                                                                  |
| ----------------------------------------------------- | --------------------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| `create_bounty` (payable)                             | `1000000000000000000` | [`0x1df52db7…c396f084`](https://explorer-studio.genlayer.com/tx/0x1df52db79d81c41cc6a303978bf95825f1f2d87c09a4684fc538d014c396f084) |
| ↳ child deployed by the factory, funded               | `1000000000000000000` | [`0x2e2b539c…00e1835e`](https://explorer-studio.genlayer.com/tx/0x2e2b539ced56e51f6adaaec313df2bdfde1150621e93881ad339c7eb00e1835e) |
| `submit_report` **by the poster** — refused           | 0                     | [`0x82c10636…211a24833`](https://explorer-studio.genlayer.com/tx/0x82c106368639cda20403a049ec534e78d541ce55d9672c2c8d1dcda211a24833) |
| `reclaim_after_timeout` **by a non-poster** — refused | 0                     | [`0xf37dfc90…e3709143`](https://explorer-studio.genlayer.com/tx/0xf37dfc90619e81afd6eaebbc9645f31636487df3af63f3fa1ca729e7e3709143) |

Both guard transactions are `FINALIZED` — the _transaction_ succeeded, the _call_
reverted with `[EXPECTED] …`. That is exactly what it means for a sender check to
work, and the child's own state is the proof: no report was ever accepted, the
escrow never moved.

Bounty 2 — `it-528147882` — correct report, consensus-paid. Child
[`0x59DACFC6…5C1Cf186`](https://explorer-studio.genlayer.com/address/0x59DACFC695d1b420AcA57Db28B8C2d825C1Cf186),
`PAID`, `reported_verdict=TRUE`, `truth_verdict=TRUE`, escrow now `0`:

| step                                       | value                 | tx                                                                                                                                  |
| ------------------------------------------ | --------------------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| `create_bounty` (payable)                  | `1000000000000000000` | [`0x0df2b096…6d66200e`](https://explorer-studio.genlayer.com/tx/0x0df2b09624ff82a46a30a56d6dc6d4f7fa7cb0c233aa239c1e39836e6d66200e) |
| ↳ child deployed by the factory, funded    | `1000000000000000000` | [`0x5cfcf33d…dfe7afa9`](https://explorer-studio.genlayer.com/tx/0x5cfcf33d61ab015e99cb32d8bdd347b2d16c4a26ba1d107db2e8ba8ddfe7afa9) |
| `submit_report("TRUE")` by the reporter    | 0                     | [`0x39447ece…cea6fe885`](https://explorer-studio.genlayer.com/tx/0x39447ece9b81415ed9fa8453385cf91d697934c6ce16312e9179ca9cea6fe885) |
| `verify()` — leader + 5 validators fetched | 0                     | [`0x3f54dab8…0121d2eb0`](https://explorer-studio.genlayer.com/tx/0x3f54dab81a6dc1900891a74ac81800182a624e8ecd52602535b1b5d0121d2eb0) |
| ↳ payout emitted to the reporter           | `1000000000000000000` | [`0x8c408d8e…5a6017c9`](https://explorer-studio.genlayer.com/tx/0x8c408d8ea13f13a902c6c14daaf0641c3dc7ceab33d3727deb32df105a6017c9) |

`test_payout_moves_the_escrow` does not accept the `PAID` field as proof: it
reads the reporter's balance before and after and requires the difference to
equal the reward. That assertion is what caught the EOA-transfer bug documented
in item 5 below.

Bounty 3 — `it-528283922` — wrong report, rejected, nothing paid. Child
[`0x62C5C5bB…DED6c46E`](https://explorer-studio.genlayer.com/address/0x62C5C5bB64Cfd541c3B6479805760466DED6c46E),
`REJECTED`, `reported_verdict=FALSE` against `truth_verdict=TRUE`:

| step                                    | value                 | tx                                                                                                                                  |
| --------------------------------------- | --------------------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| `create_bounty` (payable)               | `1000000000000000000` | [`0x0d6ed98d…898eed57`](https://explorer-studio.genlayer.com/tx/0x0d6ed98dbae96befac68ae4da408a95a2b54f332f3f529b382d2d82b898eed57) |
| ↳ child deployed by the factory, funded | `1000000000000000000` | [`0xeeab2607…844711d3`](https://explorer-studio.genlayer.com/tx/0xeeab2607bbb3c7f0e216305f214976141f2f0837154fef7829483795844711d3) |
| `submit_report("FALSE")`                | 0                     | [`0x9e804f1e…142a29a47`](https://explorer-studio.genlayer.com/tx/0x9e804f1ea8c00331f96abbe3031221fd93bfb11a37b52a1d0ec347b142a29a47) |
| `verify()`                              | 0                     | [`0x7cc934be…bc6ac048`](https://explorer-studio.genlayer.com/tx/0x7cc934be0f666b008ac6d7a831ebd4a6c234337455a18dfdcb681db5bc6ac048) |

The child's explorer balance is still `1000000000000000000` and it emitted **no**
value transaction — a rejected report is a settled state, not a payout with a
status label attached.

Bounty 4 — `it-528408310` — an absent repository yields a verdict, not a hang.
Child [`0xF837df49…F139f502`](https://explorer-studio.genlayer.com/address/0xF837df49a2847d18152d87ED736a9f10F139f502),
claim `genlayerlabs/definitely-not-a-real-repo-* >= 10` stars. A reproducible
HTTP 404 derives `repo_ok=False`, so the truth is `FALSE` and the reporter who
said `FALSE` is paid: `PAID`, `fact_check_repo_ok=false`, escrow `0`.

| step                                    | value                 | tx                                                                                                                                  |
| --------------------------------------- | --------------------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| `create_bounty` (payable)               | `1000000000000000000` | [`0xb7529d44…105617f0`](https://explorer-studio.genlayer.com/tx/0xb7529d44cf27396487a8ba078bb04b5f7ddf46b668ee2f34f5cd2255105617f0) |
| ↳ child deployed by the factory, funded | `1000000000000000000` | [`0x8208af59…5c2e1bc2`](https://explorer-studio.genlayer.com/tx/0x8208af5999c1556609c9de21fb2dfca4512aa98d98eed3acdc60bac55c2e1bc2) |
| `submit_report("FALSE")`                | 0                     | [`0xa6ae3095…892ec666`](https://explorer-studio.genlayer.com/tx/0xa6ae309512e3e48495cd246e736ed8ed5c886fbfb5dff9642c53ccb1892ec666) |
| `verify()`                              | 0                     | [`0xdd341944…c1bfb833`](https://explorer-studio.genlayer.com/tx/0xdd341944a0f424e2b5787b91a01ea2c080ee7c864792a5b0129f4881c1bfb833) |
| ↳ payout emitted to the reporter        | `1000000000000000000` | [`0x823b85c6…0c9cc3f5`](https://explorer-studio.genlayer.com/tx/0x823b85c66fc80a8d3e9ddadae74074a3cfc1e23babc07b2d3402e97e0c9cc3f5) |

Bounty 5 — `it-528801509` — **an on-time report cannot be undercut by a reclaim**
(fix 3, the exact race, live on studionet). Child
[`0x6736f2B2…bDdF7E63`](https://explorer-studio.genlayer.com/address/0x6736f2B2d77b85D2060Cd2D93f77F869bDdF7E63):
the reporter files a valid report (`submit_report` `FINALIZED`), then the **poster**
attempts `reclaim_after_timeout()` before `verify()` has ever run:

| step                                              | value                 | tx                                                                                                                                  |
| ------------------------------------------------- | --------------------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| `create_bounty` (payable)                         | `1000000000000000000` | [`0x7e300624…d90c2077`](https://explorer-studio.genlayer.com/tx/0x7e30062405db86571355cbd8fb5006dfb125c24a4d28009664609e7cd90c2077) |
| ↳ child deployed by the factory, funded           | `1000000000000000000` | [`0x4417f318…ba98fa88`](https://explorer-studio.genlayer.com/tx/0x4417f3184e45ca1fcf3d4ac239eee316d9dac3fdb686f5f333daf704ba98fa88) |
| `submit_report("TRUE")` by the reporter (on time) | 0                     | [`0xb4be08c1…28381b1c`](https://explorer-studio.genlayer.com/tx/0xb4be08c13436b0ece6a75116e46fbcb926bde5d54d89b0781fac2ff128381b1c) |
| `reclaim_after_timeout()` **by the poster** — refused, child stays `REPORTED` | 0 | [`0x69a807e8…7c6132f3`](https://explorer-studio.genlayer.com/tx/0x69a807e884dbbf9310a2d92943300394167c57f5608e6f35898562e27c6132f3) |

The reclaim transaction is `FINALIZED`, but the child **never left `REPORTED`** —
the explorer still shows it holding its full `1000000000000000000` escrow with a
recorded `reported_verdict=TRUE` and no payout. That is the state gate firing on
chain: a timely, unverified report blocks the refund until `verify()` settles it.

The `verify()` receipts for the paid / rejected bounties carry
`result_name = MAJORITY_AGREE` with `num_of_initial_validators = 5`; the suite
asserts the vote breakdown rather than trusting the call's return value
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
    while `pytest tests/direct -q` passed the whole direct suite — a harness bug
    that only appeared in the exact command a reviewer is most likely to type. Fix: the
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
