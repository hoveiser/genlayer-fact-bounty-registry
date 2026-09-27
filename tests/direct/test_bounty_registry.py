"""Direct Mode tests for the `BountyRegistry` factory contract.

Scope note (see ``conftest.py``): Direct Mode runs the leader only and services
cross-contract operations through the test-installed ``CrossContractBus``. What
these tests *do* prove is the factory machinery itself -- that ``gl.deploy_contract``
is really invoked with the child source, the locked value and the right constructor
arguments, that the CREATE2 address comes back and lands in the ``TreeMap`` index,
and that the parent's aggregation view reads its children synchronously. What they
cannot prove is that a second party agrees with the derived verdict; that is the
studionet integration test's job.
"""

from __future__ import annotations

import hashlib

import pytest

from conftest import (
    CLAIM_REPO,
    CLAIM_THRESHOLD,
    DEADLINE,
    REWARD_ATTO,
    REGISTRY_SOURCE,
    make_address,
)

pytestmark = pytest.mark.direct

SOURCE_URL = f"https://api.github.com/repos/{CLAIM_REPO}"


@pytest.fixture
def registry(direct_vm, direct_deploy):
    """A freshly deployed registry owned by its deployer."""
    owner = make_address("registry-owner")
    direct_vm.sender = owner
    return direct_deploy(str(REGISTRY_SOURCE))


@pytest.fixture
def owner(direct_vm):
    """The address ``registry`` was deployed by."""
    return direct_vm.sender


def _child_address_of(vm, nonce: int) -> str:
    """The CREATE2 address the SDK predicts for `nonce` under this registry."""
    import sys

    from genlayer.py._internal import create2_address

    gl = sys.modules["genlayer.gl"]
    return create2_address(
        gl.message.contract_address, nonce, gl.message.chain_id
    ).as_hex


# ---------------------------------------------------------------------------
# The factory primitive
# ---------------------------------------------------------------------------
def test_create_bounty_deploys_a_child_with_the_claim_and_the_locked_value(
    registry, direct_vm, bus, owner
):
    direct_vm.value = REWARD_ATTO
    child = registry.create_bounty("b1", CLAIM_REPO, CLAIM_THRESHOLD, DEADLINE)

    assert len(bus.deploys) == 1
    deployment = bus.deploys[0]
    # The escrow is the value carried by the creation call itself.
    assert int(deployment["value"]) == REWARD_ATTO
    assert int(deployment["salt_nonce"]) == 1
    assert deployment["on"] == "finalized"
    # The child source is the compiled body of contracts/BountyClaim.py.
    assert deployment["code"].startswith(b'# { "Depends": "py-genlayer:')

    args = deployment["calldata"]["args"]
    assert args[0] == "b1"
    assert args[1] == CLAIM_REPO
    assert int(args[2]) == CLAIM_THRESHOLD
    assert args[3] == DEADLINE
    # Poster and reward reference come from the message, not from the caller.
    assert hex_of(args[4]) == owner.as_hex
    assert int(args[5]) == REWARD_ATTO
    assert hex_of(args[6]) == addr_of(direct_vm)

    # The returned address is the SDK's client-side CREATE2 prediction.
    assert child.as_hex == _child_address_of(direct_vm, 1)


def test_create_bounty_indexes_the_child_and_enumerates_it(registry, direct_vm, bus):
    direct_vm.value = REWARD_ATTO
    first = registry.create_bounty("b1", CLAIM_REPO, CLAIM_THRESHOLD, DEADLINE)
    direct_vm.value = REWARD_ATTO * 2
    second = registry.create_bounty("b2", CLAIM_REPO, 5000, DEADLINE)

    listing = registry.list_bounties()
    assert listing["count"] == 2
    assert listing["total_created"] == 2
    assert [entry["bounty_id"]
            for entry in listing["bounties"]] == ["b1", "b2"]
    assert [entry["address"] for entry in listing["bounties"]] == [
        first.as_hex,
        second.as_hex,
    ]
    # Distinct salt nonces -> distinct deterministic addresses.
    assert first.as_hex != second.as_hex
    assert second.as_hex == _child_address_of(direct_vm, 2)
    assert registry.get_bounty_address("b1") == first.as_hex


