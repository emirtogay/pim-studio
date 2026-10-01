"""Failure notifications — SMTP email and Microsoft Teams webhook.

Every destination is optional. If `settings.notifier_smtp_host` is unset,
SMTP is silently skipped; same for the Teams webhook. That lets operators
configure just one, both, or neither without code changes — useful during
local development (no notifications) and different for staging vs prod.

Three alert shapes are supported:

- `notify_scrape_failure(ScrapeFailure)` — a single run has failed after
  retries. Fires from the scheduler's job wrapper.
- `notify_stale_channel(StaleChannel)` — a channel hasn't had a successful
  run in N days. Fires from the daily staleness check. Catches the failure
  mode the per-run alert misses: a scheduler that never fires at all
  (the May 2026 Zalando outage).
- `notify_coverage_drop(CoverageDrop)` — a channel still runs and still
  writes rows, but far fewer than it normally does. Catches the failure mode
  both of the others miss: Zalando returned 24, 30 and 35 rows on separate
  occasions against a ~1,000-row baseline, and every one of those runs looked
  healthy to a check that only asks "did any data land?".

Errors from one destination don't prevent the others from firing — we log
and continue.
"""

from __future__ import annotations

import contextlib
import json
import smtplib
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from typing import Any

import httpx

from .config import settings
from .logging import log


@dataclass(frozen=True, slots=True)
class ScrapeFailure:
    """The payload that describes a failing / partial / zero-row scrape."""

    channel: str
    run_id: int
    status: str
    rows_written: int
    duration_s: float | None
    error_summary: str | None


def _compose_subject(f: ScrapeFailure) -> str:
    return f"[Northwind Prices] {f.channel} scrape {f.status}"


def _compose_body(f: ScrapeFailure) -> str:
    lines = [
        f"Channel: {f.channel}",
        f"Run ID:  {f.run_id}",
        f"Status:  {f.status}",
        f"Rows:    {f.rows_written}",
    ]
    if f.duration_s is not None:
        lines.append(f"Took:    {f.duration_s:.1f}s")
    if f.error_summary:
        lines.append("")
        lines.append("Error summary (first 20 errors):")
        lines.append(f.error_summary)
    return "\n".join(lines)


def _send_smtp(f: ScrapeFailure) -> None:
    """Send the alert as a plain-text email. No-op if SMTP config missing."""
    host = settings.notifier_smtp_host
    to_addrs = settings.notifier_smtp_to
    from_addr = settings.notifier_smtp_from or settings.notifier_smtp_user
    if not host or not to_addrs or not from_addr:
        return
    msg = EmailMessage()
    msg["Subject"] = _compose_subject(f)
    msg["From"] = from_addr
    # Support comma-separated recipient lists in the env var.
    recipients = [a.strip() for a in to_addrs.split(",") if a.strip()]
    msg["To"] = ", ".join(recipients)
    msg.set_content(_compose_body(f))

    try:
        with smtplib.SMTP(host, settings.notifier_smtp_port, timeout=30) as smtp:
            # Upgrade to TLS when the server supports it (any modern host).
            # Swallow the error when it doesn't — some internal Northwind SMTP
            # relays may accept unauthenticated plain submissions.
            with contextlib.suppress(smtplib.SMTPException):
                smtp.starttls()
            if settings.notifier_smtp_user and settings.notifier_smtp_pass:
                smtp.login(
                    settings.notifier_smtp_user,
                    settings.notifier_smtp_pass,
                )
            smtp.send_message(msg, from_addr=from_addr, to_addrs=recipients)
        log.info("notifier: sent SMTP alert", channel=f.channel, run_id=f.run_id)
    except Exception as e:  # noqa: BLE001 — log & continue, don't crash the scheduler
        log.error(
            "notifier: SMTP send failed",
            channel=f.channel,
            run_id=f.run_id,
            error=str(e),
        )


