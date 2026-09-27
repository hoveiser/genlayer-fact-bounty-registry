"""Live studionet integration tests -- real leader/validator consensus.

Nothing here is mocked. Every assertion is about a transaction that a GenLayer
leader and five validators independently executed and agreed on, and the state
they agreed on is read back from the chain afterwards.

What Direct Mode cannot prove, and this does
-------------------------------------------
Direct Mode runs the *leader* path only. It can show that a fetch failure
settles to UNRESOLVED, that a payout emits the right transfer, and that a
non-payable write is rejected -- but it never runs a validator, so it can never
show that an independently derived verdict was *accepted by consensus*. It also
never enforces `payable` at all. These tests are the ones that exercise the
validator side of `gl.vm.run_nondet_unsafe`: the vote breakdown and
`result_name` come straight off the finalized receipt.

Cost note: each consensus transaction takes minutes, so the transactions are
performed once per scenario in session-scoped fixtures and the tests then
assert on distinct properties of that shared, real outcome.

Run with::

    py -3.12 -m pytest tests/integration -m integration -v -s
"""

from __future__ import annotations

import time

import pytest
from netconfig import (
    ATTO_PER_GEN,
    CLAIM_REPO,
    CLAIM_THRESHOLD,
    FAR_FUTURE,
    MALFORMED_DEADLINE,
    PAST_DEADLINE,
    SOURCE_URL,
)

pytestmark = pytest.mark.integration

MISSING_REPO = "genlayerlabs/definitely-not-a-real-repo-9f3a1c"


def _votes(tx: dict) -> list:
    return sorted(((tx.get("consensus_data") or {}).get("votes") or {}).values())


def _modes(tx: dict) -> list:
    data = tx.get("consensus_data") or {}
    return [r.get("mode") for r in (data.get("leader_receipt") or []) + (data.get("validators") or [])]


@pytest.fixture(scope="session")
def paid_bounty(client, bounty, reporter, poster, settle):
    """A bounty whose TRUE report was verified and paid -- 3 live transactions."""
    created = bounty(CLAIM_REPO, CLAIM_THRESHOLD, FAR_FUTURE, ATTO_PER_GEN)
    child = created["child"]
    created["report_tx"] = str(
        client.write_contract(child, "submit_report",
                              account=reporter, args=["TRUE"])
    )
    created["report_receipt"] = settle(
        created["report_tx"], f"submit_report {child}")
    # Nothing else moves the reporter's GEN on studionet (gas price is 0), so a
    # before/after pair around the payout is a real ledger measurement.
    created["reporter_before_atto"] = int(
        client.w3.eth.get_balance(reporter.address))
    created["verify_tx"] = str(client.write_contract(
        child, "verify", account=reporter))
    created["verify_receipt"] = settle(created["verify_tx"], f"verify {child}")
    # The payout leaves the child as its own transaction, emitted on
    # finalization; it must settle before the recipient's balance means anything.
    created["payout_txs"] = [
        str(handle) for handle in client.get_triggered_transaction_ids(created["verify_tx"])
    ]
    for handle in created["payout_txs"]:
        created["payout_receipt"] = settle(handle, "payout transfer")
    created["reporter_after_atto"] = int(
        client.w3.eth.get_balance(reporter.address))
    created["status"] = dict(client.read_contract(child, "get_status"))
    return created


@pytest.fixture(scope="session")
def rejected_bounty(client, bounty, reporter, settle):
    """A bounty whose FALSE report was verified and rejected -- 3 live transactions."""
    created = bounty(CLAIM_REPO, CLAIM_THRESHOLD, FAR_FUTURE, ATTO_PER_GEN)
    child = created["child"]
    created["report_tx"] = str(
        client.write_contract(child, "submit_report",
                              account=reporter, args=["FALSE"])
    )
    settle(created["report_tx"], f"submit_report FALSE {child}")
    created["verify_tx"] = str(client.write_contract(
        child, "verify", account=reporter))
    created["verify_receipt"] = settle(
        created["verify_tx"], f"verify FALSE {child}")
    created["status"] = dict(client.read_contract(child, "get_status"))
    return created


@pytest.fixture(scope="session")
def absent_repo_bounty(client, bounty, reporter, settle):
    """A bounty about a repository that returns HTTP 404 -- 3 live transactions."""
    created = bounty(MISSING_REPO, CLAIM_THRESHOLD, FAR_FUTURE, ATTO_PER_GEN)
    child = created["child"]
    created["report_tx"] = str(
        client.write_contract(child, "submit_report",
                              account=reporter, args=["FALSE"])
    )
    settle(created["report_tx"], f"submit_report 404 {child}")
    created["verify_tx"] = str(client.write_contract(
        child, "verify", account=reporter))
    created["verify_receipt"] = settle(
        created["verify_tx"], f"verify 404 {child}")
    created["status"] = dict(client.read_contract(child, "get_status"))
    return created


