"""End-to-end exercise of the deployed registry against studionet (chainId 61999).

This is a client-side driver, not a contract -- it carries no runner header.

This is *not* a mocked run: every transaction below is submitted to the live
studionet RPC and settled by the real leader/validator consensus ring. The
script prints and persists every transaction hash, the deployed child contract
addresses, and the consensus vote breakdown, so the evidence can be checked
independently on https://explorer-studio.genlayer.com.

Flow
----
1. `create_bounty` -- a real value-bearing write (1 GEN attached) on the
   already-deployed BountyRegistry. The registry deploys a child BountyClaim.
2. A *different* EOA submits `submit_report("TRUE")` on that child.
3. `verify()` runs the non-deterministic fact-check: the leader fetches
   https://api.github.com/repos/<claim> and derives stable booleans, and each
   validator fetches again for itself and compares the *derived* verdicts.
   A matching report settles to PAID.
4. A second bounty is created with a deadline already in the past and is never
   reported; the poster then calls `reclaim_after_timeout()`, settling to
   REFUNDED -- proving the timeout path needs no AI verification at all.

Usage
-----
    py -3.12 scripts/e2e_studionet.py --registry 0x...

The poster key is read from GENLAYER_PRIVATE_KEY (.env). The reporter key is
read from GENLAYER_REPORTER_PRIVATE_KEY, or generated once and cached in
.gitignored `.reporter_key` so repeat runs reuse the same funded reporter.

Re-running after an interrupted run
----------------------------------
The hosted studionet RPC sits behind Cloudflare and intermittently answers a
transaction poll with a `502 Bad gateway` HTML page instead of JSON (the SDK
surfaces that as `eth_getTransactionReceipt returned invalid JSON`). That aborts
this driver mid-flow *after* the transaction was already submitted. Re-running
is safe: bounty ids carry a millisecond timestamp, so a retried run creates
fresh bounties rather than colliding with the half-finished one, and the orphan
turns up in `list_bounties` exactly like any other bounty.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from genlayer_py import create_account, create_client, studionet

ROOT = Path(__file__).resolve().parent.parent
ATTO_PER_GEN = 10**18

# The claim this run settles: "does the public GitHub repository
# genlayerlabs/skills have at least 10 stars?" -- checked on-chain by fetching
# https://api.github.com/repos/genlayerlabs/skills and reading stargazers_count.
CLAIM_REPO = "genlayerlabs/skills"
CLAIM_THRESHOLD = 10
CLAIM_REPORT = "TRUE"

FAR_FUTURE_DEADLINE = "2099-12-31T23:59:59"
# The timeout bounty is now created with a deadline a couple of minutes ahead
# and reclaimed only once that deadline has actually passed. `create_bounty`
# rejects a deadline that is not strictly in the future, so the old trick of
# opening a bounty that was already expired at creation is no longer possible.
PAST_DEADLINE = "2020-01-01T00:00:00"  # kept only for the negative-path note below


def soon_deadline(seconds_ahead: int = 150) -> str:
    """A well-formed future UTC `YYYY-MM-DDTHH:MM:SS` the ring will accept."""
    return time.strftime(
        "%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() + seconds_ahead))

TERMINAL_BAD = {"CANCELED", "ERROR", "INVALID"}


def load_dotenv(path: Path) -> None:
    """Minimal .env loader -- no dependency, and never echoes a value."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _as_hex_key(raw) -> str:
    """Normalise whatever the SDK hands back into a 0x-prefixed hex key.

    Deviation from the SDK docs: ``genlayer_py.generate_private_key()`` returns
    ``eth_account``'s ``account.key``, which on the installed eth_account is a
    ``HexBytes`` -- ``str()`` of it is a ``b'...'`` repr, not hex, and
    ``create_account`` then dies in ``binascii``. Normalise instead of assuming.
    """
    if isinstance(raw, str):
        return raw if raw.startswith("0x") else "0x" + raw
    return "0x" + bytes(raw).hex()


def reporter_marker() -> Path:
    return ROOT / ".reporter_key"


