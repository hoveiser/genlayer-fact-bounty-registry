"""Direct Mode tests for the `BountyClaim` child contract.

Read the ``conftest.py`` docstring first: Direct Mode runs the **leader only**.
The comparative validator that GenLayer executes on studionet is captured but
never consulted by a network here, so nothing below proves that two independent
parties agree -- the ``run_validator`` tests only prove the validator *function*
behaves correctly when a test drives it by hand. Real leader/validator consensus
is exercised in ``tests/integration/test_studionet_factcheck.py``.
"""

from __future__ import annotations

import pytest

from direct_harness import (
    AFTER_DEADLINE,
    BEFORE_DEADLINE,
    CLAIM_REPO,
    CREATED_AT,
    DEADLINE,
    REWARD_ATTO,
    make_address,
    set_datetime,
    warp_before_deploy,
)

pytestmark = pytest.mark.direct

SOURCE_URL = "https://api.github.com/repos/genlayerlabs/genlayer"


# ---------------------------------------------------------------------------
# Creation / initial state
# ---------------------------------------------------------------------------
def test_deploy_locks_the_claim_and_starts_open(claim, poster):
    status = claim.get_status()

    assert status["status"] == "OPEN"
    assert status["claim"] == "genlayerlabs/genlayer"
    assert status["threshold"] == 1000
    assert status["source_url"] == SOURCE_URL
    assert status["reward_atto"] == REWARD_ATTO
    assert status["poster"] == poster.as_hex
    assert status["escrow_atto"] == REWARD_ATTO
    # The deadline is stored normalised to its fixed-width prefix, so a
    # microseconds-carrying timestamp cannot change a comparison outcome.
    assert status["deadline_at"] == DEADLINE


def test_created_at_comes_from_the_transaction_timestamp(direct_vm, deploy_claim):
    warp_before_deploy(direct_vm, CREATED_AT)
    contract = deploy_claim()
    assert contract.get_status()["created_at"] == CREATED_AT[:19]


@pytest.mark.parametrize(
    "repo, threshold, deadline_at, needle",
    [
        ("not-a-repo", 1000, DEADLINE, "owner/name"),
        ("owner/", 1000, DEADLINE, "owner/name"),  # empty segment, not just no '/'
        ("a//b", 1000, DEADLINE, "owner/name"),
        ("genlayerlabs/genlayer", 0, DEADLINE, "threshold must be positive"),
        ("genlayerlabs/genlayer", 1000, "nope", "YYYY-MM-DDTHH:MM:SS"),
        # 19 characters, wrong separator: length-only checks called this valid
        ("genlayerlabs/genlayer", 1000, "2026-01-02 00:00:00", "YYYY-MM-DDTHH:MM:SS"),
        # shape and length perfect, month 13 is not a month
        ("genlayerlabs/genlayer", 1000, "2026-13-01T00:00:00", "YYYY-MM-DDTHH:MM:SS"),
    ],
)
def test_constructor_rejects_unusable_claims(
    deploy_claim, direct_vm, repo, threshold, deadline_at, needle
):
    with direct_vm.expect_revert(needle):
        deploy_claim(repo=repo, threshold=threshold, deadline_at=deadline_at)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def test_reporter_can_assert_a_verdict(claim, serve_github, direct_vm, reporter):
    serve_github("genlayerlabs/genlayer", 5000)
    direct_vm.sender = reporter

    assert claim.submit_report("true") == "TRUE"

    status = claim.get_status()
    assert status["status"] == "REPORTED"
    assert status["reported_verdict"] == "TRUE"
    assert status["reporter"] == reporter.as_hex


def test_poster_cannot_report_their_own_bounty(claim, serve_github, direct_vm, poster):
    serve_github("genlayerlabs/genlayer", 5000)
    direct_vm.sender = poster
    with direct_vm.expect_revert("poster cannot report their own bounty"):
        claim.submit_report("TRUE")