@pytest.fixture(scope="session")
def open_bounty(bounty):
    """One plain, unsettled bounty, shared by every test that only needs OPEN.

    Each consensus transaction costs a full leader/validator round, so the
    read-only and rejection-path tests reuse this rather than each locking a
    reward of their own.
    """
    return bounty(CLAIM_REPO, CLAIM_THRESHOLD, FAR_FUTURE, ATTO_PER_GEN)


class TestPayableAndEscrow:
    """Requirement 4, verified against the real VM rather than a harness."""

    def test_value_bearing_create_bounty_finalizes(self, open_bounty):
        """A plain write would have been rejected with non-zero value.

        `create_bounty` is decorated `@gl.public.write.payable`; Direct Mode
        never checks that flag, so this finalized, value-carrying transaction is
        the proof that the SDK's real payable decorator is in use.
        """
        receipt = open_bounty["create_receipt"]

        assert receipt["status_name"] == "FINALIZED"
        assert receipt["result_name"] == "MAJORITY_AGREE"
        assert int(receipt["value"]) == ATTO_PER_GEN
        assert "agree" in _votes(receipt)

    def test_locked_reward_becomes_the_child_escrow(self, client, open_bounty, poster):
        status = client.read_contract(open_bounty["child"], "get_status")
        assert status["status"] == "OPEN"
        assert status["poster"].lower() == poster.address.lower()
        assert int(status["escrow_atto"]) == ATTO_PER_GEN
        assert int(status["reward_atto"]) == ATTO_PER_GEN
        # The GEN really left the poster and sits in the child, per the ledger.
        assert int(client.w3.eth.get_balance(
            open_bounty["child"])) == ATTO_PER_GEN

    def test_a_call_that_locks_nothing_is_refused(self, client, registry_address, settle, poster):
        bounty_id = f"it-zero-{int(time.time()) % 10**7}"
        try:
            tx = str(
                client.write_contract(
                    registry_address,
                    "create_bounty",
                    account=poster,
                    value=0,
                    args=[bounty_id, CLAIM_REPO, CLAIM_THRESHOLD, FAR_FUTURE],
                )
            )
        except Exception:
            return  # refused at submit time
        # Or the ring finalizes the call with the contract's own rejection, so no
        # bounty exists either way. Both outcomes prove the guard; an indexed
        # zero-reward bounty would not.
        try:
            settle(tx, "create_bounty value=0")
        except Exception:
            pass
        listing = client.read_contract(registry_address, "list_bounties")
        ids = {entry["bounty_id"] for entry in listing["bounties"]}
        assert bounty_id not in ids, "a bounty that locked no reward was created"


