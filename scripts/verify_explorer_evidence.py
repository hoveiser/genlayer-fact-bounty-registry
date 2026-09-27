"""Independently verify the studionet evidence against the public explorer API.

Nothing in this script reads the local RPC or any cached state: every fact it
reports is fetched from https://explorer-studio.genlayer.com, which indexes the
chain separately from the nodes that ran the consensus. It exists so that the
README's claims are checkable by a third party -- and so that a failure here is
reported rather than assumed away.

What it checks
--------------
* every transaction hash from the run resolves on the explorer and reports
  ``status == FINALIZED``;
* every contract address (parent + each child) resolves as ``type == CONTRACT``;
* the *on-chain* source of the parent really begins with the pinned-runner
  dependency header -- i.e. the pin is what the chain stored, not just what the
  local file says;
* the value carried by the `create_bounty` transaction is visible from the
  explorer's own record of the tx.

Usage::

    py -3.12 scripts/verify_explorer_evidence.py --evidence evidence/studionet-e2e.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE = "https://explorer-studio.genlayer.com"
EXPLORER_TX = f"{BASE}/tx"
EXPLORER_ADDRESS = f"{BASE}/address"
PIN_PREFIX = '# { "Depends": "py-genlayer:'
BANNED_REFS = {"test", "latest", "dev", "main"}


def deployed_pin_ref(source: str) -> str:
    """The runner ref the chain actually stored, or '' when there is none.

    Returns '' for `:test`/`:latest` too -- a tag is not a pin, and requirement 1
    is about the deployed artifact, not about the local file.
    """
    first_line = source.splitlines()[0] if source else ""
    if not first_line.startswith(PIN_PREFIX):
        return ""
    ref = first_line.split("py-genlayer:", 1)[1].strip().rstrip('"} ').strip()
    if ref in BANNED_REFS or len(ref) < 40 or not ref.isalnum():
        return ""
    return ref


# A 200 HTML shell is what the SPA serves for *unknown* routes, so the JSON API
# is the only honest way to confirm a record exists.
HEADERS = {"User-Agent": "genlayer-bounty-registry-evidence-check",
           "Accept": "application/json"}


def get_json(url: str, timeout: int = 90, attempts: int = 4):
    """GET a explorer JSON record, retrying a transient 5xx.

    The explorer is fronted by Cloudflare and does answer `503 Service
    Unavailable` / `502 Bad Gateway` for records that are indexed a second later,
    so a single failure is retried before it is treated as a problem. A record
    that is genuinely absent still fails -- with `404` -- and is reported.
    """
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code < 500:
                raise
        except Exception as exc:  # noqa: BLE001 - connection resets etc.
            last_error = exc
        time.sleep(2.0 * (attempt + 1))
    raise last_error


def check_transaction(tx_hash: str) -> dict:
    payload = get_json(f"{BASE}/api/transactions/{tx_hash}")
    tx = payload.get("transaction") or {}
    record = {
        "tx_hash": tx_hash,
        "explorer_url": f"{EXPLORER_TX}/{tx_hash}",
        "found": bool(tx.get("hash")),
        "status": tx.get("status"),
        "from_address": tx.get("from_address"),
        "to_address": tx.get("to_address"),
        "value": int(tx.get("value") or 0),
        "created_at": tx.get("created_at"),
        "num_of_initial_validators": tx.get("num_of_initial_validators"),
        "result": tx.get("consensus_data", {}).get("result_name")
        if isinstance(tx.get("consensus_data"), dict)
        else None,
    }
    if not tx:
        # Not indexed yet (or a different payload shape); say so with evidence
        # rather than reporting a clean bill of health.
        record["explorer_payload_keys"] = sorted(payload.keys()) if isinstance(
            payload, dict) else str(type(payload))
    return record


def check_address(address: str, require_pin: bool, local_source: Path | None = None) -> dict:
    payload = get_json(f"{BASE}/api/address/{address}")
    source = payload.get("contract_code") or ""
    first_line = source.splitlines()[0] if source else ""
    pin = deployed_pin_ref(source)
    result = {
        "address": address,
        "explorer_url": f"{EXPLORER_ADDRESS}/{address}",
        "explorer_type": payload.get("type"),
        "tx_count": payload.get("tx_count"),
        "deployed_source_first_line": first_line[:120],
        "deployed_runner_pin": pin,
        "deployed_source_pin_is_content_hash": bool(pin),
        "creator": (payload.get("creator_info") or {}).get("address")
        if isinstance(payload.get("creator_info"), dict)
        else None,
    }
    result["pin_header_present_on_chain"] = bool(pin)
    if require_pin:
        result["required_pin_seen"] = bool(pin)
    if local_source is not None and local_source.is_file() and source:
        # "The file in this repo is the source that ran" is a claim worth testing
        # against the chain rather than asserting. The explorer stores the
        # uploaded contract source verbatim, including its line endings, so both
        # the byte-exact and the line-ending-insensitive comparison are reported.
        deployed_bytes = source.encode("utf-8")
        local_bytes = local_source.read_bytes()
        result["local_source_file"] = str(local_source)
        result["deployed_source_equals_local_bytes"] = local_bytes == deployed_bytes
        result["deployed_source_equals_local_modulo_line_endings"] = (
            local_bytes.replace(b"\r\n", b"\n")
            == deployed_bytes.replace(b"\r\n", b"\n")
        )
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", default=str(ROOT /
                        "evidence" / "studionet-e2e.json"))
    parser.add_argument("--registry", required=True)
    parser.add_argument(
        "--out",
        default=str(ROOT / "evidence" / "explorer-verification.json"),
        help="where to write the report; a second file keeps a second run's evidence separate",
    )
    parser.add_argument(
        "--audit",
        default=str(ROOT / "evidence" / "payout-audit.json"),
        help="payout-audit JSON whose emitted transfers are verified too; 'none' to skip",
    )
    parser.add_argument(
        "--local-source",
        default=str(ROOT / "contracts" / "BountyRegistry.py"),
        help="the repo file that claims to be the deployed registry's source",
    )
    parser.add_argument(
        "--child-source",
        default=str(ROOT / "contracts" / "BountyClaim.py"),
        help="the repo file that claims to be the source of every deployed child",
    )
    args = parser.parse_args()

    evidence_path = Path(args.evidence)
    evidence = (
        json.loads(evidence_path.read_text(encoding="utf-8"))
        if evidence_path.is_file()
        else {"transactions": [], "child_A": None, "child_B": None}
    )

    report: dict = {"explorer_base": BASE,
                    "transactions": [], "addresses": [], "problems": []}

    hashes = [entry["tx_hash"] for entry in evidence.get(
        "transactions", []) if entry.get("tx_hash")]
    print(
        f"[explorer] verifying {len(hashes)} transaction hashes from {evidence_path.name}")
    for tx_hash in hashes:
        try:
            record = check_transaction(tx_hash)
        except urllib.error.HTTPError as exc:
            record = {"tx_hash": tx_hash,
                      "found": False, "http_error": exc.code}
            report["problems"].append(
                f"{tx_hash}: explorer returned HTTP {exc.code}")
        except Exception as exc:  # noqa: BLE001 - report, never hide
            record = {"tx_hash": tx_hash, "found": False, "error": str(exc)}
            report["problems"].append(f"{tx_hash}: {exc}")
        report["transactions"].append(record)
        step = next(
            (e.get("step") for e in evidence.get(
                "transactions", []) if e.get("tx_hash") == tx_hash),
            "?",
        )
        status = record.get("status") or "NOT-FOUND"
        print(f"  {step:<24} {status:<12} value={record.get('value', 0)} {tx_hash}")
        if record.get("status") != "FINALIZED":
            report["problems"].append(
                f"{step} {tx_hash}: explorer status is {record.get('status')!r}, not FINALIZED"
            )

    # The money itself moves in transactions that the contracts only *emit*: a
    # factory child deploy and each payout/refund. Those are the records that
    # prove value actually settled, so the explorer check covers them too.
    audit_path = None if args.audit.lower() == "none" else Path(args.audit)
    if audit_path is not None and audit_path.is_file():
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
        extra = [
            (f"{entry.get('parent_step')}-> {entry.get('to_address')}",
             entry["tx_hash"])
            for entry in audit.get("triggered", [])
            if entry.get("tx_hash")
        ]
        print(
            f"[explorer] verifying {len(extra)} emitted transactions from {audit_path.name}")
        for label, tx_hash in extra:
            try:
                record = check_transaction(tx_hash)
            except Exception as exc:  # noqa: BLE001
                record = {"tx_hash": tx_hash,
                          "found": False, "error": str(exc)}
                report["problems"].append(f"{tx_hash}: {exc}")
            report["transactions"].append(record)
            status = record.get("status") or "NOT-FOUND"
            print(
                f"  {label:<38} {status:<12} value={record.get('value', 0)} {tx_hash}")
            if record.get("status") != "FINALIZED":
                report["problems"].append(
                    f"emitted transfer {tx_hash}: explorer status is {record.get('status')!r}, not FINALIZED"
                )

    addresses = {"registry": (args.registry, True)}
    for key in ("child_A", "child_B"):
        if evidence.get(key):
            addresses[key] = (evidence[key], False)
    # A run may index more than two children (the integration suite does); any
    # ``{"child_n": address}`` map is covered by the same contract check.
    for key, address in (evidence.get("children") or {}).items():
        if address:
            addresses[key] = (address, False)
    print(f"[explorer] verifying {len(addresses)} contract addresses")
    local_source = Path(args.local_source)
    child_source = Path(args.child_source)
    for label, (address, require_pin) in addresses.items():
        counterpart = None
        if label == "registry":
            counterpart = local_source
        elif label.startswith("child"):
            # Every child was deployed by the factory from the embedded payload,
            # so each one should read back as exactly `BountyClaim.py`.
            counterpart = child_source
        try:
            record = check_address(address, require_pin, counterpart)
        except Exception as exc:  # noqa: BLE001
            record = {"address": address, "error": str(exc)}
            report["problems"].append(f"{label} {address}: {exc}")
        report["addresses"].append({"role": label, **record})
        print(f"  {label:<10} {record.get('explorer_type')} tx_count={record.get('tx_count')} "
              f"pin={str(record.get('deployed_runner_pin'))[:16]}.. {address}")
        if "deployed_source_equals_local_bytes" in record:
            exact = record["deployed_source_equals_local_bytes"]
            loose = record["deployed_source_equals_local_modulo_line_endings"]
            print(f"  {'':<10} deployed source vs {Path(record['local_source_file']).name}: "
                  f"bytes={exact}, modulo line endings={loose}")
            if not exact and not loose:
                report["problems"].append(
                    f"{label} {address}: the chain's source is not this repo's "
                    f"{record['local_source_file']} -- the README would be "
                    "describing a contract that is not in the tree")
        if record.get("explorer_type") != "CONTRACT":
            report["problems"].append(
                f"{label} {address}: explorer type is {record.get('explorer_type')!r}")
        if require_pin and not record.get("required_pin_seen"):
            report["problems"].append(
                f"{label} {address}: on-chain source has no pinned runner header")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    if report["problems"]:
        print("\n[explorer] PROBLEMS FOUND:")
        for problem in report["problems"]:
            print(f"  - {problem}")
        print(f"\n[explorer] report written to {out}")
        return 1
    print(
        f"\n[explorer] all {len(hashes)} transactions FINALIZED, all addresses are contracts.")
    print(f"[explorer] report written to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