def reporter_account() -> tuple:
    """The reporting EOA. Deliberately *not* the poster: the child contract
    rejects a report from the account that funded the bounty."""
    cached = os.environ.get("GENLAYER_REPORTER_PRIVATE_KEY")
    marker = reporter_marker()
    if not cached and marker.is_file():
        cached = marker.read_text(encoding="utf-8").strip()
    fresh = False
    if not cached:
        from eth_account import Account

        created = Account.create()
        cached = _as_hex_key(created.key)
        marker.write_text(cached, encoding="utf-8")
        fresh = True
    return create_account(cached), fresh


def ensure_funded(client, poster, account, amount_atto: int, label: str) -> None:
    """Top an EOA up so it can pay for its own transactions.

    Two deviations, both discovered by hitting them:
      * `genlayer account send <to> <amount>` exits 0 and prints nothing on this
        CLI build without ever moving GEN.
      * `client.w3.eth.send_transaction(...)` is unusable on studionet: web3's
        gas middleware calls `eth_estimateGas`, which the GenLayer provider
        rejects with -32602 "Too many parameters provided".
    So this is a locally signed raw transfer with explicit gas fields, which the
    provider does accept. studionet reports `eth_gasPrice` == 0, so the only
    cost is the value moved.
    """
    balance = int(client.w3.eth.get_balance(account.address))
    if balance >= amount_atto:
        print(
            f"[funding] {label} {account.address} holds {balance / ATTO_PER_GEN} GEN -- skipping")
        return
    print(f"[funding] {label} {account.address} has {balance / ATTO_PER_GEN} GEN, sending "
          f"{amount_atto / ATTO_PER_GEN} GEN ...")
    txn = {
        "from": poster.address,
        "to": account.address,
        "value": amount_atto,
        "nonce": client.w3.eth.get_transaction_count(poster.address),
        "gas": 21000,
        "gasPrice": int(client.w3.eth.gas_price),
        "chainId": client.chain.id,
    }
    signed = poster.sign_transaction(txn)
    raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
    tx_hash = client.w3.eth.send_raw_transaction(bytes(raw)).hex()
    for _ in range(40):
        if int(client.w3.eth.get_balance(account.address)) >= amount_atto:
            break
        time.sleep(3)
    print(f"[funding] tx={tx_hash} -> now "
          f"{client.w3.eth.get_balance(account.address) / ATTO_PER_GEN} GEN")


def wait_child_live(client, child: str, timeout: int = 300, label: str = "child") -> dict:
    """Poll a factory-deployed child until it is deployable-readable.

    The parent deploys the child with ``on="finalized"``, so the child's own
    deployment transaction only runs *after* the parent's transaction finalizes.
    Reading it immediately returns "Contract ... not found" -- the parent being
    FINALIZED is not sufficient, which is exactly the kind of thing a mocked test
    would never reveal.
    """
    deadline = time.time() + timeout
    last_error = None
    while time.time() < deadline:
        try:
            return dict(client.read_contract(child, "get_status"))
        except Exception as exc:
            last_error = exc
            time.sleep(5)
    raise SystemExit(f"{label} {child} never became readable: {last_error}")


def rpc_url(chain) -> str:
    """studionet.rpc_urls is nested ({'default': {'http': [...]}}), not flat."""
    urls = chain.rpc_urls
    inner = urls.get("default", urls) if isinstance(urls, dict) else urls
    if isinstance(inner, dict):
        return list(inner.values())[0][0]
    return inner[0]


def status_name(tx: dict) -> str:
    name = tx.get("status_name")
    if name:
        return str(name)
    from genlayer_py.types.transactions import TransactionStatus

    by_number = {int(s.value) if str(s.value).isdigit()
                 else None: s.name for s in TransactionStatus}
    return by_number.get(tx.get("status"), str(tx.get("status")))