def test_only_the_first_report_counts(claim, serve_github, direct_vm, reporter):
    serve_github("genlayerlabs/genlayer", 5000)
    late = make_address("second-reporter")

    direct_vm.sender = reporter
    claim.submit_report("TRUE")
    direct_vm.sender = late
    with direct_vm.expect_revert("not open"):
        claim.submit_report("FALSE")

    assert claim.get_status()["reporter"] == reporter.as_hex


def test_verdict_must_be_true_or_false(claim, direct_vm, reporter):
    direct_vm.sender = reporter
    with direct_vm.expect_revert("verdict must be 'TRUE' or 'FALSE'"):
        claim.submit_report("MAYBE")


# ---------------------------------------------------------------------------
# The deadline gate
# ---------------------------------------------------------------------------
def test_a_report_after_the_deadline_is_refused_and_never_verified(
    claim, direct_vm, bus, reporter, poster
):
    """Expiry must close the bounty outright, not merely make it racy.

    No web mock is registered on purpose: if `submit_report` had accepted the
    late verdict and scheduled anything, the missing mock would surface. The
    state must be byte-for-byte what it was before the attempt.
    """
    set_datetime(direct_vm, AFTER_DEADLINE)
    direct_vm.sender = reporter
    with direct_vm.expect_revert("reports submitted after the deadline are not accepted"):
        claim.submit_report("TRUE")

    # Nothing became reportable, so verification still sees no report at all...
    direct_vm.sender = poster
    with direct_vm.expect_revert("nothing to verify"):
        claim.verify()
    # ... and the poster's refund path is untouched by the refused report.
    assert claim.reclaim_after_timeout() == "REFUNDED"
    assert bus.total_sent(reporter) == 0


def test_a_report_arriving_exactly_at_the_deadline_is_still_on_time(
    claim, serve_github, direct_vm, reporter
):
    """The boundary is inclusive: 'now > deadline' rejects, 'now == deadline' does not.

    Strict inequality is what keeps a report filed in the final second alive;
    the equality case must therefore be accepted, not silently written off as
    late by an off-by-one comparison.
    """
    serve_github("genlayerlabs/genlayer", 5000)
    set_datetime(direct_vm, DEADLINE + ".500000Z")  # same normalised 19-char stamp
    direct_vm.sender = reporter

    assert claim.submit_report("TRUE") == "TRUE"
    assert claim.get_status()["status"] == "REPORTED"


# ---------------------------------------------------------------------------
# Settlement
# ---------------------------------------------------------------------------
def test_correct_report_is_paid_out(
    claim, serve_github, direct_vm, bus, reporter, poster
):
    """Truth is TRUE (5000 >= 1000), the reporter said TRUE -> they get the escrow."""
    serve_github("genlayerlabs/genlayer", 5000)
    direct_vm.sender = reporter
    claim.submit_report("TRUE")

    direct_vm.sender = poster
    assert claim.verify() == "PAID"

    status = claim.get_status()
    assert status["truth_verdict"] == "TRUE"
    assert status["fact_check_reachable"] is True
    assert status["fact_check_repo_ok"] is True
    assert status["fact_check_met"] is True
    assert status["evidence"] == f"{SOURCE_URL} stargazers_count >= 1000"
    # The escrow moved to the reporter and nowhere else.
    assert bus.transfers == [(reporter.as_hex, REWARD_ATTO)]
    assert bus.total_sent(poster) == 0
    # ... but the *ledger* still shows the escrow: direct mode hands PostMessage
    # to the test hook and never debits the VM balance, so `escrow_atto` is not
    # evidence of a payout here. Real balance movement is checked on studionet.
    assert status["escrow_atto"] == REWARD_ATTO


