"""Mirroring Omnitron's registry.

The behaviours pinned here are the ones that decide whether the mirror can be
trusted to answer "what changed upstream": a second run must be a no-op, a
truncated read must not be mistaken for a mass deletion, and a genuine
deletion must actually propagate.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import select

from pim_studio.db import session_scope
from pim_studio.models import OmnitronAttribute, OmnitronAttributeValue
from pim_studio.omnitron import OmnitronClient
from pim_studio.sync import registry
from pim_studio.sync.runs import SyncRun

pytestmark = pytest.mark.usefixtures("temp_db")

CREDS = {
    "base_url": "https://pim.example.test",
    "username": "u",
    "password": "p",
}


def attribute(pk: int, key: str = "integration_yaka", **over) -> dict:
    """An attribute shaped like the live tenant's — all 21 fields present."""
    base = {
        "pk": pk,
        "key": key,
        "name": "Yaka",
        "data_type": "dropdown",
        "erp_code": key,
        "description": "",
        "default_value": None,
        "is_required": False,
        "is_visible": True,
        "is_searchable": True,
        "is_filterable": True,
        "is_variant": False,
        "is_variant_listable": False,
        "is_form_required": False,
        "is_form_field_required": False,
        "is_offer": False,
        "pre_attribute": False,
        "is_localizable": False,
        "entity_type": 2,
        "created_date": "2018-03-26T08:47:23.828816Z",
        "modified_date": "2026-07-01T06:34:45.800235Z",
    }
    return {**base, **over}


def api_serving(records: list[dict]) -> OmnitronClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"count": len(records), "next": None, "results": records}
        )

    c = OmnitronClient(**CREDS)
    c._http = httpx.Client(transport=httpx.MockTransport(handler))
    c._token = "test-token"
    return c


def a_run() -> SyncRun:
    return SyncRun(row_id=1, kind="omnitron_registry")


def held() -> list[OmnitronAttribute]:
    with session_scope() as db:
        return list(db.scalars(select(OmnitronAttribute).order_by(OmnitronAttribute.pk)))


# ── the basic mirror ───────────────────────────────────────────────────────


def test_first_run_inserts_everything() -> None:
    stats = registry.sync_attributes(api_serving([attribute(1), attribute(2, "kalip")]), a_run())
    assert (stats["read"], stats["inserted"], stats["updated"]) == (2, 2, 0)
    assert [a.key for a in held()] == ["integration_yaka", "kalip"]


def test_datetimes_are_parsed_not_stored_as_strings() -> None:
    registry.sync_attributes(api_serving([attribute(1)]), a_run())
    row = held()[0]
    assert isinstance(row.modified_date, datetime)
    assert row.modified_date.year == 2026


def test_the_raw_data_type_string_is_preserved() -> None:
    """The tenant sends `area` and `bool`, which are not what the vendor's
    documentation lists (`text_area`, `boolean`). Store what arrives; mapping
    onto our own vocabulary is the reconciliation report's problem."""
    registry.sync_attributes(api_serving([attribute(1, data_type="area")]), a_run())
    assert held()[0].data_type == "area"


def test_a_record_with_no_pk_is_dropped_and_counted() -> None:
    run = a_run()
    record = attribute(1)
    del record["pk"]
    registry.sync_attributes(api_serving([record]), run)
    assert held() == []
    assert run.rows_dropped == 1


# ── idempotence, which is the whole point ──────────────────────────────────


def test_a_second_identical_run_changes_nothing() -> None:
    """The regression this guards is silent and only visible on the second run.

    Timestamps arrive from the API timezone-aware; SQLite's DateTime(timezone=
    True) hands them back naive. Same instant, unequal comparison — so every
    dated row reported as changed on every run, and the one signal the mirror
    exists to produce ("three vocabulary values changed today") was permanently
    meaningless. Nothing errored, which is why only a repeat run finds it.
    """
    records = [attribute(1), attribute(2, "kalip")]
    registry.sync_attributes(api_serving(records), a_run())

    stats = registry.sync_attributes(api_serving(records), a_run())
    assert stats["unchanged"] == 2
    assert (stats["inserted"], stats["updated"], stats["removed"]) == (0, 0, 0)


