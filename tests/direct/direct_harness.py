"""Shared constants and helpers for the Direct Mode suite.

This lives in a module of its own rather than in ``conftest.py`` on purpose:
both test directories used to be imported by their test modules as
``from conftest import ...``, and pytest registers a non-package ``conftest.py``
in ``sys.modules`` under exactly that bare name. When the whole suite is
collected from the root, the *integration* conftest is loaded too and wins the
name, so a late ``import conftest`` inside a direct test silently resolved to
the studionet harness and raised ``ImportError``. Unique module names remove
that ordering dependency; the fixtures stay in ``conftest.py`` because pytest
resolves those per directory anyway.

Direct Mode (``gltest.direct``) executes the real pinned GenLayer Python runner
in-process: the actual contract source, the real ``calldata`` codec, the real
``TreeMap``/``DynArray`` storage implementation and the real
``gl.vm.run_nondet_unsafe`` boundary. What it does NOT do is run consensus --
see ``tests/integration/`` for that.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACTS_DIR = REPO_ROOT / "contracts"
CLAIM_SOURCE = CONTRACTS_DIR / "BountyClaim.py"
REGISTRY_SOURCE = CONTRACTS_DIR / "BountyRegistry.py"

# studionet emits `gl.message_raw["datetime"]` as
# "2026-09-26T20:06:34.271991Z"; direct mode's default has the same shape.
# The un-warped default is the real wall clock, and the lifecycle is now
# time-gated on every edge (create_bounty needs a future deadline, reports
# need one that has not passed), so the fixtures pin the clock to CREATED_AT
# via ``set_datetime`` before letting a test interact with a bounty.
CREATED_AT = "2026-01-01T00:00:00.000000Z"
DEADLINE = "2026-01-02T00:00:00"
AFTER_DEADLINE = "2026-01-03T00:00:00.000000Z"
BEFORE_DEADLINE = "2026-01-01T12:00:00.000000Z"
PAST_DEADLINE = "2025-12-31T23:59:59"
# 19 characters long -- the old length-only format check accepted it -- but the
# separator sits where the 'T' belongs, so it cannot be compared chronologically.
MALFORMED_DEADLINE = "2026-01-02 00:00:00"

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
    import sys

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

    Direct mode never installs a ``_gl_call_hook`` (that is a glsim feature), so
    ``DeployContract`` / ``CallContract`` / ``PostMessage`` / ``EthSend`` fall
    through. Install this on a ``VMContext`` and it observes the operations
    direct mode otherwise drops on the floor.
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