def test_incorrect_report_is_rejected_and_pays_nothing(
    claim, serve_github, direct_vm, bus, reporter, poster
):
    """Truth is FALSE (500 < 1000) but the reporter claimed TRUE -> no payout."""
    serve_github("genlayerlabs/genlayer", 500)
    direct_vm.sender = reporter
    claim.submit_report("TRUE")

    direct_vm.sender = poster
    assert claim.verify() == "REJECTED"

    status = claim.get_status()
    assert status["truth_verdict"] == "FALSE"
    assert status["fact_check_met"] is False
    assert bus.transfers == []
    # Money is still held by the bounty, for the poster to reclaim.
    assert status["escrow_atto"] == REWARD_ATTO


def test_a_correct_false_report_is_paid_out(
    claim, serve_github, direct_vm, bus, reporter, poster
):
    """The contract pays for *matching the truth*, not for optimism."""
    serve_github("genlayerlabs/genlayer", 12)
    direct_vm.sender = reporter
    claim.submit_report("FALSE")

    direct_vm.sender = poster
    assert claim.verify() == "PAID"
    assert bus.transfers == [(reporter.as_hex, REWARD_ATTO)]


def test_wrong_repo_always_derives_false(
    claim, serve_github, direct_vm, bus, reporter, poster
):
    """The URL answers, but describes a different repository -> the claim is FALSE."""
    serve_github(CLAIM_REPO, 9_999_999, served_as="someone-else/different")
    direct_vm.sender = reporter
    claim.submit_report("TRUE")

    direct_vm.sender = poster
    assert claim.verify() == "REJECTED"
    status = claim.get_status()
    assert status["fact_check_reachable"] is True
    assert status["fact_check_repo_ok"] is False
    assert bus.transfers == []


def test_missing_repo_derives_false_not_unresolved(
    claim, serve_github, direct_vm, reporter, poster
):
    """A 404 is stable and reproducible, so it *is* evidence: the repo does not exist."""
    serve_github("genlayerlabs/genlayer", 0, status=404,
                 body='{"message":"Not Found"}')
    direct_vm.sender = reporter
    claim.submit_report("TRUE")

    direct_vm.sender = poster
    assert claim.verify() == "REJECTED"
    assert claim.get_status()["truth_verdict"] == "FALSE"


@pytest.mark.parametrize("unreachable_status", [403, 500, 503])
def test_transient_fetch_failure_settles_unresolved(
    claim, serve_github, direct_vm, bus, reporter, poster, unreachable_status
):
    """A rate limit or an outage says nothing about the claim, so it cannot mint a payout.

    Requirement 3: an unreachable source settles to UNRESOLVED and leaves the
    poster able to reclaim after the timeout, rather than looping on appeals.
    """
    serve_github("genlayerlabs/genlayer", 5000,
                 status=unreachable_status, body="")
    direct_vm.sender = reporter
    claim.submit_report("TRUE")

    direct_vm.sender = poster
    assert claim.verify() == "UNRESOLVED"

    status = claim.get_status()
    assert status["truth_verdict"] == "UNRESOLVED"
    assert status["fact_check_reachable"] is False
    assert bus.transfers == []


def test_unresolved_bounty_is_reclaimable_after_the_deadline(
    claim, serve_github, direct_vm, bus, reporter, poster
):
    serve_github("genlayerlabs/genlayer", 5000, status=500, body="")
    direct_vm.sender = reporter
    claim.submit_report("TRUE")
    direct_vm.sender = poster
    claim.verify()

    set_datetime(direct_vm, AFTER_DEADLINE)
    assert claim.reclaim_after_timeout() == "REFUNDED"
    assert bus.total_sent(poster) == REWARD_ATTO


def test_verify_refuses_a_stranger(claim, serve_github, direct_vm, reporter):
    serve_github("genlayerlabs/genlayer", 5000)
    direct_vm.sender = reporter
    claim.submit_report("TRUE")

    direct_vm.sender = make_address("passer-by")
    with direct_vm.expect_revert("only the poster or the reporter may trigger verification"):
        claim.verify()


def test_verify_requires_a_report_first(claim, direct_vm, poster):
    direct_vm.sender = poster
    with direct_vm.expect_revert("nothing to verify"):
        claim.verify()