def test_create_bounty_refuses_a_call_that_locks_nothing(registry, direct_vm):
    """The payable decorator is worthless if a zero-value call could open a bounty."""
    direct_vm.value = 0
    with direct_vm.expect_revert("must attach a positive GEN reward"):
        registry.create_bounty("b1", CLAIM_REPO, CLAIM_THRESHOLD, DEADLINE)


def test_create_bounty_is_declared_payable_and_its_siblings_are_not(direct_vm):
    """Direct mode never enforces `payable`, so assert on the decorator itself.

    ``gl.public.write.payable`` stamps ``__gl_payable__`` on the method, which is
    what the schema generator reads to advertise the method as payable and what
    the real runner checks before refusing a value-bearing call
    (``_genlayer_runner.py``: "called non-payable method ... with non-zero
    value"). A plain ``gl.public.write`` must not carry it.
    """
    from pathlib import Path

    from gltest.direct.loader import load_contract_class

    source = Path(REGISTRY_SOURCE)
    direct_vm._contract_address = hashlib.sha256(
        str(source).encode()).digest()[:20]
    contract_cls = load_contract_class(source, direct_vm, None)

    assert getattr(contract_cls.create_bounty, "__gl_payable__", False) is True
    assert getattr(contract_cls.set_owner, "__gl_payable__", False) is False
    # The views are public and read-only, so they can never carry value either.
    for view in ("list_bounties", "get_bounty_status", "aggregate_statuses"):
        method = getattr(contract_cls, view)
        assert getattr(method, "__gl_public__", False) is True
        assert getattr(method, "__gl_readonly__", None) is True
        assert getattr(method, "__gl_payable__", False) is False


@pytest.mark.parametrize(
    "bounty_id, needle",
    [
        ("", "must not be empty"),
        ("  ", "must not be empty"),
        ("has space", "must not contain spaces"),
    ],
)
def test_create_bounty_rejects_unusable_identifiers(
    registry, direct_vm, bounty_id, needle
):
    direct_vm.value = REWARD_ATTO
    with direct_vm.expect_revert(needle):
        registry.create_bounty(bounty_id, CLAIM_REPO,
                               CLAIM_THRESHOLD, DEADLINE)


def test_bounty_ids_are_unique(registry, direct_vm, bus):
    direct_vm.value = REWARD_ATTO
    registry.create_bounty("b1", CLAIM_REPO, CLAIM_THRESHOLD, DEADLINE)
    with direct_vm.expect_revert("already exists"):
        registry.create_bounty("b1", CLAIM_REPO, CLAIM_THRESHOLD, DEADLINE)

    assert registry.list_bounties()["count"] == 1
    assert len(bus.deploys) == 1


def test_get_bounty_address_rejects_an_unknown_id(registry, direct_vm):
    with direct_vm.expect_revert("unknown bounty_id 'nope'"):
        registry.get_bounty_address("nope")


# ---------------------------------------------------------------------------
# Parent reads children
# ---------------------------------------------------------------------------
def _status_payload(bounty_id: str, state: str, escrow: int) -> dict:
    return {
        "bounty_id": bounty_id,
        "status": state,
        "claim": CLAIM_REPO,
        "threshold": CLAIM_THRESHOLD,
        "source_url": SOURCE_URL,
        "reward_atto": escrow,
        "poster": make_address("registry-owner").as_hex,
        "reporter": "",
        "reported_verdict": "",
        "truth_verdict": "",
        "fact_check_reachable": False,
        "fact_check_repo_ok": False,
        "fact_check_met": False,
        "evidence": "",
        "created_at": "2026-01-01T00:00:00",
        "deadline_at": DEADLINE,
        "escrow_atto": escrow,
    }


