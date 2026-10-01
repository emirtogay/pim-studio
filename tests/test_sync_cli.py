"""The command line that stands between cron and the sync.

Every stage is careful about the full/incremental distinction, and none of that
care mattered, because the weekly reconciliation never reached a stage at all.
The cron line was `pim-studio-sync.sh --full`; the script's `${*:-all}`
substitutes only when there are *no* arguments, so it forwarded `--full` alone;
and `main` stripped the flag, found no positional left, and treated that as a
usage error. Help text, exit 2, every Sunday — while the nightly incrementals
reported green and `last_full_run` aged quietly past a fortnight. Departures
and withdrawn prices accumulated the whole time, which is the exact drift the
mode distinction exists to prevent.

Nothing in the suite referenced `--full` before these tests. That is the part
worth fixing: the flag gating every reconciliation sweep in the project had no
coverage of its own, so a wiring error in front of it was invisible.

Both ends are pinned here. The CLI must read a bare mode flag as `all` in that
mode, and the cron snippet must name a stage — belt and braces, because either
one alone would have prevented the outage and neither one did.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pim_studio.sync import __main__ as sync_cli

REPO_ROOT = Path(__file__).resolve().parents[1]


class _NoClient:
    """Stands in for `OmnitronClient`, which `main` builds before dispatching."""

    def __enter__(self) -> _NoClient:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


@pytest.fixture
def dispatched(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Run `main` as far as its dispatch, recording the mode it settled on."""
    seen: dict[str, object] = {}

    def _fake_run_all(api: object, full: bool = False) -> int:
        seen["full"] = full
        return 0

    monkeypatch.setattr(sync_cli, "OmnitronClient", _NoClient)
    monkeypatch.setattr(sync_cli, "configure_logging", lambda: None)
    monkeypatch.setattr(sync_cli, "_run_all", _fake_run_all)
    return seen


# ── a mode flag on its own is a request, not a syntax error ────────────────


def test_full_alone_runs_every_stage_in_full_mode(dispatched: dict) -> None:
    """The exact argv the weekly cron line produced for weeks.

    This returned 2 without running anything, which is the whole outage.
    """
    assert sync_cli.main(["--full"]) == 0
    assert dispatched == {"full": True}


def test_incremental_alone_runs_every_stage_incrementally(dispatched: dict) -> None:
    """The same rule for the other flag; `--incremental` is not a special case."""
    assert sync_cli.main(["--incremental"]) == 0
    assert dispatched == {"full": False}


def test_all_full_is_unchanged(dispatched: dict) -> None:
    """The explicit spelling keeps working. It is what the cron line says now."""
    assert sync_cli.main(["all", "--full"]) == 0
    assert dispatched == {"full": True}


def test_flag_order_does_not_matter(dispatched: dict) -> None:
    assert sync_cli.main(["--full", "all"]) == 0
    assert dispatched == {"full": True}


def test_a_named_stage_still_receives_the_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`sync catalogue --full` — the two-day reconciliation's cron line."""
    seen: dict[str, object] = {}

    def _fake_stage(api: object, full: bool) -> int:
        seen["full"] = full
        return 0

    monkeypatch.setattr(sync_cli, "OmnitronClient", _NoClient)
    monkeypatch.setattr(sync_cli, "configure_logging", lambda: None)
    monkeypatch.setitem(sync_cli.STAGES, "catalogue", (_fake_stage, "test double"))

    assert sync_cli.main(["catalogue", "--full"]) == 0
    assert seen == {"full": True}


# ── but genuinely empty input still explains itself ────────────────────────


def test_no_arguments_at_all_prints_usage() -> None:
    """Someone asking what this does, not asking for a four-hour sync.

    The distinction the old code missed: "no stage named" and "nothing asked
    for" look identical after the flags are stripped, and only one of them
    should refuse to run.
    """
    assert sync_cli.main([]) == 2


@pytest.mark.parametrize("flag", ["-h", "--help"])
def test_help_prints_usage(flag: str) -> None:
    assert sync_cli.main([flag]) == 2


def test_an_unknown_stage_is_still_rejected() -> None:
    assert sync_cli.main(["catalouge", "--full"]) == 2


# ── the cron snippet, which is the other half of the same bug ──────────────

# ── the stages that only the command line reaches ──────────────────────────
#
# `proposals` and `publication` are not in `all`, so the pipeline tests never
# touch them, and their bodies are the only place the `SyncRun` API is used
# directly. That gap shipped a real bug: the proposals stage read
# `run.row_id`, which is the constructor's parameter name and not an
# attribute — `SyncRun` stores it as `id`. Every unit test passed and the
# stage died on its first real invocation.
#
# These run the stage bodies against an empty database, which is enough: the
# failure was an AttributeError on the run object, not anything to do with
# what was found.


@pytest.mark.usefixtures("temp_db")
@pytest.mark.parametrize("stage", ["proposals", "publication"])
def test_a_manual_stage_runs_against_an_empty_database(stage: str) -> None:
    fn, _description = sync_cli.STAGES[stage]
    assert fn(_NoClient(), True) == 0


def test_only_the_push_is_kept_out_of_the_nightly_pass() -> None:
    """The line is what a stage *changes*, not how expensive it is.

    `push` writes to the live catalogue, so it stays something a person starts
    and confirms. `publication` only reads the four shops. `proposals` only
    fills a lane nothing reads until somebody accepts a row — leaving that to
    be remembered by hand meant a backlog that shrank only when someone
    thought of it.

    If a paid translator is ever configured, `proposals` becomes a stage that
    spends money unattended and this decision needs taking again. The comment
    in `pipeline.FULL_ORDER` says so where somebody would see it.
    """
    from pim_studio.sync.pipeline import FULL_ORDER

    for stage in ("proposals", "publication", "push"):
        assert stage in sync_cli.STAGES
    assert "push" not in FULL_ORDER
    assert "proposals" in FULL_ORDER
    assert "publication" in FULL_ORDER
    # And it runs after the stage whose output it reads.
    assert FULL_ORDER.index("proposals") > FULL_ORDER.index("values")
