"""Reconciling our tables against Omnitron, and surviving a bad answer.

The report's whole design is that "we could not read this" is a first-class
result: `_remote` returns `int | None`, `Check.drift` returns `None` when either
side is unknown, and `IntegrityReport.clean` skips the rows it cannot judge. A
502 on one endpoint is meant to cost one line of the report, not the report.

`sum()` was the one place that contract did not hold, and it sat on the only
check that makes more than one request — four to six per market against one
each for the other two. So the check most likely to meet a bad response was the
only one that could not survive it, and it failed at exactly the moment the
report is wanted, since the reason to run it is usually that Omnitron is already
behaving oddly.

These pin the degradation rather than the arithmetic. The counts themselves are
whatever the tenant says; what has to hold is that one unreadable catalogue
produces an unknown, an unknown produces no drift, and the report still comes
back.
"""

from __future__ import annotations

import pytest

from pim_studio.omnitron.client import OmnitronError
from pim_studio.reports.integrity import Check, IntegrityReport, _remote, _remote_total

pytestmark = pytest.mark.usefixtures("temp_db")


class FakeApi:
    """Answers with a count, or raises on the nth call.

    `fail_on` is 1-based and 0 means "never fail", so a test names the position
    of the bad response rather than constructing a response sequence.
    """

    def __init__(self, count: int = 100, fail_on: int = 0) -> None:
        self.count = count
        self.fail_on = fail_on
        self.calls = 0

    def get(self, path: str, params: dict | None = None) -> dict:
        self.calls += 1
        if self.calls == self.fail_on:
            raise OmnitronError("502 Bad Gateway")
        return {"count": self.count}


# ── the primitive ──────────────────────────────────────────────────────────


def test_a_readable_pair_of_catalogues_adds_up() -> None:
    assert _remote_total(FakeApi(count=100), "/api/v1/catalog_items/", (4, 601)) == 200


@pytest.mark.parametrize("fail_on", [1, 2], ids=["first-read", "second-read"])
def test_one_unreadable_catalogue_makes_the_total_unknown(fail_on: int) -> None:
    """Unknown, not a partial sum, and not an exception.

    Whichever request fails, the answer is the same — a partial total is the
    dangerous middle option here: "three of PL's four catalogues" looks like an
    answer, and would print as a drift against our full membership, sending
    somebody after a shortfall that is only a missing request.
    """
    api = FakeApi(count=100, fail_on=fail_on)
    assert _remote_total(api, "/api/v1/catalog_items/", (4, 601)) is None


def test_the_total_does_not_raise_where_sum_did() -> None:
    """The regression, stated as the thing that used to happen.

    `sum(_remote(...) for ...)` raised `TypeError: unsupported operand type(s)
    for +: 'int' and 'NoneType'` on the first unreadable catalogue and took
    `check_integrity` down with it.
    """
    api = FakeApi(count=100, fail_on=2)
    try:
        result = _remote_total(api, "/api/v1/catalog_items/", (4, 601))
    except TypeError as exc:  # pragma: no cover — this is the bug, not the fix
        pytest.fail(f"_remote_total raised instead of degrading: {exc}")
    assert result is None


def test_a_single_bad_response_is_already_absorbed_by_remote() -> None:
    """`_remote_total` is only correct because `_remote` already abstains.
    Pinned here so a change to one is not made without seeing the other."""
    assert _remote(FakeApi(fail_on=1), "/api/v1/attributes/") is None


# ── what the report does with an unknown ────────────────────────────────────


def test_an_unknown_side_yields_no_drift_and_is_not_a_failure() -> None:
    """A row nobody could read must not read as a row that disagrees.

    `ok` is False for an unknown — it is not a pass — but `clean` filters on
    `drift is not None`, so an unreadable check leaves the report clean rather
    than reporting a fault in the data it never saw.
    """
    check = Check("catalogue membership PL", ours=4116, theirs=None)
    assert check.drift is None
    assert check.ok is False

    report = IntegrityReport(counts=[check])
    assert report.clean is True


def test_a_genuine_disagreement_is_still_reported() -> None:
    """The other direction: degrading gracefully must not degrade into silence."""
    report = IntegrityReport(counts=[Check("catalogue membership PL", 4116, 4200)])
    assert report.counts[0].drift == -84
    assert report.clean is False