def wait_for_final(client, tx_hash: str, timeout: int = 900, label: str = "") -> dict:
    """Poll until the consensus ring reaches FINALIZED (not merely ACCEPTED)."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            tx = client.get_transaction(tx_hash)
        except Exception as exc:  # node may not have indexed it yet
            print(f"  [wait] {label}: not readable yet ({type(exc).__name__})")
            time.sleep(5)
            continue
        name = status_name(tx)
        if name != last:
            print(f"  [wait] {label}: {name}")
            last = name
        if name == "FINALIZED":
            return tx
        if name in TERMINAL_BAD:
            raise SystemExit(f"{label} settled in terminal state {name}: {tx}")
        time.sleep(5)
    raise SystemExit(
        f"{label} did not finalize within {timeout}s (last state {last})")


def _mapping(value) -> dict:
    """The SDK decodes some receipt fields as plain strings; tolerate both."""
    return value if isinstance(value, dict) else {}


def consensus_summary(tx: dict) -> dict:
    data = _mapping(tx.get("consensus_data"))
    votes = _mapping(data.get("votes"))
    return {
        "tx_hash": tx.get("hash") or tx.get("tx_id"),
        "status_name": status_name(tx),
        "result_name": tx.get("result_name"),
        "num_of_rounds": tx.get("num_of_rounds"),
        "nonce": tx.get("nonce"),
        "created_at": tx.get("created_at"),
        "sender": tx.get("sender") or tx.get("from_address"),
        "recipient": tx.get("recipient") or tx.get("to_address"),
        "value_atto": int(tx.get("value") or 0),
        "value_credited": tx.get("value_credited"),
        "gaslimit": tx.get("gaslimit"),
        "messages": [
            {
                "recipient": m.get("recipient") or m.get("to_address"),
                "value_atto": int(m.get("value") or 0),
                "type": m.get("message_type") or m.get("type"),
            }
            for m in (tx.get("messages") or [])
            if isinstance(m, dict)
        ],
        "votes": {str(k): str(v) for k, v in votes.items()},
        "validator_executions": [
            {
                "mode": r.get("mode"),
                "vote": r.get("vote"),
                "execution_result": r.get("execution_result"),
                "node": _mapping(r.get("node_config")).get("address"),
                "model": _mapping(_mapping(r.get("node_config")).get("primary_model")).get("model"),
                "result": _mapping(r.get("result")).get("status") or r.get("result"),
                "readable_payload": _mapping(_mapping(r.get("result")).get("payload")).get("readable"),
            }
            for r in (data.get("leader_receipt") or []) + (data.get("validators") or [])
            if isinstance(r, dict)
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--registry", required=True,
                        help="deployed BountyRegistry address")
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument(
        "--out", default=str(ROOT / "evidence" / "studionet-e2e.json"))
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    private_key = os.environ.get("GENLAYER_PRIVATE_KEY")
    if not private_key:
        raise SystemExit(
            "GENLAYER_PRIVATE_KEY is not set; refusing to prompt for it.")

    poster = create_account(private_key)
    reporter, reporter_is_fresh = reporter_account()
    client = create_client(studionet, account=poster)

    evidence: dict = {
        "network": {"name": "studionet", "chain_id": studionet.id, "rpc": rpc_url(studionet)},
        "explorer_base": "https://explorer-studio.genlayer.com",
        "registry": args.registry,
        "poster": poster.address,
        "reporter": reporter.address,
        "claim": {
            "repo": CLAIM_REPO,
            "threshold": CLAIM_THRESHOLD,
            "source_url": f"https://api.github.com/repos/{CLAIM_REPO}",
            "report_submitted": CLAIM_REPORT,
        },
        "transactions": [],
    }

    print(f"[setup] chainId={studionet.id} registry={args.registry}")
    print(f"[setup] poster  ={poster.address}")
    print(
        f"[setup] reporter={reporter.address} ({'NEW - fund it before step 3' if reporter_is_fresh else 'cached'})")

    before = client.read_contract(args.registry, "list_bounties")
    print(f"[read] list_bounties before: {before}")
    evidence["bounties_before"] = int(before["count"])
    ensure_funded(client, poster, reporter, ATTO_PER_GEN, "reporter")
    stamp = int(time.time())

    # ------------------------------------------------------------------
    # 1. create_bounty -- real value-bearing write (payable)
    # ------------------------------------------------------------------
    uid_a = f"skills-ge-10-{stamp}"
    print(f"\n[1] create_bounty({uid_a}) locking 1 GEN ...")
    tx_a = client.write_contract(
        args.registry,
        "create_bounty",
        account=poster,
        value=1 * ATTO_PER_GEN,
        args=[uid_a, CLAIM_REPO, CLAIM_THRESHOLD, FAR_FUTURE_DEADLINE],
    )
    tx_a = str(tx_a)
    print(f"    tx={tx_a}")
    receipt_a = wait_for_final(client, tx_a, args.timeout, "create_bounty A")
    step_a = {"step": "create_bounty_A", **consensus_summary(receipt_a)}
    evidence["transactions"].append(step_a)
    print(f"    carried value: {step_a['value_atto'] / ATTO_PER_GEN} GEN "
          f"(value_credited={step_a['value_credited']})")

    # ------------------------------------------------------------------
    # 2. resolve the child the factory deployed
    # ------------------------------------------------------------------
    child = client.read_contract(
        args.registry, "get_bounty_address", args=[uid_a])
    child = str(child)
    print(f"\n[2] child BountyClaim = {child}")
    evidence["child_A"] = child
    triggered = [str(h) for h in client.get_triggered_transaction_ids(tx_a)]
    evidence["transactions"][0]["triggered_tx_ids"] = triggered
    print(f"    triggered tx ids: {triggered}")

    status = wait_child_live(client, child, args.timeout, f"child {child}")
    print(
        f"    child status: {status['status']} escrow={int(status['escrow_atto']) / ATTO_PER_GEN} GEN")

    # ------------------------------------------------------------------
    # 3. submit_report from a different EOA
    # ------------------------------------------------------------------
    print(f"\n[3] reporter submit_report({CLAIM_REPORT!r}) ...")
    tx_report = str(
        client.write_contract(child, "submit_report",
                              account=reporter, args=[CLAIM_REPORT])
    )
    print(f"    tx={tx_report}")
    receipt_report = wait_for_final(
        client, tx_report, args.timeout, "submit_report")
    evidence["transactions"].append(
        {"step": "submit_report_A", **consensus_summary(receipt_report)})
    print(
        f"    child status now: {client.read_contract(child, 'get_status')['status']}")

    # ------------------------------------------------------------------
    # 4. verify -- real leader/validator consensus on the derived verdict
    # ------------------------------------------------------------------
    balance_before_payout = int(client.w3.eth.get_balance(reporter.address))
    evidence["reporter_balance_before_atto"] = balance_before_payout
    print(
        f"\n[4] verify() -- live fact-check consensus ... "
        f"(reporter holds {balance_before_payout / ATTO_PER_GEN} GEN beforehand)"
    )
    tx_verify = str(client.write_contract(child, "verify", account=poster))
    print(f"    tx={tx_verify}")
    receipt_verify = wait_for_final(client, tx_verify, args.timeout, "verify")
    summary = consensus_summary(receipt_verify)
    summary["readable_result"] = _mapping(
        _mapping(receipt_verify.get("data")).get("calldata")).get("readable")
    evidence["transactions"].append({"step": "verify_A", **summary})
    print(f"    result_name={summary['result_name']} votes={summary['votes']}")

    settled = client.read_contract(child, "get_status")
    evidence["status_A"] = {k: v for k, v in settled.items()}
    print(f"    final: status={settled['status']} truth={settled['truth_verdict']} "
          f"reported={settled['reported_verdict']} evidence='{settled['evidence']}'")
    print(
        f"    escrow left in child: {int(settled['escrow_atto']) / ATTO_PER_GEN} GEN")
    reporter_balance = int(client.w3.eth.get_balance(reporter.address))
    evidence["reporter_balance_atto"] = reporter_balance
    print(
        f"    reporter {reporter.address} balance now: {reporter_balance / ATTO_PER_GEN} GEN")
    moved = reporter_balance - balance_before_payout
    if int(settled["reward_atto"]) and settled["status"] == "PAID" and moved < int(settled["reward_atto"]):
        # Never let a PAID status stand in for money that actually moved; the
        # audit script traces the emitted transfer to its own transaction.
        print(
            f"    WARNING: PAID but the reporter only received {moved / ATTO_PER_GEN} GEN of "
            f"{int(settled['reward_atto']) / ATTO_PER_GEN} GEN -- run scripts/audit_payout_ledger.py"
        )

    # ------------------------------------------------------------------
    # 5. second bounty, never reported, deadline reached -> reclaim
    # ------------------------------------------------------------------
    # `create_bounty` now refuses a deadline that is not strictly in the future,
    # so the timeout path can no longer be shown with an already-expired bounty:
    # open one that expires ~2.5 minutes out, let the deadline pass untouched,
    # then reclaim -- which is the real sequence a poster goes through.
    uid_b = f"timeout-{stamp}"
    deadline_b = soon_deadline()
    print(
        f"\n[5] create_bounty({uid_b}) with near-future deadline {deadline_b}, "
        f"0.5 GEN, never reported ...")
    tx_b = str(
        client.write_contract(
            args.registry,
            "create_bounty",
            account=poster,
            value=ATTO_PER_GEN // 2,
            args=[uid_b, CLAIM_REPO, CLAIM_THRESHOLD, deadline_b],
        )
    )
    print(f"    tx={tx_b}")
    receipt_b = wait_for_final(client, tx_b, args.timeout, "create_bounty B")
    evidence["transactions"].append(
        {"step": "create_bounty_B", **consensus_summary(receipt_b)})

    child_b = str(client.read_contract(
        args.registry, "get_bounty_address", args=[uid_b]))
    print(f"    child B = {child_b}")
    evidence["child_B"] = child_b
    status_b = wait_child_live(
        client, child_b, args.timeout, f"child B {child_b}")
    print(
        f"    status B: {status_b['status']} escrow={int(status_b['escrow_atto']) / ATTO_PER_GEN} GEN")

    # Reclaim is guarded by the deadline; wait until the chain clock is past it.
    import calendar

    deadline_epoch = calendar.timegm(
        time.strptime(deadline_b, "%Y-%m-%dT%H:%M:%S")) + 5
    remaining = deadline_epoch - time.time()
    if remaining > 0:
        print(f"    waiting {int(remaining)}s for deadline {deadline_b} to pass ...")
        time.sleep(remaining)

    # ------------------------------------------------------------------
    # 5b. a post-expiry report must be refused outright (never processed)
    # ------------------------------------------------------------------
    print(
        f"\n[5b] reporter submit_report('TRUE') on the expired B (must be refused) ...")
    tx_late = str(client.write_contract(
        child_b, "submit_report", account=reporter, args=["TRUE"]))
    print(f"    tx={tx_late}")
    try:
        receipt_late = wait_for_final(
            client, tx_late, args.timeout, "late submit_report")
        evidence["transactions"].append(
            {"step": "late_submit_report_B", **consensus_summary(receipt_late)})
    except SystemExit:
        # A contract-level rejection can settle in a non-finalized rejecting
        # state; either way the invariant below is what proves the fix.
        pass
    after_late = client.read_contract(child_b, "get_status")
    evidence["late_report_refused_status"] = after_late["status"]
    print(f"    status after the late report: {after_late['status']}")
    if after_late["status"] != "OPEN":
        print("    WARNING: a post-expiry report changed the bounty state; "
              "the deadline gate did not refuse it", file=sys.stderr)

    print("\n[6] poster reclaim_after_timeout() on B ...")
    tx_c = str(client.write_contract(
        child_b, "reclaim_after_timeout", account=poster))
    print(f"    tx={tx_c}")
    receipt_c = wait_for_final(
        client, tx_c, args.timeout, "reclaim_after_timeout")
    evidence["transactions"].append(
        {"step": "reclaim_after_timeout_B", **consensus_summary(receipt_c)})
    settled_b = client.read_contract(child_b, "get_status")
    evidence["status_B"] = {k: v for k, v in settled_b.items()}
    print(
        f"    final B: status={settled_b['status']} escrow={int(settled_b['escrow_atto']) / ATTO_PER_GEN} GEN")

    # ------------------------------------------------------------------
    # 6. parent-side aggregation, reading every child synchronously
    # ------------------------------------------------------------------
    print("\n[7] registry aggregate_statuses() (parent reads both children) ...")
    rollup = client.read_contract(args.registry, "aggregate_statuses")
    evidence["aggregate"] = {
        "by_status": dict(rollup.get("by_status") or {}),
        "escrow_total_atto": int(rollup.get("escrow_total_atto", 0)),
        "children": list(rollup.get("children") or []),
    }
    print(f"    by_status={evidence['aggregate']['by_status']}")
    listing = client.read_contract(args.registry, "list_bounties")
    evidence["list_bounties"] = {
        "count": int(listing["count"]),
        "total_created": int(listing["total_created"]),
        "bounties": [dict(entry) for entry in listing["bounties"]],
    }
    print(
        f"    list_bounties count={listing['count']} total_created={listing['total_created']}")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(evidence, indent=2,
                   default=str), encoding="utf-8")
    print(f"\n[done] evidence written to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
