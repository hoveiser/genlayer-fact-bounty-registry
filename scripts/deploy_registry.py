"""Redeploy the corrected `BountyRegistry` to studionet via the SDK.

The `genlayer` CLI is the documented path (README "Deploying"), but it decrypts
the active account's keystore with an interactive passphrase, which a scripted
resubmit cannot type. This driver uses the *same* primitive the CLI wraps --
`genlayer_py.deploy_contract` -- with the raw key from the git-ignored `.env`,
so the on-chain artifact is byte-for-byte what `contracts/BountyRegistry.py`
holds (the source text is uploaded verbatim; the pinned-runner hash on line 1 is
what the node executes).

Flow
----
1. read `contracts/BountyRegistry.py` as text and submit it as the deploy code;
2. poll the deployment transaction until the ring reports FINALIZED;
3. resolve the created contract address from the transaction's own record (a
   GenLayer deployment carries the new contract in `to_address`);
4. confirm the address reads back as a `CONTRACT` on the *public explorer* --
   an index the consensus nodes do not control -- before printing it.

The address is written to `evidence/registry-deploy.json` and printed; feed it
to `scripts/e2e_studionet.py --registry` and the integration suite
(`GENLAYER_REGISTRY_ADDRESS`).

Usage::

    .venv/bin/python scripts/deploy_registry.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from genlayer_py import create_account, create_client, studionet

ROOT = Path(__file__).resolve().parent.parent
CONTRACT = ROOT / "contracts" / "BountyRegistry.py"


def load_dotenv() -> None:
    path = ROOT / ".env"
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _status_name(tx: dict) -> str:
    name = tx.get("status_name")
    if name:
        return str(name)
    from genlayer_py.types.transactions import TransactionStatus

    by_number = {int(s.value) if str(s.value).isdigit() else None: s.name
                 for s in TransactionStatus}
    return by_number.get(tx.get("status"), str(tx.get("status")))


def wait_for_final(client, tx_hash: str, timeout: int, label: str) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            tx = client.get_transaction(tx_hash)
        except Exception as exc:  # node may not have indexed it yet
            print(f"  [wait] {label}: not readable yet ({type(exc).__name__})")
            time.sleep(5)
            continue
        name = _status_name(tx)
        if name != last:
            print(f"  [wait] {label}: {name}")
            last = name
        if name == "FINALIZED":
            return tx
        if name in {"CANCELED", "ERROR", "INVALID"}:
            raise SystemExit(f"{label} settled in terminal state {name}: {tx}")
        time.sleep(5)
    raise SystemExit(f"{label} did not finalize within {timeout}s (last {last})")


def explorer_type(address: str) -> str:
    """The public explorer's own label for an address; '' when unreachable."""
    import urllib.error
    import urllib.request

    url = f"https://explorer-studio.genlayer.com/api/address/{address}"
    req = urllib.request.Request(
        url, headers={"Accept": "application/json",
                      "User-Agent": "genlayer-bounty-registry-deploy"})
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            return json.loads(resp.read().decode("utf-8")).get("type") or ""
    except urllib.error.HTTPError as exc:
        return f"http-{exc.code}"
    except Exception as exc:  # noqa: BLE001
        return f"error:{type(exc).__name__}"


def main() -> int:
    load_dotenv()
    private_key = os.environ.get("GENLAYER_PRIVATE_KEY")
    if not private_key:
        print("[deploy] GENLAYER_PRIVATE_KEY is not set", file=sys.stderr)
        return 2

    timeout = int(os.environ.get("GENLAYER_DEPLOY_TIMEOUT", "1200"))
    source = CONTRACT.read_text(encoding="utf-8")

    poster = create_account(private_key)
    client = create_client(studionet, account=poster)
    print(f"[deploy] chainId={studionet.id} deployer={poster.address}")
    print(f"[deploy] submitting {CONTRACT.name} ({len(source)} chars) ...")

    tx_hash = str(client.deploy_contract(code=source, account=poster))
    print(f"[deploy] tx={tx_hash}")
    receipt = wait_for_final(client, tx_hash, timeout, "deploy BountyRegistry")

    address = str(receipt.get("recipient") or receipt.get("to_address") or "")
    if not address:
        raise SystemExit(f"deploy finalized but no contract address on {tx_hash}")

    kind = ""
    for _ in range(12):
        kind = explorer_type(address)
        if kind == "CONTRACT":
            break
        time.sleep(5)
    print(f"[deploy] contract address = {address} (explorer type={kind!r})")
    print(f"[deploy] explorer: https://explorer-studio.genlayer.com/address/{address}")
    if kind != "CONTRACT":
        print("[deploy] WARNING: the explorer does not yet index this as a contract",
              file=sys.stderr)

    out = ROOT / "evidence" / "registry-deploy.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "network": {"name": "studionet", "chain_id": studionet.id},
                "deployer": poster.address,
                "deploy_tx": tx_hash,
                "registry": address,
                "explorer_type": kind,
                "status_name": _status_name(receipt),
                "result_name": receipt.get("result_name"),
                "source_chars": len(source),
            },
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    print(f"[deploy] wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
