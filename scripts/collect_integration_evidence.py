"""Collect explorer evidence for the *integration suite's* own transactions.

`tests/integration` runs real transactions on studionet, but it asserts on
receipts read back from the RPC that produced them. This script closes the same
loop that `verify_explorer_evidence.py` closes for the E2E driver: it finds the
bounties the suite created (ids prefixed `it-`) by asking the *deployed registry*
-- not a console capture -- then resolves every transaction the explorer
associates with each child, and writes an evidence file in the shape
`verify_explorer_evidence.py` consumes.

Usage::

    py -3.12 scripts/collect_integration_evidence.py \
        --registry 0x116DE... --out evidence/integration-evidence.json

Read-only: it never sends a transaction. The private key is read from the
environment / git-ignored `.env` only to build a client for *view* calls.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from verify_explorer_evidence import BASE, check_transaction  # noqa: E402

INTEGRATION_ID_PREFIX = "it-"

# The explorer stores the call as an opaque msgpack/base64 blob, but the method
# name inside it is readable. Labelling from that is honest (it is the explorer's
# own record of what the transaction called) and beats guessing from ordering.
KNOWN_METHODS = ("submit_report", "reclaim_after_timeout", "verify",
                 "create_bounty")


def infer_method(raw_calldata) -> str:
    """Best-effort method name behind an inbound write, '' when undecidable."""
    if not isinstance(raw_calldata, str) or not raw_calldata:
        return ""
    import base64

    try:
        blob = base64.b64decode(raw_calldata, validate=False)
    except Exception:  # noqa: BLE001 - not base64 after all
        return ""
    text = blob.decode("latin-1")
    # Longest first: 'verify' is a substring of nothing here, but ordering keeps
    # a hypothetical overlap deterministic.
    for method in sorted(KNOWN_METHODS, key=len, reverse=True):
        if method in text:
            return method
    return ""


def _load_dotenv() -> None:
    path = ROOT / ".env"
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def child_transactions(address: str) -> dict:
    """Everything the explorer says happened at one child contract address."""
    from verify_explorer_evidence import get_json

    payload = get_json(f"{BASE}/api/address/{address}")
    creator = payload.get("creator_info") or {}
    txs = payload.get("transactions") or []
    normalised = []
    for tx in txs:
        if not tx.get("hash"):
            continue
        from_address = (tx.get("from_address") or "").lower()
        to_address = (tx.get("to_address") or "").lower()
        value = int(tx.get("value") or 0)
        self_address = address.lower()
        if from_address == self_address and value:
            role = "value-out"          # payout / refund emitted by the child
        elif from_address == self_address:
            role = "call-out"
        elif value:
            role = "funded-in"          # the factory deploy that carried the escrow
        else:
            data = tx.get("data") if isinstance(tx.get("data"), dict) else {}
            method = infer_method(data.get("calldata"))
            role = f"write:{method}" if method else "write-in"
        normalised.append(
            {
                "tx_hash": tx.get("hash"),
                "status": tx.get("status"),
                "value": value,
                "from_address": tx.get("from_address"),
                "to_address": tx.get("to_address"),
                "role": role,
                "execution_mode": tx.get("execution_mode"),
                "num_of_initial_validators": tx.get("num_of_initial_validators"),
                "triggered_by_hash": tx.get("triggered_by_hash"),
                "result_name": (tx.get("consensus_data") or {}).get("result_name"),
            }
        )
    return {
        "explorer_type": payload.get("type"),
        "tx_count": payload.get("tx_count"),
        "deployment_tx_hash": creator.get("deployment_tx_hash"),
        "creator_address": creator.get("creator_address"),
        "balance": (payload.get("state") or {}).get("balance"),
        "transactions": normalised,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--registry", default="0x116DE8851D2101D583b84EBFEEadc9d2f10Ae012")
    parser.add_argument("--prefix", default=INTEGRATION_ID_PREFIX)
    parser.add_argument(
        "--out", default=str(ROOT / "evidence" / "integration-evidence.json"))
    args = parser.parse_args()

    _load_dotenv()
    key = os.environ.get("GENLAYER_PRIVATE_KEY")
    if not key:
        print("[collect] GENLAYER_PRIVATE_KEY is unavailable -- cannot read the registry",
              file=sys.stderr)
        return 2

    from genlayer_py import create_account, create_client, studionet

    client = create_client(studionet, account=create_account(key))
    listing = client.read_contract(args.registry, "list_bounties")
    children = [
        {"bounty_id": entry["bounty_id"], "address": entry["address"]}
        for entry in listing.get("bounties", [])
        if str(entry["bounty_id"]).startswith(args.prefix)
    ]
    print(f"[collect] registry indexes {listing.get('count')} bounties, "
          f"{len(children)} of them from integration runs")

    evidence: dict = {"registry": args.registry, "transactions": [],
                      "children": {}, "bounties": []}
    seen: set[str] = set()

    def add(step: str, tx_hash: str) -> None:
        if tx_hash and tx_hash not in seen:
            seen.add(tx_hash)
            evidence["transactions"].append({"step": step, "tx_hash": tx_hash})

    for index, child in enumerate(children, start=1):
        address = child["address"]
        bounty_id = child["bounty_id"]
        try:
            detail = child_transactions(address)
        except urllib.error.HTTPError as exc:
            print(f"  {bounty_id}: explorer HTTP {exc.code} for {address}")
            continue
        status = {}
        try:
            status = client.read_contract(address, "get_status") or {}
        except Exception as exc:  # noqa: BLE001 - report, never hide
            detail["state_read_error"] = str(exc)
        detail["bounty_id"] = bounty_id
        detail["on_chain_status"] = status.get("status")
        detail["reward_atto"] = status.get("reward_atto")
        detail["truth_verdict"] = status.get("truth_verdict")
        detail["reported_verdict"] = status.get("reported_verdict")
        detail["explorer_url"] = f"{BASE}/address/{address}"
        evidence["bounties"].append(detail)
        evidence["children"][f"child_{index}"] = address

        print(
            f"\n[collect] {bounty_id} -> {address} status={status.get('status')}")
        print(f"  explorer type={detail['explorer_type']} "
              f"tx_count={detail['tx_count']} balance={detail['balance']}")
        add(f"{bounty_id}:deploy", str(detail.get("deployment_tx_hash") or ""))
        for tx in detail["transactions"]:
            print(f"  {tx['status']:<12} {tx['role']:<20} value={tx['value']:<22} "
                  f"to={tx['to_address']} {tx['tx_hash']}")
            add(f"{bounty_id}:{tx['role']}", tx["tx_hash"])
            # The parent's create_bounty transaction is not part of the child's
            # own address listing; the child's *deployment* is the one record
            # that points back at it. An emitted payout points back at the
            # `verify()` call instead, so only the deploy is followed here.
            if tx["role"] == "funded-in":
                add(f"{bounty_id}:create_bounty(parent)",
                    str(tx.get("triggered_by_hash") or ""))

    # Re-resolve each hash through the transaction endpoint: the address listing
    # and the transaction endpoint are two different explorer reads, and a record
    # that appears in one but not the other is exactly the kind of thing worth
    # catching rather than papering over.
    print(f"\n[collect] re-checking {len(seen)} distinct transactions")
    for tx_hash in sorted(seen):
        try:
            record = check_transaction(tx_hash)
            print(
                f"  {record.get('status'):<12} value={record.get('value', 0):<22} {tx_hash}")
        except Exception as exc:  # noqa: BLE001
            print(f"  ERROR {tx_hash}: {exc}")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(evidence, indent=2,
                   default=str), encoding="utf-8")
    print(f"\n[collect] wrote {out} "
          f"({len(evidence['transactions'])} txs, {len(children)} children)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
