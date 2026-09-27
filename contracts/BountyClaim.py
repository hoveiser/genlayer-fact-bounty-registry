# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }

"""Child contract deployed by `BountyRegistry` for a single fact-check bounty.

Claim type settled by this contract
----------------------------------
"Does the public GitHub repository `<owner>/<repo>` have at least `<threshold>`
stars?" -- checked against `https://api.github.com/repos/<owner>/<repo>`.

The contract never trusts the page text. Both the leader and every validator
fetch the source independently and reduce it to a small set of *stable,
reproducible* booleans; consensus is reached on those derived verdicts only.

One instance per bounty, so that:
  * escrow is isolated -- a bounty can only ever move the GEN it holds;
  * the (potentially expensive, appealable) verification state machine of one
    bounty can never interfere with another;
  * the child address is itself the receipt for the bounty.
"""

from dataclasses import dataclass

import json

from genlayer import *

# --------------------------------------------------------------------------
# Status values. Kept as plain `str` -- GenLayer storage does not support Enum.
# --------------------------------------------------------------------------
STATUS_OPEN = "OPEN"  # locked, awaiting a report
STATUS_REPORTED = "REPORTED"  # a report exists, verification not run yet
STATUS_PAID = "PAID"  # report matched the validator-derived truth, reporter paid
STATUS_REJECTED = "REJECTED"  # report contradicted the derived truth
STATUS_UNRESOLVED = "UNRESOLVED"  # source unreachable for consensus, no verdict
STATUS_REFUNDED = "REFUNDED"  # poster reclaimed the escrow

VERDICT_TRUE = "TRUE"
VERDICT_FALSE = "FALSE"
VERDICT_UNRESOLVED = "UNRESOLVED"

# Error taxonomy, so leader and validator can agree on *failures* too.
ERROR_EXPECTED = "[EXPECTED]"
ERROR_TRANSIENT = "[TRANSIENT]"
ERROR_LLM = "[LLM_ERROR]"

# `gl.message_raw["datetime"]` observed on studionet looks like
# "2026-09-26T20:06:34.271991Z". The first 19 characters are a fixed-width,
# zero-padded, UTC ISO-8601 stamp, so plain lexicographic comparison of the
# normalised prefix is the same ordering as chronological comparison -- and it
# needs no `datetime` parsing inside the consensus path.
DATETIME_PREFIX_LEN = 19


@allow_storage
@dataclass
class FactCheck:
    """A validator-consensused fact-check result.

    APPEND-ONLY: this is the on-chain storage layout of an already deployed
    contract family. New fields must be added at the END and must have a default
    or be reconstructible from zero -- inserting a field in the middle shifts
    every subsequent slot and silently corrupts existing state.
    """

    reachable: bool
    """False when the source could not be read at all by the executing party."""
    repo_ok: bool
    """False when the source does not describe the repository the claim cites."""
    met: bool
    """True when the stable field satisfied the asserted threshold."""


def _normalize_datetime(raw: str) -> str:
    """Reduce a transaction timestamp to its fixed-width `YYYY-MM-DDTHH:MM:SS`."""
    return str(raw)[:DATETIME_PREFIX_LEN]


def _valid_datetime(raw: str) -> bool:
    """True only when `raw` starts with a well-formed `YYYY-MM-DDTHH:MM:SS` UTC stamp.

    Fixed-width positional check: digits at the documented offsets, and `-`/`T`/`:`
    at theirs. Every deadline comparison in this contract family is a
    lexicographic comparison of that 19-character prefix, so a string which does
    not normalise to a chronological ordering must never be stored at all --
    otherwise '2026-01-02 00:00:00' (space instead of 'T') would silently pass a
    length check yet sort against every real timestamp in the wrong order.
    """
    stamp = str(raw)
    if len(stamp) < DATETIME_PREFIX_LEN:
        return False
    stamp = stamp[:DATETIME_PREFIX_LEN]
    if stamp[4] != "-" or stamp[7] != "-":
        return False
    if stamp[10] != "T":
        return False
    if stamp[13] != ":" or stamp[16] != ":":
        return False
    digit_positions = (0, 1, 2, 3, 5, 6, 8, 9, 11, 12, 14, 15, 17, 18)
    for index in digit_positions:
        if stamp[index] < "0" or stamp[index] > "9":
            return False
    # Zero-padded two-digit fields compare correctly as strings: month 13 is
    # not a month, and '2026-13-01T00:00:00' would otherwise order as if it were.
    if stamp[5:7] < "01" or stamp[5:7] > "12":
        return False
    if stamp[8:10] < "01" or stamp[8:10] > "31":
        return False
    if stamp[11:13] > "23":
        return False
    if stamp[14:16] > "59" or stamp[17:19] > "59":
        return False
    return True


