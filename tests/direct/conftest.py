"""Shared harness for Direct Mode tests.

Direct Mode (``gltest.direct``) executes the real pinned GenLayer Python runner
in-process: the actual contract source, the real ``calldata`` codec, the real
``TreeMap``/``DynArray`` storage implementation and the real
``gl.vm.run_nondet_unsafe`` boundary. What it does NOT do is run consensus.

Several things differ from a live network and are worked around here:

1. **Only the leader runs.** ``run_nondet_unsafe`` in direct mode calls
   ``leader_fn()`` and merely *captures* ``validator_fn`` for the test to drive
   by hand. Real leader/validator agreement is therefore NOT exercised by these
   tests -- see ``tests/integration/`` for that. ``vm.run_validator()`` pushes a
   hand-made leader result through the validator, which is what the
   disagreement tests do.

2. **No cross-contract support.** ``DeployContract`` / ``CallContract`` /
   ``PostMessage`` / ``EthSend`` are only serviced when a ``vm._gl_call_hook`` is
   installed, and direct mode never installs one (it is a glsim feature). Without
   a hook the call silently reports failure. ``CrossContractBus`` below is a
   minimal hook: it records value transfers (both flavours), records child
   deployments, and answers ``CallContract`` view reads from a test-supplied
   table.

3. **``vm.warp()`` does not reach the contract.** The SDK caches
   ``gl.message_raw`` once, at import time, and
   ``VMContext._refresh_gl_message()`` only refreshes ``sender_address`` /
   ``origin_address`` / ``value`` -- never ``datetime``. So ``warp_before_deploy``
   must run before the contract module is imported, and ``set_datetime`` patches
   the live dict directly for time travel inside a test.

4. **Direct Mode cannot deploy on Windows as shipped.**
   ``gltest.direct.loader._inject_message_to_fd0`` writes the message to a temp
   file, ``dup2``s it onto stdin, then ``os.unlink``s it -- which raises
   ``WinError 32`` because fd 0 still holds the file open. ``_defer_stdin_unlink``
   below postpones the deletion to the end of the session; without it not a
   single contract can be deployed on this platform.

5. **Cross-contract value transfer is not implemented at all.** The VM's own
   dispatcher answers neither ``PostMessage`` (internal IC -> IC transfer, trace
   ``Unknown gl_call request type: ['PostMessage']``) nor ``EthSend`` (external
   IC -> chain-layer transfer, same trace shape), so a payout or a reclaim is
   observable only through the recorded transfer, not through
   ``contract.balance``. Tests assert on the emission and say so; only
   ``tests/integration/`` can assert that money actually moved.

Also note that ``genlayer.gl.genvm_contracts`` keeps a module-global "only one
Contract subclass" guard, and direct mode evicts the SDK from ``sys.modules``
after every test -- so exactly one contract may be deployed per test. The
registry and a real child therefore cannot share one direct VM, and
``aggregate_statuses`` is exercised through the ``CrossContractBus`` instead.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

#: message temp files Windows would not let the harness delete mid-run
_PENDING_STDIN_TEMPS: List[str] = []


def _defer_stdin_unlink() -> None:
    """Work around ``WinError 32`` in ``gltest.direct.loader`` (see module doc)."""
    if sys.platform != "win32":
        return
    from gltest.direct import loader

    if getattr(loader, "_bounty_registry_unlink_patch", False):
        return
    original = loader._inject_message_to_fd0

    def patched(vm: Any) -> None:
        real_unlink = os.unlink

        def deferred(path: Any) -> None:
            _PENDING_STDIN_TEMPS.append(path)

        os.unlink = deferred  # type: ignore[assignment]
        try:
            original(vm)
        finally:
            os.unlink = real_unlink  # type: ignore[assignment]

    loader._inject_message_to_fd0 = patched
    loader._bounty_registry_unlink_patch = True


_defer_stdin_unlink()


def pytest_unconfigure(config: Any) -> None:
    """Delete the deferred message temp files once stdin has been restored."""
    for path in _PENDING_STDIN_TEMPS:
        try:
            os.unlink(path)
        except OSError:
            pass
    _PENDING_STDIN_TEMPS.clear()


REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACTS_DIR = REPO_ROOT / "contracts"
CLAIM_SOURCE = CONTRACTS_DIR / "BountyClaim.py"
REGISTRY_SOURCE = CONTRACTS_DIR / "BountyRegistry.py"

# studionet emits `gl.message_raw["datetime"]` as
# "2026-09-26T20:06:34.271991Z"; direct mode's default has the same shape.
CREATED_AT = "2026-01-01T00:00:00.000000Z"
DEADLINE = "2026-01-02T00:00:00"
AFTER_DEADLINE = "2026-01-03T00:00:00.000000Z"
BEFORE_DEADLINE = "2026-01-01T12:00:00.000000Z"

# GEN has 18 decimals, so every amount here is a plain atto-scale u256 integer.
REWARD_ATTO = 5_000_000_000_000_000_000  # 5 GEN
CLAIM_REPO = "genlayerlabs/genlayer"
CLAIM_THRESHOLD = 1000

POSTER_SEED = "poster"
REGISTRY_SEED = "registry"


def addr_bytes(value: Any) -> bytes:
    """Normalise anything address-shaped to 20 raw bytes."""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    as_bytes = getattr(value, "as_bytes", None)
    if as_bytes is not None:
        return bytes(as_bytes)
    if isinstance(value, str):
        return bytes.fromhex(value[2:] if value.startswith("0x") else value)
    raise TypeError(f"cannot interpret {value!r} as an address")


def addr_hex(value: Any) -> str:
    from eth_utils import to_checksum_address

    return to_checksum_address("0x" + addr_bytes(value).hex())


class TestAddress:
    """Actor address usable *before* the pinned runner is importable.

    ``gltest.direct.loader.create_address`` falls back to raw ``bytes`` whenever
    ``genlayer.py.types`` cannot be imported -- and it cannot, at fixture-setup
    time, because the harness only puts the runner on ``sys.path`` inside
    ``deploy_contract`` and strips it again after every test. Plain bytes are
    rejected twice over: ``deploy_contract`` round-trips constructor arguments
    through ``calldata`` (whose ``Encodable`` union accepts ``Address`` but not
    bytes), and the storage descriptor calls ``val.as_bytes`` on write. This
    stand-in therefore exposes exactly the surface the harness needs --
    ``as_bytes`` for storage, ``as_hex`` for the checksummed string a contract
    returns -- and is only used when the runner genuinely cannot be primed.
    """

    __slots__ = ("as_bytes", "_as_hex")

    def __init__(self, raw: bytes) -> None:
        if len(raw) != 20:
            raise ValueError(f"address must be 20 bytes, got {len(raw)}")
        self.as_bytes = bytes(raw)
        self._as_hex: Optional[str] = None

    @property
    def as_hex(self) -> str:
        if self._as_hex is None:
            self._as_hex = addr_hex(self.as_bytes)
        return self._as_hex

    def __eq__(self, other: Any) -> bool:
        try:
            return self.as_bytes == addr_bytes(other)
        except TypeError:
            return NotImplemented

    def __hash__(self) -> int:
        return hash(self.as_bytes)

    def __str__(self) -> str:
        return self.as_hex

    def __repr__(self) -> str:
        return f"TestAddress({self.as_hex!r})"


def address_cls() -> Any:
    """The pinned runner's real ``Address`` class, priming the SDK if needed.

    Prime it ourselves because the harness only adds the runner to ``sys.path``
    inside ``deploy_contract`` and strips it again on teardown. Importing
    ``genlayer`` early is safe: the package exposes ``gl`` as a lazy proxy, so
    nothing reads the message from stdin until the harness injects it at deploy
    time -- and ``calldata``'s ``Encodable`` union matches on the live class,
    which is why the currently imported one has to be used.
    """
    module = sys.modules.get("genlayer.py.types")
    if module is None:
        from gltest.direct import wasi_mock
        from gltest.direct.sdk_loader import setup_sdk_paths

        sys.modules.setdefault("_genlayer_wasi", wasi_mock)
        setup_sdk_paths(CLAIM_SOURCE, None)
        import genlayer.py.types as module  # type: ignore[no-redef]
    return module.Address


def make_address(seed: str) -> Any:
    """A deterministic address for use as a test actor."""
    import hashlib

    raw = hashlib.sha256(seed.encode()).digest()[:20]
    try:
        return address_cls()(raw)
    except Exception:  # pragma: no cover - runner unavailable
        return TestAddress(raw)


def warp_before_deploy(vm: Any, timestamp: str) -> None:
    """Set the transaction timestamp *before* the contract module is imported.

    ``gl.message_raw`` is decoded from stdin exactly once at
    ``import genlayer.gl`` time, so a deploy-time stamp has to be in place
    before that happens.
    """
    vm.warp(timestamp)


def set_datetime(vm: Any, timestamp: str) -> None:
    """Time-travel mid-test, working around direct mode not refreshing datetime."""
    import sys

    vm.warp(timestamp)
    gl = sys.modules.get("genlayer.gl")
    if gl is not None and getattr(gl, "message_raw", None) is not None:
        gl.message_raw["datetime"] = timestamp


class CrossContractBus:
    """Stand-in for GenVM's cross-contract dispatcher in Direct Mode.

    Install it on a ``VMContext`` and it observes the three operations direct
    mode otherwise drops on the floor.
    """

    def __init__(self) -> None:
        self.transfers: List[Tuple[str, int]] = []
        self.deploys: List[Dict[str, Any]] = []
        #: address hex -> payload returned for a ``CallContract`` read
        self.view_payloads: Dict[str, Any] = {}
        #: address hex -> error text (simulates an unreadable child)
        self.view_errors: Dict[str, str] = {}

    def install(self, vm: Any) -> "CrossContractBus":
        vm._gl_call_hook = self._hook
        return self

    def serve_view(self, address: Any, payload: Any) -> None:
        self.view_payloads[addr_hex(address)] = payload

    def fail_view(self, address: Any, message: str = "no such contract") -> None:
        self.view_errors[addr_hex(address)] = message

    # -- the hook proper ------------------------------------------------
    def _hook(self, vm: Any, request: Dict[str, Any]) -> Optional[bytes]:
        from genlayer.py import calldata

        if "PostMessage" in request:
            post = request["PostMessage"]
            self.transfers.append(
                (addr_hex(post["address"]), int(post["value"])))
            # A plain transfer calls no method, so there is nothing to return.
            return None

        if "EthSend" in request:
            send = request["EthSend"]
            self.transfers.append(
                (addr_hex(send["address"]), int(send.get("value") or 0)))
            # ResultCode.RETURN: the SDK decodes this call's result with
            # `lambda _x: None`, so an empty success payload is all it needs.
            return b"\x00"

        if "DeployContract" in request:
            self.deploys.append(request["DeployContract"])
            return None

        if "CallContract" in request:
            call = request["CallContract"]
            key = addr_hex(call["address"])
            if key in self.view_errors:
                # ResultCode.USER_ERROR -> the SDK raises gl.vm.UserError here.
                return b"\x01" + self.view_errors[key].encode("utf-8")
            if key not in self.view_payloads:
                return b"\x02" + f"no contract at {key}".encode("utf-8")
            # ResultCode.RETURN -> the SDK hands the decoded payload straight back.
            return b"\x00" + calldata.encode(self.view_payloads[key])

        return None

    # -- conveniences for assertions -----------------------------------
    def total_sent(self, to: Any) -> int:
        wanted = addr_hex(to)
        return sum(value for address, value in self.transfers if address == wanted)


@pytest.fixture
def bus(direct_vm) -> CrossContractBus:
    """A cross-contract hook installed on the active direct VM."""
    return CrossContractBus().install(direct_vm)


@pytest.fixture
def deploy_claim(direct_vm, direct_deploy):
    """Deploy a `BountyClaim` and fund it with the escrow it guards.

    Direct mode has no factory, so the child is deployed by the test and the
    escrow is granted with the ``deal`` cheatcode -- the same state
    `BountyRegistry.create_bounty` leaves behind on a real network.
    """
    poster = make_address(POSTER_SEED)
    registry = make_address(REGISTRY_SEED)

    def _deploy(
        *,
        bounty_id: str = "bounty-1",
        repo: str = CLAIM_REPO,
        threshold: int = CLAIM_THRESHOLD,
        reward: int = REWARD_ATTO,
        deadline_at: str = DEADLINE,
        as_poster: Any = None,
        from_registry: Any = None,
    ):
        actor = as_poster if as_poster is not None else poster
        direct_vm.sender = actor
        direct_vm.value = reward
        contract = direct_deploy(
            str(CLAIM_SOURCE),
            bounty_id,
            repo,
            threshold,
            deadline_at,
            actor,
            reward,
            from_registry if from_registry is not None else registry,
        )
        direct_vm.deal(direct_vm._contract_address, reward)
        return contract

    return _deploy


@pytest.fixture
def claim(deploy_claim):
    """The default bounty, ready to be reported on: a 1000-star threshold."""
    return deploy_claim()


@pytest.fixture
def poster():
    return make_address(POSTER_SEED)


@pytest.fixture
def reporter():
    return make_address("reporter")


@pytest.fixture
def github_repo_body():
    """Builder for the upstream payload the contract reads."""

    def _body(full_name: str, stargazers_count: int) -> str:
        import json

        # Volatile fields are present on purpose: the contract must ignore them.
        return json.dumps(
            {
                "full_name": full_name,
                "stargazers_count": stargazers_count,
                "updated_at": "2026-09-26T20:06:34Z",
                "watchers_count": 12345,
                "forks_count": 999,
                "description": "anything at all",
            }
        )

    return _body


@pytest.fixture
def serve_github(direct_vm, github_repo_body):
    """Mock the exact source URL `BountyClaim` builds for a repository.

    *repo* decides the URL that gets served; *served_as* decides the
    ``full_name`` inside the payload. Splitting them is how a test simulates a
    source that answers but describes some other repository.
    """

    def _serve(
        repo: str,
        stars: int,
        status: int = 200,
        body: Optional[str] = None,
        served_as: Optional[str] = None,
    ) -> str:
        url = f"https://api.github.com/repos/{repo.lower()}"
        if body is None:
            body = github_repo_body(
                served_as or repo, stars) if status == 200 else ""
        direct_vm.mock_web(
            url, {"method": "GET", "status": status, "body": body})
        return url

    return _serve