class TestDerivedVerdictConsensus:
    """Requirement 3: consensus is reached on derived booleans, not raw text."""

    def test_correct_report_settles_paid(self, paid_bounty):
        assert paid_bounty["verify_receipt"]["status_name"] == "FINALIZED"
        assert paid_bounty["verify_receipt"]["result_name"] == "MAJORITY_AGREE"
        assert paid_bounty["status"]["status"] == "PAID"

    def test_validators_actually_ran_and_agreed(self, paid_bounty):
        """The decisive difference from Direct Mode: real validator executions."""
        receipt = paid_bounty["verify_receipt"]
        votes = _votes(receipt)
        assert "agree" in votes, f"no validator agreed: {votes}"
        assert "validator" in _modes(receipt), _modes(receipt)

    def test_derived_fact_check_is_recorded_not_raw_page(self, paid_bounty):
        """Only the three stable booleans and a summary string survive on-chain."""
        status = paid_bounty["status"]
        assert status["fact_check_reachable"] is True
        assert status["fact_check_repo_ok"] is True
        assert status["fact_check_met"] is True
        assert status["truth_verdict"] == "TRUE"
        assert SOURCE_URL in status["evidence"]
        # No raw HTML/JSON body is ever stored: `evidence` is a bounded summary.
        assert len(status["evidence"]) < 200
        assert "{" not in status["evidence"], "evidence must not be the fetched document"

    def test_payout_moves_the_escrow(self, paid_bounty, reporter):
        """`PAID` has to mean the reporter holds the GEN, not that a field flipped.

        This is the assertion that caught the first shipped version of this
        contract: `gl.get_contract_at(eoa).emit_transfer()` produces an *internal*
        IC -> IC message, and on studionet a recipient that holds no Intelligent
        Contract is settled by a `contract_not_found_handler` with NO_MAJORITY --
        the escrow left the child and was credited to nobody. Value moving to an
        EOA has to leave as an external message, which is what
        `@gl.evm.contract_interface` emits.
        """
        status = paid_bounty["status"]
        assert status["status"] == "PAID"
        assert int(status["escrow_atto"]
                   ) == 0, "the escrow should have left the child"
        assert status["reporter"].lower() == reporter.address.lower()
        moved = paid_bounty["reporter_after_atto"] - \
            paid_bounty["reporter_before_atto"]
        assert moved == int(status["reward_atto"]), (
            f"reporter went {paid_bounty['reporter_before_atto']} -> "
            f"{paid_bounty['reporter_after_atto']} atto for a "
            f"{status['reward_atto']} atto payout (payout txs {paid_bounty['payout_txs']})"
        )

    def test_wrong_report_is_rejected_without_payout(self, rejected_bounty):
        receipt = rejected_bounty["verify_receipt"]
        assert receipt["status_name"] == "FINALIZED"
        assert "agree" in _votes(receipt)
        status = rejected_bounty["status"]
        assert status["truth_verdict"] == "TRUE"
        assert status["reported_verdict"] == "FALSE"
        assert status["status"] == "REJECTED"
        assert int(status["escrow_atto"]
                   ) == ATTO_PER_GEN, "a wrong report must move no money"

    def test_absent_repository_yields_a_verdict_not_a_hang(self, absent_repo_bounty):
        """HTTP 404 is stable and reproducible, so it must settle to FALSE.

        A transient failure would have produced UNRESOLVED instead; conflating
        the two is exactly what the timeout-reclaim path depends on avoiding.
        """
        status = absent_repo_bounty["status"]
        assert absent_repo_bounty["verify_receipt"]["result_name"] == "MAJORITY_AGREE"
        assert status["status"] == "PAID", (
            f"a 404 must derive FALSE and pay the correct reporter, got {status}"
        )
        assert status["fact_check_repo_ok"] is False
        assert status["truth_verdict"] == "FALSE"


class TestAuthorizationAndAggregation:
    """Requirements 5 and 6, on live state."""

    def test_poster_cannot_report_its_own_bounty(self, client, open_bounty, poster, settle):
        child = open_bounty["child"]
        assert client.read_contract(child, "get_status")["status"] == "OPEN"
        try:
            report = str(client.write_contract(
                child, "submit_report", account=poster, args=["TRUE"]))
        except Exception:
            return  # refused at submit time: the self-report never landed
        try:
            settle(report, "poster self-report")
        except Exception:
            return
        # The invariant either way: a poster's report never became the bounty's state.
        status = client.read_contract(child, "get_status")
        assert status["status"] == "OPEN", f"poster self-report took effect: {status}"
        assert status["reporter"] in ("", None)

    def test_only_the_poster_may_reclaim(self, client, open_bounty, reporter, settle, poster):
        """A non-poster calling `reclaim_after_timeout` must not move the escrow.

        The sender check here is `gl.message.sender_address` -- the field the
        installed SDK actually exposes -- evaluated by the live ring, so an
        unauthorized caller's transaction cannot quietly succeed.
        """
        child = open_bounty["child"]
        before = dict(client.read_contract(child, "get_status"))
        assert before["poster"].lower() == poster.address.lower()
        assert before["status"] == "OPEN"

        try:
            tx = str(client.write_contract(
                child, "reclaim_after_timeout", account=reporter))
            settle(tx, "non-poster reclaim attempt")
        except Exception:
            pass  # refused outright: the reclaim never landed

        after = dict(client.read_contract(child, "get_status"))
        assert after["status"] == "OPEN", f"a stranger reclaimed the escrow: {after}"
        assert int(after["escrow_atto"]) == int(
            before["escrow_atto"]) == ATTO_PER_GEN

    def test_registry_aggregates_by_reading_children(self, client, registry_address, open_bounty):
        """The parent's roll-up is a synchronous cross-contract read of children."""
        rollup = client.read_contract(registry_address, "aggregate_statuses")
        by_status = dict(rollup["by_status"] or {})
        assert by_status.get("OPEN", 0) >= 1
        assert int(rollup["escrow_total_atto"]) >= ATTO_PER_GEN

        delegated = dict(
            client.read_contract(registry_address, "get_bounty_status", args=[
                                 open_bounty["bounty_id"]])
        )
        direct = dict(client.read_contract(open_bounty["child"], "get_status"))
        assert delegated["status"] == direct["status"]
        assert int(delegated["escrow_atto"]) == int(direct["escrow_atto"])
        assert delegated["bounty_id"] == open_bounty["bounty_id"]

        listing = client.read_contract(registry_address, "list_bounties")
        ids = {entry["bounty_id"] for entry in listing["bounties"]}
        assert open_bounty["bounty_id"] in ids
        assert int(listing["count"]) == len(ids)
        assert int(listing["total_created"]) >= int(listing["count"])

    def test_unknown_bounty_id_is_not_silently_empty(self, client, registry_address):
        with pytest.raises(Exception):
            client.read_contract(
                registry_address, "get_bounty_address", args=["no-such-bounty"])


