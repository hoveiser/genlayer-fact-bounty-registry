"""Audit whether every value transfer the contracts emitted actually landed.

Why this exists
---------------
`BountyClaim` releases its escrow with the SDK's documented pure-transfer
primitive::

    gl.get_contract_at(recipient).emit_transfer(value=...)

`emit_transfer` does not call a method: it posts an internal message that the
ring turns into its own transaction, and *that* transaction has to be accepted
before the recipient's balance moves. A contract's status field can therefore
read `PAID` while the GEN never reaches the reporter.

This script separates the two questions. For every transaction in
`evidence/studionet-e2e.json` it collects

* the messages the transaction emitted (recipient, value, calldata),
* the triggered transactions those messages produced, and for each one the
  consensus `result_name`, the `last_leader` that handled it and whether
  `value_credited` was set, and
* the resulting balances of the poster, the reporter and each child contract,
  compared against the balance the run *should* have produced.

It reads only the studionet RPC and writes `evidence/payout-audit.json`. It
never asserts that money moved -- it reports where it did not.

Usage::

    py -3.12 scripts/audit_payout_ledger.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from genlayer_py import create_account, create_client, studionet

ROOT = Path(__file__).resolve().parent.parent
ATTO_PER_GEN = 10**18


def load_env() -> None:
    path = ROOT / ".env"
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _mapping(value) -> dict:
    return value if isinstance(value, dict) else {}


def _sequence(value) -> list:
    return value if isinstance(value, list) else []


def main() -> int:
    load_env()
    private_key = os.environ.get("GENLAYER_PRIVATE_KEY")
    if not private_key:
        print("[audit] GENLAYER_PRIVATE_KEY is not set", file=sys.stderr)
        return 2

    evidence_path = ROOT / "evidence" / "studionet-e2e.json"
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    client = create_client(studionet, account=create_account(private_key))

    steps = {entry.get("step"): entry.get("tx_hash")
             for entry in evidence.get("transactions", [])}
    report: dict = {"transactions": [],
                    "triggered": [], "balances": {}, "problems": []}

    print(f"[audit] auditing {len(steps)} steps from {evidence_path.name}")
    for step, tx_hash in steps.items():
        if not tx_hash:
            continue
        try:
            tx = client.get_transaction(str(tx_hash))
        except Exception as exc:  # noqa: BLE001 - an unreadable tx is a finding
            report["problems"].append(f"{step}: receipt unreadable ({exc})")
            continue
        consensus = _mapping(tx.get("consensus_data"))
        messages = _sequence(consensus.get("messages") or tx.get("messages"))
        triggered = _sequence(tx.get("triggered_transactions")
                              or consensus.get("triggered_transactions"))
        report["transactions"].append(
            {
                "step": step,
                "tx_hash": str(tx_hash),
                "status_name": tx.get("status_name"),
                "result_name": tx.get("result_name"),
                "value": int(tx.get("value") or 0),
                "value_credited": bool(tx.get("value_credited")),
                "emitted_messages": len(messages),
                "triggered_transactions": [str(h) for h in triggered],
            }
        )
        for message in messages:
            message = _mapping(message)
            if int(message.get("value") or 0) == 0:
                continue
            print(
                f"  {step:<24} emits {int(message['value']) / ATTO_PER_GEN:>6.3f} GEN "
                f"-> {message.get('recipient')}"
            )

        for handle in triggered:
            try:
                inner = client.get_transaction(str(handle))
            except Exception as exc:  # noqa: BLE001
                report["triggered"].append(
                    {"parent_step": step, "tx_hash": str(handle), "error": str(exc)})
                continue
            record = {
                "parent_step": step,
                "tx_hash": str(handle),
                "from_address": inner.get("sender") or _mapping(inner.get("data")).get("from_address"),
                "to_address": inner.get("recipient") or _mapping(inner.get("data")).get("to_address"),
                "value": int(inner.get("value") or 0),
                "status_name": inner.get("status_name"),
                "result_name": inner.get("result_name"),
                "last_leader": inner.get("last_leader"),
                "value_credited": bool(inner.get("value_credited")),
                "activator": inner.get("activator"),
            }
            report["triggered"].append(record)
            print(
                f"    triggered {record['value'] / ATTO_PER_GEN:>6.3f} GEN "
                f"{record['from_address']} -> {record['to_address']}: "
                f"{record['status_name']} / {record['result_name']} "
                f"leader={record['last_leader']} credited={record['value_credited']}"
            )
            if record["value"] and not record["value_credited"]:
                report["problems"].append(
                    f"{step}: emitted transfer of {record['value']} atto to "
                    f"{record['to_address']} settled {record['result_name']} via "
                    f"'{record['last_leader']}' and credited nothing"
                )

    print("[audit] final balances")
    accounts = {
        "poster": evidence.get("poster"),
        "reporter": evidence.get("reporter"),
        "child_A": evidence.get("child_A"),
        "child_B": evidence.get("child_B"),
    }
    for label, address in accounts.items():
        if not address:
            continue
        atto = int(client.w3.eth.get_balance(address))
        report["balances"][label] = {
            "address": address, "atto": atto, "gen": atto / ATTO_PER_GEN}
        print(f"  {label:<10} {atto / ATTO_PER_GEN:>10.4f} GEN  {address}")

    # If the run captured a pre-payout balance for the reporter, the ledger gets a
    # second, arithmetic check on top of the per-message `value_credited` flag.
    before = evidence.get("reporter_balance_before_atto")
    settled = evidence.get("status_A") or {}
    if before and settled.get("reward_atto") and settled.get("status") == "PAID":
        moved = report["balances"]["reporter"]["atto"] - int(before)
        report["reporter_balance_moved_atto"] = moved
        if moved < int(settled["reward_atto"]):
            report["problems"].append(
                f"reporter balance rose {moved} atto for a {settled['reward_atto']} atto payout"
            )

    out = ROOT / "evidence" / "payout-audit.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    if report["problems"]:
        print("\n[audit] TRANSFERS THAT DID NOT LAND:")
        for problem in report["problems"]:
            print(f"  - {problem}")
    else:
        print("\n[audit] every emitted transfer was credited.")
    print(f"[audit] report written to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