def _send_teams(f: ScrapeFailure) -> None:
    """Post the alert to a Teams incoming webhook. No-op if URL missing.

    Uses the legacy MessageCard format (connectors API) — the newer
    Workflow-adaptive-card format would work too but the MessageCard format
    still works on every Teams tenant today and needs no app installation.
    """
    url = settings.notifier_teams_webhook_url
    if not url:
        return

    theme_color_by_status = {
        "failed": "C62828",   # red
        "partial": "EF6C00",  # orange
        "success": "2E7D32",  # green — unused for alerts but keep for symmetry
    }
    theme = theme_color_by_status.get(f.status, "C62828")

    facts: list[dict[str, str]] = [
        {"name": "Channel", "value": f.channel},
        {"name": "Run ID", "value": str(f.run_id)},
        {"name": "Status", "value": f.status},
        {"name": "Rows", "value": str(f.rows_written)},
    ]
    if f.duration_s is not None:
        facts.append({"name": "Duration", "value": f"{f.duration_s:.1f}s"})

    card: dict[str, Any] = {
        "@type": "MessageCard",
        "@context": "https://schema.org/extensions",
        "themeColor": theme,
        "summary": _compose_subject(f),
        "title": f"Northwind price scrape {f.status}: {f.channel}",
        "sections": [{"facts": facts, "markdown": True}],
    }
    if f.error_summary:
        card["sections"].append(
            {"title": "Error summary", "text": f"```\n{f.error_summary}\n```"}
        )

    try:
        resp = httpx.post(
            url,
            content=json.dumps(card),
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
        resp.raise_for_status()
        log.info("notifier: sent Teams alert", channel=f.channel, run_id=f.run_id)
    except Exception as e:  # noqa: BLE001
        log.error(
            "notifier: Teams post failed",
            channel=f.channel,
            run_id=f.run_id,
            error=str(e),
        )


def notify_scrape_failure(failure: ScrapeFailure) -> None:
    """Dispatch a scrape-failure alert to every configured destination.

    Idempotent: callers can fire this whether or not SMTP/Teams are
    configured. Uses independent error handling per destination so one
    misconfigured channel doesn't block the other.
    """
    _send_smtp(failure)
    _send_teams(failure)


# ── Staleness alerts ───────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class StaleChannel:
    """A channel that hasn't had a successful scrape in too long.

    `last_success_at` is None when the channel has never produced a
    successful run (fresh deploy, brand-new channel, or persistent block).
    """

    channel: str
    last_success_at: datetime | None
    threshold_days: int


def _format_staleness(s: StaleChannel) -> tuple[str, str]:
    """Return (subject, body) for a stale-channel alert."""
    subject = f"[Northwind Prices] {s.channel} has not scraped in {s.threshold_days}+ days"
    if s.last_success_at is None:
        last_line = "Last success: never"
    else:
        last_line = f"Last success: {s.last_success_at.isoformat()}"
    body = "\n".join([
        f"Channel:   {s.channel}",
        last_line,
        f"Threshold: {s.threshold_days} days",
        "",
        "The scheduler may not be firing this channel, or every recent",
        "run has failed/partialed. Check scrape_runs for this channel and",
        "the scheduler's last_fired_at on the schedules row.",
    ])
    return subject, body


def _send_stale_smtp(s: StaleChannel) -> None:
    host = settings.notifier_smtp_host
    to_addrs = settings.notifier_smtp_to
    from_addr = settings.notifier_smtp_from or settings.notifier_smtp_user
    if not host or not to_addrs or not from_addr:
        return
    subject, body = _format_staleness(s)
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_addr
    recipients = [a.strip() for a in to_addrs.split(",") if a.strip()]
    msg["To"] = ", ".join(recipients)
    msg.set_content(body)
    try:
        with smtplib.SMTP(host, settings.notifier_smtp_port, timeout=30) as smtp:
            with contextlib.suppress(smtplib.SMTPException):
                smtp.starttls()
            if settings.notifier_smtp_user and settings.notifier_smtp_pass:
                smtp.login(
                    settings.notifier_smtp_user,
                    settings.notifier_smtp_pass,
                )
            smtp.send_message(msg, from_addr=from_addr, to_addrs=recipients)
        log.info("notifier: sent stale-channel SMTP alert", channel=s.channel)
    except Exception as e:  # noqa: BLE001
        log.error(
            "notifier: stale-channel SMTP send failed",
            channel=s.channel,
            error=str(e),
        )


def _send_stale_teams(s: StaleChannel) -> None:
    url = settings.notifier_teams_webhook_url
    if not url:
        return
    subject, _ = _format_staleness(s)
    facts: list[dict[str, str]] = [
        {"name": "Channel", "value": s.channel},
        {
            "name": "Last success",
            "value": s.last_success_at.isoformat() if s.last_success_at else "never",
        },
        {"name": "Threshold", "value": f"{s.threshold_days} days"},
    ]
    card: dict[str, Any] = {
        "@type": "MessageCard",
        "@context": "https://schema.org/extensions",
        # Orange — staleness is between "this run failed" (red) and "all good"
        # (green). Distinct colour helps on-call distinguish at a glance.
        "themeColor": "EF6C00",
        "summary": subject,
        "title": f"Northwind price scrape stale: {s.channel}",
        "sections": [{"facts": facts, "markdown": True}],
    }
    try:
        resp = httpx.post(
            url,
            content=json.dumps(card),
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
        resp.raise_for_status()
        log.info("notifier: sent stale-channel Teams alert", channel=s.channel)
    except Exception as e:  # noqa: BLE001
        log.error(
            "notifier: stale-channel Teams post failed",
            channel=s.channel,
            error=str(e),
        )


def notify_stale_channel(stale: StaleChannel) -> None:
    """Dispatch a stale-channel alert to every configured destination.

    Same dispatch shape as `notify_scrape_failure`: no-op when SMTP/Teams
    aren't configured, independent error handling per destination.
    """
    _send_stale_smtp(stale)
    _send_stale_teams(stale)


# ── Shared transports ──────────────────────────────────────────────────────
#
# The two alert shapes above each carry their own copy of the SMTP and Teams
# plumbing. Rather than add a third copy for coverage drops, the transport is
# factored out here. The older paths are left on their own copies deliberately:
# they are the alerts about to be switched on in production, and a refactor of
# working notification code is not worth the risk today.


def _send_email(subject: str, body: str, **log_ctx: Any) -> None:
    """Send a plain-text email. No-op when SMTP isn't fully configured."""
    host = settings.notifier_smtp_host
    to_addrs = settings.notifier_smtp_to
    from_addr = settings.notifier_smtp_from or settings.notifier_smtp_user
    if not host or not to_addrs or not from_addr:
        return
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_addr
    recipients = [a.strip() for a in to_addrs.split(",") if a.strip()]
    msg["To"] = ", ".join(recipients)
    msg.set_content(body)
    try:
        with smtplib.SMTP(host, settings.notifier_smtp_port, timeout=30) as smtp:
            with contextlib.suppress(smtplib.SMTPException):
                smtp.starttls()
            if settings.notifier_smtp_user and settings.notifier_smtp_pass:
                smtp.login(settings.notifier_smtp_user, settings.notifier_smtp_pass)
            smtp.send_message(msg, from_addr=from_addr, to_addrs=recipients)
        log.info("notifier: sent SMTP alert", **log_ctx)
    except Exception as e:  # noqa: BLE001 — log & continue, never crash the caller
        log.error("notifier: SMTP send failed", error=str(e), **log_ctx)


def _post_teams(card: dict[str, Any], **log_ctx: Any) -> None:
    """POST a MessageCard to the Teams webhook. No-op when the URL is unset."""
    url = settings.notifier_teams_webhook_url
    if not url:
        return
    try:
        resp = httpx.post(
            url,
            content=json.dumps(card),
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
        resp.raise_for_status()
        log.info("notifier: sent Teams alert", **log_ctx)
    except Exception as e:  # noqa: BLE001
        log.error("notifier: Teams post failed", error=str(e), **log_ctx)


# ── Coverage-drop alerts ───────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class CoverageDrop:
    """A channel that still scrapes but returns far fewer rows than usual.

    `baseline_rows` is the median row count of the previous `baseline_runs`
    runs that wrote anything. The alert fires when `latest_rows` falls below
    `ratio_threshold` of it.
    """

    channel: str
    run_id: int
    latest_rows: int
    baseline_rows: float
    ratio_threshold: float
    baseline_runs: int

    @property
    def ratio(self) -> float:
        if not self.baseline_rows:
            return 1.0
        return self.latest_rows / self.baseline_rows


def _format_coverage(c: CoverageDrop) -> tuple[str, str]:
    """Return (subject, body) for a coverage-drop alert."""
    pct = round(c.ratio * 100)
    subject = (
        f"[Northwind Prices] {c.channel} coverage dropped to {pct}% of normal"
    )
    body = "\n".join([
        f"Channel:  {c.channel}",
        f"Run ID:   {c.run_id}",
        f"Rows:     {c.latest_rows}",
        f"Baseline: {c.baseline_rows:.0f} (median of the last "
        f"{c.baseline_runs} runs that wrote rows)",
        f"Ratio:    {pct}% (alerts below "
        f"{round(c.ratio_threshold * 100)}%)",
        "",
        "The scrape ran and wrote rows, so it will not show as failed or",
        "stale — but it returned far less than this channel normally does.",
        "Usual causes: the retailer changed its listing markup so pagination",
        "stops early, or it started rate-limiting partway through the run.",
        "Check the run's error_summary in scrape_runs.",
    ])
    return subject, body


def notify_coverage_drop(drop: CoverageDrop) -> None:
    """Dispatch a coverage-drop alert to every configured destination."""
    subject, body = _format_coverage(drop)
    _send_email(subject, body, channel=drop.channel, run_id=drop.run_id)

    pct = round(drop.ratio * 100)
    card: dict[str, Any] = {
        "@type": "MessageCard",
        "@context": "https://schema.org/extensions",
        # Amber — the scrape worked, the data is thin. Between stale (orange)
        # and outright failure (red).
        "themeColor": "F9A825",
        "summary": subject,
        "title": f"Northwind price scrape coverage drop: {drop.channel}",
        "sections": [{
            "facts": [
                {"name": "Channel", "value": drop.channel},
                {"name": "Run ID", "value": str(drop.run_id)},
                {"name": "Rows", "value": str(drop.latest_rows)},
                {"name": "Baseline", "value": f"{drop.baseline_rows:.0f}"},
                {"name": "Ratio", "value": f"{pct}%"},
            ],
            "markdown": True,
        }],
    }
    _post_teams(card, channel=drop.channel, run_id=drop.run_id)