def test_get_bounty_status_delegates_the_read_to_the_child(
    registry, direct_vm, bus
):
    direct_vm.value = REWARD_ATTO
    child = registry.create_bounty("b1", CLAIM_REPO, CLAIM_THRESHOLD, DEADLINE)
    bus.serve_view(child, _status_payload("b1", "OPEN", REWARD_ATTO))

    status = registry.get_bounty_status("b1")

    assert status["bounty_id"] == "b1"
    assert status["status"] == "OPEN"
    assert status["escrow_atto"] == REWARD_ATTO


def test_aggregate_statuses_reads_every_child_synchronously(
    registry, direct_vm, bus
):
    """The roll-up comes from the children, not from a copy the parent maintains."""
    direct_vm.value = REWARD_ATTO
    first = registry.create_bounty("b1", CLAIM_REPO, CLAIM_THRESHOLD, DEADLINE)
    direct_vm.value = REWARD_ATTO * 2
    second = registry.create_bounty(
        "b2", CLAIM_REPO, CLAIM_THRESHOLD, DEADLINE)
    direct_vm.value = REWARD_ATTO
    third = registry.create_bounty("b3", CLAIM_REPO, CLAIM_THRESHOLD, DEADLINE)

    bus.serve_view(first, _status_payload("b1", "PAID", 0))
    bus.serve_view(second, _status_payload("b2", "REPORTED", REWARD_ATTO * 2))
    bus.serve_view(third, _status_payload("b3", "OPEN", REWARD_ATTO))

    aggregate = registry.aggregate_statuses()

    assert aggregate["bounty_count"] == 3
    # 0 (paid out) + 2 GEN (reported) + 1 GEN (open)
    assert aggregate["escrow_total_atto"] == REWARD_ATTO * 3
    assert aggregate["by_status"] == {"PAID": 1, "REPORTED": 1, "OPEN": 1}
    assert [entry["bounty_id"] for entry in aggregate["bounties"]] == [
        "b1",
        "b2",
        "b3",
    ]
    assert aggregate["bounties"][0]["address"] == first.as_hex
    assert aggregate["bounties"][1]["escrow_atto"] == REWARD_ATTO * 2


def test_aggregate_statuses_reports_unreadable_children_instead_of_hiding_them(
    registry, direct_vm, bus
):
    direct_vm.value = REWARD_ATTO
    first = registry.create_bounty("b1", CLAIM_REPO, CLAIM_THRESHOLD, DEADLINE)
    second = registry.create_bounty(
        "b2", CLAIM_REPO, CLAIM_THRESHOLD, DEADLINE)

    bus.serve_view(first, _status_payload("b1", "OPEN", REWARD_ATTO))
    bus.fail_view(second, "child not finalized yet")

    aggregate = registry.aggregate_statuses()

    assert aggregate["bounty_count"] == 2
    assert aggregate["by_status"] == {"OPEN": 1, "UNREADABLE": 1}
    # The unreadable bounty is still counted, just not silently given a verdict.
    assert [entry["bounty_id"] for entry in aggregate["bounties"]] == ["b1"]


def test_aggregate_statuses_of_an_empty_registry(registry):
    aggregate = registry.aggregate_statuses()
    assert aggregate == {
        "bounty_count": 0,
        "escrow_total_atto": 0,
        "by_status": {},
        "bounties": [],
    }


# ---------------------------------------------------------------------------
# Administrative authz
# ---------------------------------------------------------------------------
def test_only_the_owner_may_reassign_ownership(registry, direct_vm):
    stranger = make_address("stranger")
    direct_vm.sender = stranger
    with direct_vm.expect_revert("only the owner may reassign ownership"):
        registry.set_owner(stranger)