def test_a_genuine_change_is_reported_as_changed() -> None:
    registry.sync_attributes(api_serving([attribute(1)]), a_run())
    stats = registry.sync_attributes(api_serving([attribute(1, name="Collar")]), a_run())
    assert (stats["updated"], stats["unchanged"]) == (1, 0)
    assert held()[0].name == "Collar"


def test_a_changed_timestamp_alone_is_a_change() -> None:
    """The tz fix must not go so far that real timestamp movement is ignored."""
    registry.sync_attributes(api_serving([attribute(1)]), a_run())
    stats = registry.sync_attributes(
        api_serving([attribute(1, modified_date="2026-08-14T00:00:00.000000Z")]), a_run()
    )
    assert stats["updated"] == 1


def test_naive_and_aware_representations_of_one_instant_compare_equal() -> None:
    naive = datetime(2026, 7, 1, 6, 34, 45)
    aware = datetime(2026, 7, 1, 6, 34, 45, tzinfo=UTC)
    assert registry._same(naive, aware)
    assert not registry._same(naive, datetime(2026, 7, 1, 6, 34, 46, tzinfo=UTC))


# ── pruning, and refusing to prune ─────────────────────────────────────────


def test_a_row_gone_upstream_is_removed() -> None:
    registry.sync_attributes(
        api_serving([attribute(n, f"k{n}") for n in range(1, 11)]), a_run()
    )
    assert len(held()) == 10

    # One of ten disappears — a 10% shrink, under the guard.
    stats = registry.sync_attributes(
        api_serving([attribute(n, f"k{n}") for n in range(1, 10)]), a_run()
    )
    assert stats["removed"] == 1
    assert len(held()) == 9


def test_a_suspiciously_large_shrink_is_refused_not_applied() -> None:
    """A read that dies half way looks exactly like a catalogue that shrank.
    Pruning on that would delete most of the registry and report success."""
    registry.sync_attributes(
        api_serving([attribute(n, f"k{n}") for n in range(1, 11)]), a_run()
    )

    run = a_run()
    stats = registry.sync_attributes(api_serving([attribute(1, "k1")]), run)

    assert stats["removed"] == 0, "must not delete on an implausible shrink"
    assert len(held()) == 10, "every row must survive"
    assert run.rows_dropped == 9
    assert any("prune refused" in r for r in run.drop_reasons)


def test_an_empty_read_never_prunes() -> None:
    """Zero rows back is a broken read, not an emptied registry."""
    registry.sync_attributes(api_serving([attribute(1), attribute(2, "kalip")]), a_run())
    stats = registry.sync_attributes(api_serving([]), a_run())
    assert stats["removed"] == 0
    assert len(held()) == 2


# ── the vocabulary table ───────────────────────────────────────────────────


def test_attribute_values_mirror_with_their_labels() -> None:
    records = [
        {"pk": 1443, "attribute": 190, "value": "-", "label": "-", "erp_code": "-",
         "created_date": "2018-03-26T08:58:44.800866Z",
         "modified_date": "2018-03-26T08:58:44.800905Z", "order": None},
        {"pk": 1444, "attribute": 190, "value": "003", "label": "Düz", "erp_code": "003",
         "created_date": "2018-03-26T08:58:44.800866Z",
         "modified_date": "2018-03-26T08:58:44.800905Z", "order": 1},
    ]
    stats = registry.sync_attribute_values(api_serving(records), a_run())
    assert stats["inserted"] == 2

    with session_scope() as db:
        rows = list(db.scalars(select(OmnitronAttributeValue).order_by(OmnitronAttributeValue.pk)))
    assert [r.label for r in rows] == ["-", "Düz"]
    assert rows[1].value == "003"