def _valid_repo_shape(name: str) -> bool:
    """True only for a normalised, well-formed GitHub `owner/name` repository.

    `name` is expected to already be stripped and lower-cased. Exactly one
    separator, both sides non-empty, and only the characters GitHub allows
    (letters, digits, and `.`, `-`, `_` inside a segment). A bare `'/' in name`
    test would accept 'a//b', '/x' or 'x/' -- claims that can never resolve,
    which must not be spendable as bounties or child deployments.
    """
    parts = name.split("/")
    if len(parts) != 2:
        return False
    for part in parts:
        if len(part) == 0:
            return False
        first = part[0]
        last = part[len(part) - 1]
        if first == "-" or first == "." or first == "_":
            return False
        if last == "-" or last == "." or last == "_":
            return False
        for ch in part:
            is_lower = ch >= "a" and ch <= "z"
            is_digit = ch >= "0" and ch <= "9"
            is_sep = ch == "-" or ch == "." or ch == "_"
            if not (is_lower or is_digit or is_sep):
                return False
    return True


@gl.evm.contract_interface
class Payee:
    """Declared recipient of a value transfer that lives on the chain layer.

    A reporter and a poster are EOAs. Sending GEN to an EOA is an *external*
    message (IC -> chain layer), which is a different primitive from the
    internal IC -> IC message that `gl.get_contract_at()` produces: the latter
    is resolved by the GenVM contract dispatcher and, for an address holding no
    Intelligent Contract, is settled by a `contract_not_found_handler` that
    never reaches validator majority -- the escrow leaves the child and is
    credited to nobody. `gl.evm.contract_interface` emits `EthSend` with empty
    calldata instead, which is the SDK's supported pure value transfer to an
    EOA. The empty View/Write classes are deliberate: no method is ever called
    on the recipient, only value is moved.
    """

    class View:
        pass

    class Write:
        pass


def _derive_fact(url: str, expected_repo: str, threshold: int) -> dict:
    """Fetch the source and derive ONLY stable, reproducible fields.

    Called identically by the leader and by each validator, each with its own
    independent network fetch. Deliberately never returns the raw body, the star
    count itself, a timestamp, an etag or any other volatile field -- a page that
    merely drifts between the two fetches must not by itself break consensus, only
    a drift that flips the verdict can.
    """
    try:
        response = gl.nondet.web.get(url, headers={"Accept": "application/vnd.github+json"})
        status = int(response.status)
        body = response.body
    except Exception:
        # Upstream unreachable / DNS / timeout: a *transient* failure, never a
        # verdict. Reported as not-reachable so the caller can settle UNRESOLVED.
        return {"reachable": False, "repo_ok": False, "met": False}

    if status == 403 or status >= 500:
        # 403 is GitHub's unauthenticated rate-limit response and 5xx is an
        # upstream outage: both are transient, neither is evidence about the claim.
        return {"reachable": False, "repo_ok": False, "met": False}
    if status >= 400:
        # 404 etc. is stable and reproducible: the cited repository does not exist.
        return {"reachable": True, "repo_ok": False, "met": False}
    if body is None:
        return {"reachable": False, "repo_ok": False, "met": False}

    data = json.loads(bytes(body).decode("utf-8"))
    observed_repo = str(data["full_name"]).lower()
    observed_count = int(data["stargazers_count"])
    return {
        "reachable": True,
        "repo_ok": observed_repo == expected_repo,
        "met": observed_count >= threshold,
    }


def _verdict_of(derived: dict) -> str:
    """Map a derived fact-check to the claim's truth value. Deterministic."""
    if not derived["reachable"]:
        return VERDICT_UNRESOLVED
    if not derived["repo_ok"]:
        return VERDICT_FALSE
    return VERDICT_TRUE if derived["met"] else VERDICT_FALSE


def _same_derivation(left: dict, right: dict) -> bool:
    """Comparative check over derived verdicts -- never over raw fetched text."""
    for key in ("reachable", "repo_ok", "met"):
        if bool(left[key]) != bool(right[key]):
            return False
    return True


