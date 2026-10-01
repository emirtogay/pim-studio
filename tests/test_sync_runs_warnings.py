"""A run in progress is not a run that died.

`/api/sync/runs` reports rows still at `running` so the page can say a process
died without closing its row. It listed every such row, including the one
started ninety seconds ago by the cron — under a heading that reads "not one
still working". The more often stages run, the more often the page called a
healthy crawl a corpse, which is the wrong direction for a warning to move.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from pim_studio.admin import accounts
from pim_studio.admin import app as app_module
from pim_studio.db import session_scope
from pim_studio.models import SyncRun, utcnow

pytestmark = pytest.mark.usefixtures("temp_db")

PASSWORD = "a-long-enough-one"


def _running(kind: str, hours_ago: float) -> None:
    with session_scope() as db:
        db.add(
            SyncRun(
                kind=kind,
                status="running",
                mode="full",
                started_at=utcnow() - timedelta(hours=hours_ago),
                rows_seen=0,
                rows_imported=0,
                rows_dropped=0,
                rows_retired=0,
            )
        )


@pytest.fixture
def client() -> Iterator[TestClient]:
    with session_scope() as db:
        accounts.create_user(
            db, email="dev@example.com", password=PASSWORD, role="developer"
        )
    c = TestClient(app_module.app, raise_server_exceptions=False)
    assert (
        c.post(
            "/api/auth/login", json={"email": "dev@example.com", "password": PASSWORD}
        ).status_code
        == 200
    )
    yield c


def test_a_run_that_started_moments_ago_is_not_reported_dead(
    client: TestClient,
) -> None:
    """The case the cron creates every two hours during trading."""
    _running("storefront", hours_ago=0.02)
    body = client.get("/api/sync/runs").json()
    assert body["unfinished"] == []


def test_a_run_longer_than_any_stage_takes_is_reported(client: TestClient) -> None:
    """The longest real stage is ~35 minutes; four hours is dead."""
    _running("omnitron_values", hours_ago=4)
    body = client.get("/api/sync/runs").json()
    assert [u["kind"] for u in body["unfinished"]] == ["omnitron_values"]


def test_the_floor_is_not_so_high_that_a_stuck_row_waits_a_day(
    client: TestClient,
) -> None:
    """A row stuck overnight must surface the same morning, not the next one."""
    _running("omnitron_catalogue", hours_ago=8)
    body = client.get("/api/sync/runs").json()
    assert len(body["unfinished"]) == 1


def test_a_finished_run_is_never_unfinished(client: TestClient) -> None:
    """The guard narrows `running`; it must not widen it."""
    with session_scope() as db:
        db.add(
            SyncRun(
                kind="storefront",
                status="success",
                mode="full",
                started_at=utcnow() - timedelta(hours=9),
                finished_at=utcnow() - timedelta(hours=8),
                rows_seen=1,
                rows_imported=1,
                rows_dropped=0,
                rows_retired=0,
            )
        )
    body = client.get("/api/sync/runs").json()
    assert body["unfinished"] == []