def test_a_settled_bounty_cannot_be_verified_twice(
    claim, serve_github, direct_vm, reporter, poster
):
    serve_github("genlayerlabs/genlayer", 5000)
    direct_vm.sender = reporter
    claim.submit_report("TRUE")
    direct_vm.sender = poster
    claim.verify()

    with direct_vm.expect_revert("nothing to verify"):
        claim.verify()


# ---------------------------------------------------------------------------
# Timeout reclaim, with and without any verification
# ---------------------------------------------------------------------------
def test_poster_reclaims_a_never_reported_bounty_without_any_verification(
    claim, direct_vm, bus, poster
):
    """Requirement 7: no report, no AI verification, still reclaimable.

    No web mock is registered at all -- if the contract tried to fetch anything,
    direct mode would raise MockNotFoundError and this test would fail.
    """
    set_datetime(direct_vm, BEFORE_DEADLINE)
    direct_vm.sender = poster
    with direct_vm.expect_revert("not reclaimable until"):
        claim.reclaim_after_timeout()

    set_datetime(direct_vm, AFTER_DEADLINE)
    assert claim.reclaim_after_timeout() == "REFUNDED"
    assert bus.transfers == [(poster.as_hex, REWARD_ATTO)]
    # Direct mode records the emission but does not move ledger balance, so the
    # reclaim is proven by the emitted transfer and the REFUNDED status.
    assert claim.get_status()["status"] == "REFUNDED"


def test_only_the_poster_may_reclaim(claim, direct_vm):
    set_datetime(direct_vm, AFTER_DEADLINE)
    direct_vm.sender = make_address("looter")
    with direct_vm.expect_revert("only the poster may reclaim"):
        claim.reclaim_after_timeout()


def test_a_paid_bounty_cannot_be_reclaimed(
    claim, serve_github, direct_vm, reporter, poster
):
    serve_github("genlayerlabs/genlayer", 5000)
    direct_vm.sender = reporter
    claim.submit_report("TRUE")
    direct_vm.sender = poster
    claim.verify()

    set_datetime(direct_vm, AFTER_DEADLINE)
    with direct_vm.expect_revert("was already paid out"):
        claim.reclaim_after_timeout()


def test_reclaim_is_single_use(claim, direct_vm, poster):
    set_datetime(direct_vm, AFTER_DEADLINE)
    direct_vm.sender = poster
    assert claim.reclaim_after_timeout() == "REFUNDED"
    with direct_vm.expect_revert("was already reclaimed"):
        claim.reclaim_after_timeout()


# ---------------------------------------------------------------------------
# The reclaim/report race
# ---------------------------------------------------------------------------
def test_an_on_time_report_is_not_undercut_by_a_reclaim(
    claim, serve_github, direct_vm, bus, reporter, poster
):
    """A report filed one second before expiry still owes consensus, not a refund.

    The bug this pins: the poster watches the clock, the deadline passes, and
    `reclaim_after_timeout` pays out while the bounty sits in REPORTED with a
    timely verdict nobody has verified yet -- the reporter can never be paid.
    Reclaim must stay blocked until `verify()` has had its chance; settlement
    itself must still work afterwards.
    """
    serve_github("genlayerlabs/genlayer", 5000)
    set_datetime(direct_vm, BEFORE_DEADLINE)  # the deadline is still ahead
    direct_vm.sender = reporter
    assert claim.submit_report("TRUE") == "TRUE"

    set_datetime(direct_vm, AFTER_DEADLINE)
    direct_vm.sender = poster
    with direct_vm.expect_revert("report awaiting verification"):
        claim.reclaim_after_timeout()
    assert bus.total_sent(poster) == 0  # the refund really was not emitted

    # The blocked poster is not stuck: verifying settles the bounty for real.
    assert claim.verify() == "PAID"
    assert bus.transfers == [(reporter.as_hex, REWARD_ATTO)]
    # Settled, so reclaim is refused on the paid-out ground, not the race one.
    with direct_vm.expect_revert("was already paid out"):
        claim.reclaim_after_timeout()