class BountyClaim(gl.Contract):
    """Escrow + verification state machine for exactly one factual claim."""

    # ---- immutables, fixed at factory deployment -------------------------
    bounty_id: str
    poster: Address
    repo_full_name: str
    source_url: str
    threshold: u256
    reward_atto: u256
    deadline_at: str
    created_at: str
    registry: Address
    # ---- mutable state ---------------------------------------------------
    status: str
    reporter: str
    reported_verdict: str
    truth_verdict: str
    last_fact_check: FactCheck
    evidence: str

    def __init__(
        self,
        bounty_id: str,
        repo_full_name: str,
        threshold: u256,
        deadline_at: str,
        poster: Address,
        reward_atto: u256,
        registry: Address,
    ) -> None:
        repo = str(repo_full_name).strip().lower()
        if not _valid_repo_shape(repo):
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} repo_full_name must look like 'owner/name', got '{repo_full_name}'"
            )
        if int(threshold) <= 0:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} threshold must be positive")
        if not _valid_datetime(deadline_at):
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} deadline_at must be 'YYYY-MM-DDTHH:MM:SS' UTC, got '{deadline_at}'"
            )

        self.bounty_id = str(bounty_id)
        self.repo_full_name = repo
        self.source_url = f"https://api.github.com/repos/{repo}"
        self.threshold = u256(int(threshold))
        self.deadline_at = _normalize_datetime(deadline_at)
        self.poster = poster
        self.reward_atto = u256(int(reward_atto))
        self.registry = registry
        self.created_at = _normalize_datetime(gl.message_raw["datetime"])
        self.status = STATUS_OPEN
        self.reporter = ""
        self.reported_verdict = ""
        self.truth_verdict = ""
        self.last_fact_check = FactCheck(reachable=False, repo_ok=False, met=False)
        self.evidence = ""

    # ------------------------------------------------------------------
    # Public writes
    # ------------------------------------------------------------------
    @gl.public.write
    def submit_report(self, verdict: str) -> str:
        """Assert that the claim is TRUE or FALSE. First on-time report wins.

        A report submitted after `deadline_at` has passed is refused outright:
        it never becomes state and verification is never scheduled for it, so
        the poster's refund path cannot be blocked by a stale claim.
        """
        normalized = str(verdict).strip().upper()
        if normalized not in (VERDICT_TRUE, VERDICT_FALSE):
            raise gl.vm.UserError(f"{ERROR_EXPECTED} verdict must be 'TRUE' or 'FALSE', got '{verdict}'")
        if self.status != STATUS_OPEN:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} bounty {self.bounty_id} is {self.status}, not open")

        now = _normalize_datetime(gl.message_raw["datetime"])
        if now > self.deadline_at:
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} bounty {self.bounty_id} closed at {self.deadline_at}; "
                f"reports submitted after the deadline are not accepted (now {now})"
            )

        sender = gl.message.sender_address
        if sender.as_hex == self.poster.as_hex:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} the poster cannot report their own bounty")

        self.reporter = sender.as_hex
        self.reported_verdict = normalized
        self.status = STATUS_REPORTED
        return normalized

    @gl.public.write
    def verify(self) -> str:
        """Independently re-derive the truth and settle the bounty.

        The consensus work happens inside `_fact_check`: the leader fetches and
        derives a verdict, and each validator fetches *again for itself* and
        compares the derived booleans. Nobody's raw page fetch is trusted, and no
        single AI opinion decides anything.
        """
        caller = gl.message.sender_address.as_hex
        if caller not in (self.poster.as_hex, self.reporter):
            raise gl.vm.UserError(f"{ERROR_EXPECTED} only the poster or the reporter may trigger verification")
        if self.status != STATUS_REPORTED:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} bounty {self.bounty_id} is {self.status}, nothing to verify")

        derived = self._fact_check()
        self.last_fact_check = FactCheck(
            reachable=derived["reachable"],
            repo_ok=derived["repo_ok"],
            met=derived["met"],
        )
        truth = _verdict_of(derived)
        self.truth_verdict = truth
        self.evidence = (
            f"{self.source_url} stargazers_count {'>=' if derived['met'] else '<'} {int(self.threshold)}"
        )

        if truth == VERDICT_UNRESOLVED:
            # Consensus agreed that it could not agree: settle to UNRESOLVED so the
            # poster can reclaim after the deadline instead of looping on appeals.
            self.status = STATUS_UNRESOLVED
        elif truth == self.reported_verdict:
            self.status = STATUS_PAID
            Payee(Address(self.reporter)).emit_transfer(value=self.reward_atto)
        else:
            self.status = STATUS_REJECTED
        return self.status

    @gl.public.write
    def reclaim_after_timeout(self) -> str:
        """Poster reclaims escrow once the deadline passed and nothing was paid.

        Only reachable from states where no timely report is still owed its
        chance at consensus:
          * OPEN       -- nobody ever reported; no verification is needed;
          * REJECTED   -- the report was verified and contradicted the truth;
          * UNRESOLVED -- verification concluded without a verdict.

        A REPORTED bounty is *never* refundable, even past its deadline: the
        report was accepted on time and is owed a verification round. The poster
        may trigger `verify()` themselves at any time to settle the bounty and
        unlock this path -- an on-time report can be undercut by a refund only
        after consensus has had its chance at it.
        """
        if gl.message.sender_address.as_hex != self.poster.as_hex:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} only the poster may reclaim")
        if self.status == STATUS_PAID:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} bounty {self.bounty_id} was already paid out")
        if self.status == STATUS_REFUNDED:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} bounty {self.bounty_id} was already reclaimed")
        if self.status == STATUS_REPORTED:
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} bounty {self.bounty_id} has a report awaiting verification; "
                f"call verify() to settle it before reclaiming"
            )

        now = _normalize_datetime(gl.message_raw["datetime"])
        if now < self.deadline_at:
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} bounty {self.bounty_id} is not reclaimable until {self.deadline_at} (now {now})"
            )
        if int(self.balance) <= 0:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} bounty {self.bounty_id} holds no balance")

        self.status = STATUS_REFUNDED
        Payee(self.poster).emit_transfer(value=u256(int(self.balance)))
        return self.status

    # ------------------------------------------------------------------
    # Non-deterministic core
    # ------------------------------------------------------------------
    def _fact_check(self) -> dict:
        """Leader-derived fact check, independently reproduced by validators."""
        url = self.source_url
        repo = self.repo_full_name
        threshold = int(self.threshold)

        def leader_fn() -> dict:
            return _derive_fact(url, repo, threshold)

        def validator_fn(leaders_res: gl.vm.Result) -> bool:
            if not isinstance(leaders_res, gl.vm.Return):
                # The leader blew up before producing a verdict. Never let that
                # become state: disagree and force a validator rotation.
                return False
            try:
                mine = _derive_fact(url, repo, threshold)
            except Exception:
                # A fetch failure for the validator is a disagreement, not a
                # pass. Assuming success here would let one bad network read
                # mint a payout.
                return False
            return _same_derivation(leaders_res.calldata, mine)

        return gl.vm.run_nondet_unsafe(leader_fn, validator_fn)

    # ------------------------------------------------------------------
    # Views
    # ------------------------------------------------------------------
    @gl.public.view
    def get_status(self) -> dict:
        """Full single-bounty detail. Read by the registry to aggregate."""
        return {
            "bounty_id": self.bounty_id,
            "status": self.status,
            "claim": self.repo_full_name,
            "threshold": int(self.threshold),
            "source_url": self.source_url,
            "reward_atto": int(self.reward_atto),
            "poster": self.poster.as_hex,
            "reporter": self.reporter,
            "reported_verdict": self.reported_verdict,
            "truth_verdict": self.truth_verdict,
            "fact_check_reachable": bool(self.last_fact_check.reachable),
            "fact_check_repo_ok": bool(self.last_fact_check.repo_ok),
            "fact_check_met": bool(self.last_fact_check.met),
            "evidence": self.evidence,
            "created_at": self.created_at,
            "deadline_at": self.deadline_at,
            "escrow_atto": int(self.balance),
        }

    @gl.public.view
    def get_evidence(self) -> dict:
        """Just the verification artefacts, without the money fields."""
        return {
            "status": self.status,
            "source_url": self.source_url,
            "reported_verdict": self.reported_verdict,
            "truth_verdict": self.truth_verdict,
            "fact_check_reachable": bool(self.last_fact_check.reachable),
            "fact_check_repo_ok": bool(self.last_fact_check.repo_ok),
            "fact_check_met": bool(self.last_fact_check.met),
            "evidence": self.evidence,
        }