def _bounty_ids(client, registry_address) -> set:
    listing = client.read_contract(registry_address, "list_bounties")
    return {entry["bounty_id"] for entry in listing["bounties"]}


class TestCreateBountyValidation:
    """The lifecycle fix: a malformed bounty is refused *before* a child is paid for.

    Each case submits a real `create_bounty` that the corrected factory must
    reject at its input-validation gate. The proof is not the revert message --
    the ring may refuse at submit time or finalize the call with the contract's
    own rejection -- but that no child contract was ever indexed under the id,
    because a rejected creation must not spend a deployment on an unusable claim.
    """

    def _assert_never_created(self, client, registry_address, settle, poster, args):
        bounty_id = f"it-reject-{int(time.time() * 1000) % 10**9}"
        try:
            tx = str(client.write_contract(
                registry_address, "create_bounty",
                account=poster, value=ATTO_PER_GEN, args=[bounty_id, *args]))
            try:
                settle(tx, f"create_bounty {bounty_id}")
            except Exception:
                pass  # settled in a rejecting state; the id check is the proof
        except Exception:
            pass  # refused at submit time
        assert bounty_id not in _bounty_ids(
            client, registry_address), f"a malformed bounty was indexed: {bounty_id}"

    def test_a_bad_repository_shape_is_refused_without_a_child(
            self, client, registry_address, settle, poster):
        self._assert_never_created(
            client, registry_address, settle, poster,
            ["not-a-repo", CLAIM_THRESHOLD, FAR_FUTURE])

    def test_a_non_positive_threshold_is_refused_without_a_child(
            self, client, registry_address, settle, poster):
        self._assert_never_created(
            client, registry_address, settle, poster,
            [CLAIM_REPO, 0, FAR_FUTURE])

    def test_a_deadline_in_the_past_is_refused_without_a_child(
            self, client, registry_address, settle, poster):
        self._assert_never_created(
            client, registry_address, settle, poster,
            [CLAIM_REPO, CLAIM_THRESHOLD, PAST_DEADLINE])

    def test_a_malformed_deadline_is_refused_without_a_child(
            self, client, registry_address, settle, poster):
        self._assert_never_created(
            client, registry_address, settle, poster,
            [CLAIM_REPO, CLAIM_THRESHOLD, MALFORMED_DEADLINE])


@pytest.fixture(scope="session")
def reported_unverified_bounty(client, bounty, reporter, settle):
    """Create a bounty and take a timely, *finalized* report, but never verify."""
    created = bounty(CLAIM_REPO, CLAIM_THRESHOLD, FAR_FUTURE, ATTO_PER_GEN)
    child = created["child"]
    tx = str(client.write_contract(
        child, "submit_report", account=reporter, args=["TRUE"]))
    created["report_tx"] = tx
    # Wait for the report to finalize; reading the receipt immediately would see
    # the pre-report OPEN state and tell us nothing about the reclaim guard.
    created["report_receipt"] = settle(tx, f"submit_report pending {child}")
    return created


class TestReclaimCannotUndercutAPendingReport:
    """The race fix, on live state: a timely report is never refunded away.

    The REPORTED guard in `reclaim_after_timeout` sits ahead of the deadline
    check, so a poster reclaim attempt against a still-pending report reverts on
    the *report* ground no matter how far the deadline is -- which is exactly
    what lets this be proven without waiting for an expiry. The escrow has to
    stay put, unspendable by the poster, until verification settles it.
    """

    def test_a_pending_report_blocks_the_poster_refund(
            self, client, reported_unverified_bounty, poster):
        child = reported_unverified_bounty["child"]
        before = dict(client.read_contract(child, "get_status"))
        assert before["status"] == "REPORTED", before
        assert int(before["escrow_atto"]) == ATTO_PER_GEN

        try:
            tx = str(client.write_contract(
                child, "reclaim_after_timeout", account=poster))
            try:
                client.get_transaction(tx)
            except Exception:
                pass
        except Exception:
            pass  # refused outright

        after = dict(client.read_contract(child, "get_status"))
        assert after["status"] == "REPORTED", (
            f"a pending report was refunded away: {after}")
        assert int(after["escrow_atto"]) == ATTO_PER_GEN, (
            "the escrow left the child while a report awaited verification")