# ---------------------------------------------------------------------------
# Comparative validator behaviour (driven by hand -- see module docstring)
# ---------------------------------------------------------------------------
@pytest.fixture
def settled_for_validation(claim, serve_github, direct_vm, reporter, poster):
    """Run one `verify()` so the validator closure has been captured."""
    serve_github("genlayerlabs/genlayer", 5000)
    direct_vm.sender = reporter
    claim.submit_report("TRUE")
    direct_vm.sender = poster
    claim.verify()
    return claim


def test_validator_agrees_when_it_derives_the_same_verdict(
    settled_for_validation, direct_vm
):
    assert direct_vm.run_validator() is True


def test_validator_disagrees_when_the_derived_verdict_flips(
    settled_for_validation, direct_vm, serve_github
):
    """The leader saw the threshold met; the validator's own fetch says it is not."""
    direct_vm.clear_mocks()
    serve_github("genlayerlabs/genlayer", 10)
    assert direct_vm.run_validator() is False


def test_volatile_drift_that_keeps_the_verdict_does_not_break_consensus(
    settled_for_validation, direct_vm, serve_github
):
    """The star count moves but stays above the threshold -> still agreement.

    This is the whole point of comparing *derived* verdicts instead of raw page
    text: a page that merely drifts must not by itself force a rotation.
    """
    direct_vm.clear_mocks()
    serve_github("genlayerlabs/genlayer", 8000)
    assert direct_vm.run_validator() is True


def test_validator_treats_its_own_fetch_failure_as_disagreement(
    settled_for_validation, direct_vm, serve_github
):
    """Requirement 3: a failed validator fetch returns False, never a pass."""
    direct_vm.clear_mocks()
    serve_github("genlayerlabs/genlayer", 5000, status=500, body="")
    leader_said_success = {"reachable": True, "repo_ok": True, "met": True}
    assert direct_vm.run_validator(leader_result=leader_said_success) is False


def test_validator_rejects_a_leader_that_never_returned(
    settled_for_validation, direct_vm
):
    assert direct_vm.run_validator(
        leader_error=RuntimeError("leader sandbox died")) is False


def test_validator_disagrees_when_the_repo_identity_derivation_differs(
    settled_for_validation, direct_vm, serve_github
):
    direct_vm.clear_mocks()
    serve_github(CLAIM_REPO, 5000, served_as="someone-else/different")
    assert direct_vm.run_validator() is False


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------
def test_get_evidence_exposes_the_derived_fact_check(
    claim, serve_github, direct_vm, reporter, poster
):
    serve_github("genlayerlabs/genlayer", 5000)
    direct_vm.sender = reporter
    claim.submit_report("FALSE")
    direct_vm.sender = poster
    claim.verify()

    evidence = claim.get_evidence()
    assert evidence["status"] == "REJECTED"
    assert evidence["reported_verdict"] == "FALSE"
    assert evidence["truth_verdict"] == "TRUE"
    assert evidence["fact_check_met"] is True
    assert evidence["source_url"] == SOURCE_URL
    # The evidence view deliberately carries no money fields.
    assert "reward_atto" not in evidence
    assert "escrow_atto" not in evidence


def test_child_uses_the_real_message_sender_field(claim, direct_vm, reporter, serve_github):
    """Guards the SDK deviation: the attribute is `sender_address`, not `sender_account`."""
    import sys

    gl = sys.modules["genlayer.gl"]
    assert hasattr(gl.message, "sender_address")
    assert not hasattr(gl.message, "sender_account")

    serve_github("genlayerlabs/genlayer", 5000)
    direct_vm.sender = reporter
    assert claim.get_status()["reporter"] == ""
    claim.submit_report("TRUE")
    assert claim.get_status()["reporter"] == gl.message.sender_address.as_hex
