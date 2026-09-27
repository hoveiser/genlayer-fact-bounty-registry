"""Shared harness for Direct Mode tests.

The plain constants, address helpers and the ``CrossContractBus`` live in
``direct_harness.py`` (imported below); only pytest hooks and fixtures stay
here. That split exists because pytest registers a non-package ``conftest.py``
in ``sys.modules`` under the bare name ``conftest``, which the integration
directory also uses -- see the note at the top of ``direct_harness.py``.

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
   a hook the call silently reports failure. ``CrossContractBus`` is the minimal
   hook: it records value transfers (both flavours), records child deployments,
   and answers ``CallContract`` view reads from a test-supplied table.

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
from typing import Any, List, Optional

import pytest

from direct_harness import (
    CLAIM_REPO,
    CLAIM_SOURCE,
    CLAIM_THRESHOLD,
    DEADLINE,
    POSTER_SEED,
    REGISTRY_SEED,
    REWARD_ATTO,
    CrossContractBus,
    make_address,
)

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
