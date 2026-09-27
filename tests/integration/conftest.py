"""Fixtures for the live studionet integration suite.

These tests are marked ``integration`` and therefore excluded by default
(``addopts = -m "not integration"`` in pytest.ini). Run them with::

    py -3.12 -m pytest tests/integration -m integration -v -s

Requirements:
  * ``GENLAYER_PRIVATE_KEY`` in the environment or in the repo-root ``.env``
    (a funded studionet account -- studionet GEN from the faucet).
  * a deployed ``BountyRegistry``, address given by ``GENLAYER_REGISTRY_ADDRESS``
    (falls back to the deployment recorded in README.md).
  * outbound access to the studionet RPC. Nothing is mocked: every assertion
    below is about a transaction that a real leader and five real validators
    executed and agreed on.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from genlayer_py import create_account, create_client, studionet

ROOT = Path(__file__).resolve().parents[2]
ATTO_PER_GEN = 10**18

# The single checkable claim type this contract family settles.
CLAIM_REPO = "genlayerlabs/skills"
CLAIM_THRESHOLD = 10
SOURCE_URL = f"https://api.github.com/repos/{CLAIM_REPO}"

# Deadlines are plain UTC ISO-8601 strings; the contract compares the
# fixed-width 'YYYY-MM-DDTHH:MM:SS' prefix lexicographically.
FAR_FUTURE = "2099-12-31T23:59:59"

DEFAULT_REGISTRY = "0x116DE8851D2101D583b84EBFEEadc9d2f10Ae012"


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


def _hex_key(raw) -> str:
    if isinstance(raw, str):
        return raw if raw.startswith("0x") else "0x" + raw
    return "0x" + bytes(raw).hex()


@pytest.fixture(scope="session")
def private_key() -> str:
    _load_dotenv()
    key = os.environ.get("GENLAYER_PRIVATE_KEY")
    if not key:
        pytest.skip(
            "GENLAYER_PRIVATE_KEY is not set -- studionet integration tests need it")
    return key


@pytest.fixture(scope="session")
def poster(private_key):
    return create_account(private_key)


@pytest.fixture(scope="session")
def reporter(poster, private_key):
    """A second EOA: the child contract refuses a report from the poster."""
    marker = ROOT / ".reporter_key"
    cached = os.environ.get("GENLAYER_REPORTER_PRIVATE_KEY")
    if not cached:
        cached = marker.read_text(
            encoding="utf-8").strip() if marker.is_file() else None
    if not cached:
        from eth_account import Account

        cached = _hex_key(Account.create().key)
        marker.write_text(cached, encoding="utf-8")
    account = create_account(cached)
    if account.address == poster.address:
        pytest.fail("reporter must not be the poster")
    return account


@pytest.fixture(scope="session")
def client(poster):
    return create_client(studionet, account=poster)


@pytest.fixture(scope="session")
def registry_address() -> str:
    _load_dotenv()
    return os.environ.get("GENLAYER_REGISTRY_ADDRESS", DEFAULT_REGISTRY)


@pytest.fixture(scope="session")
def consensus_timeout() -> int:
    return int(os.environ.get("GENLAYER_CONSENSUS_TIMEOUT", "1200"))


@pytest.fixture(scope="session")
def settle(client, consensus_timeout):
    """Poll a transaction id until the ring reaches FINALIZED.

    ACCEPTED is *not* good enough here: a child contract is only deployed once
    the parent transaction finalizes (``gl.deploy_contract(..., on="finalized")``),
    and only finalized state is what the explorer shows.

    The ring's `status_name` is the authoritative field; the numeric `status` is
    only a fallback label because the SDK's TransactionStatus enum is keyed by
    string, not by the number the RPC returns.
    """

    def _settle(tx_hash: str, label: str = "") -> dict:
        deadline = time.time() + consensus_timeout
        last = None
        while time.time() < deadline:
            try:
                tx = client.get_transaction(str(tx_hash))
            except Exception:
                time.sleep(5)
                continue
            name = tx.get("status_name") or str(tx.get("status"))
            if name != last:
                print(f"  [{label or 'tx'}] {name}", flush=True)
                last = name
            if name == "FINALIZED":
                return tx
            if name in {"CANCELED", "ERROR", "INVALID"}:
                pytest.fail(f"{label} settled in {name}: {tx_hash}")
            time.sleep(5)
        pytest.fail(
            f"{label} never finalized within {consensus_timeout}s (last {last})")

    return _settle


@pytest.fixture(scope="session")
def bounty(client, registry_address, poster, settle, consensus_timeout):
    """Factory: create a real bounty on the deployed registry and return the child."""

    created: list = []

    def _create(repo: str, threshold: int, deadline: str, reward_atto: int) -> dict:
        bounty_id = f"it-{int(time.time() * 1000) % 10**9}"
        tx_hash = str(
            client.write_contract(
                registry_address,
                "create_bounty",
                account=poster,
                value=reward_atto,
                args=[bounty_id, repo, threshold, deadline],
            )
        )
        receipt = settle(tx_hash, f"create_bounty {bounty_id}")
        child = str(client.read_contract(registry_address,
                    "get_bounty_address", args=[bounty_id]))
        # The child is deployed `on="finalized"`, so its own deployment tx runs
        # only after the parent's finalizes; poll until it is actually readable.
        deadline = time.time() + consensus_timeout
        while True:
            try:
                client.read_contract(child, "get_status")
                break
            except Exception:
                if time.time() > deadline:
                    raise
                time.sleep(5)
        created.append(
            {"bounty_id": bounty_id, "child": child, "tx_hash": tx_hash})
        return {
            "bounty_id": bounty_id,
            "child": child,
            "create_tx": tx_hash,
            "create_receipt": receipt,
        }

    yield _create
    if created:
        print("\n[integration] bounty txs created by this run:")
        for entry in created:
            print(
                f"  {entry['bounty_id']} create={entry['tx_hash']} child={entry['child']}")