def test_ownership_transfer_takes_effect(registry, direct_vm, owner):
    successor = make_address("successor")
    direct_vm.sender = owner
    registry.set_owner(successor)

    # The previous owner has lost the privilege, and no stray value was minted:
    # set_owner is a plain write, so a payment to it is not part of the design.
    direct_vm.sender = owner
    with direct_vm.expect_revert("only the owner may reassign ownership"):
        registry.set_owner(owner)

    direct_vm.sender = successor
    registry.set_owner(make_address("third"))


# ---------------------------------------------------------------------------
# Layout / dependency guards
# ---------------------------------------------------------------------------
def test_embedded_child_source_still_matches_the_canonical_file():
    """The factory ships the child as bytes, so pin those bytes to the real file.

    ``scripts/build_bundle.py`` regenerates the block from ``contracts/BountyClaim.py``;
    this test is what makes "forgetting to re-run it" a failure instead of a
    registry that deploys a stale child.
    """
    import re

    from conftest import CLAIM_SOURCE

    text = REGISTRY_SOURCE.read_text(encoding="utf-8")
    match = re.search(
        r"_CHILD_SOURCE_HEX = \((.*?)\)\n# --- END EMBEDDED CHILD SOURCE",
        text,
        re.S,
    )
    assert match is not None, "embedded child source block is missing"

    hex_payload = "".join(re.findall(r'"([0-9a-fA-F]*)"', match.group(1)))
    embedded = bytes.fromhex(hex_payload)

    assert embedded == CLAIM_SOURCE.read_bytes()
    assert embedded.splitlines(
    )[0] == CLAIM_SOURCE.read_bytes().splitlines()[0]


def test_both_contracts_pin_the_runner_by_hash_and_never_by_tag():
    """Hard requirement 1, enforced by test rather than by review."""
    import re
    from conftest import CLAIM_SOURCE

    pattern = re.compile(
        r'^# \{ "Depends": "py-genlayer:(?P<ref>[0-9a-z]{40,})" \}$'
    )
    for path in (CLAIM_SOURCE, REGISTRY_SOURCE):
        first_line = path.read_text(encoding="utf-8").splitlines()[0]
        match = pattern.match(first_line)
        assert match is not None, f"{path.name} line 1 is not a pinned header: {first_line}"
        assert match.group("ref") not in {"latest", "test"}


def test_neither_contract_uses_a_banned_pattern():
    """Guards for the rest of the hard requirements.

    No Enum in storage, no float for money, no strict equality on raw web output
    (the fact check compares derived verdicts), and no bare Python exception --
    those surface as unclassified VM errors instead of a revert reason.

    The checks are regex-driven, run against comment-stripped source, and
    deliberately narrow: the contracts' own comments say words like
    "Enumeration" and "does not support Enum", so scanning prose would flag the
    very documentation that explains why those patterns are absent.
    """
    import re

    from conftest import CLAIM_SOURCE

    banned = {
        "enum import": r"^\s*(from|import)\s+\S*enum",
        "enum in storage": r"\bEnum\b\s*[(.:]",
        "float annotation": r":\s*float\b",
        "float() cast": r"\bfloat\s*\(",
        "strict_eq": r"\bstrict_eq\b",
        "gl.eq comparator": r"\bgl\.eq\b",
        "bare Exception raise": r"raise\s+(Exception|ValueError|RuntimeError|TypeError)\b",
    }
    for path in (CLAIM_SOURCE, REGISTRY_SOURCE):
        code = re.sub(r"#[^\n]*", "", path.read_text(encoding="utf-8"))
        for label, pattern in banned.items():
            found = re.search(pattern, code, re.M)
            assert found is None, f"{path.name} uses {label}: {found.group(0)!r}"


def addr_of(vm) -> str:
    """Checksum hex of the address the registry itself is deployed at."""
    from conftest import addr_hex

    return addr_hex(bytes(vm._contract_address))


def hex_of(value) -> str:
    """Checksum hex of a calldata-decoded address argument (Address or hex str)."""
    as_hex = getattr(value, "as_hex", None)
    return str(as_hex if as_hex is not None else value)
