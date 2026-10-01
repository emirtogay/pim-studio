"""Panel accounts from a terminal — `python -m pim_studio.admin.users`.

    python -m pim_studio.admin.users add you@example.com --role developer
    python -m pim_studio.admin.users passwd you@example.com
    python -m pim_studio.admin.users role you@example.com developer
    python -m pim_studio.admin.users disable someone@example.com
    python -m pim_studio.admin.users enable someone@example.com
    python -m pim_studio.admin.users list

This existed before the Users page did, because a deployed panel with no way
to create an account is unopenable by anyone including its author. It stays
after it, for two reasons that are not going away:

**It is the bootstrap.** The first account cannot be created through a page
that requires an account. Deliberately not a signup route — this panel has a
handful of named users and a public path to account creation would be a
liability, not a convenience.

**It is the escape hatch.** The API refuses to let anyone demote or deactivate
themselves, or to strip the last active developer, precisely so the panel
cannot be closed from inside it. Those guards have to be defeatable from
somewhere, and "somewhere" should require a shell on the server. So `role` and
`disable` here do what they are told.

Everything else is shared with the API through `accounts.py`, so the two
cannot disagree about what a valid password is or how an email is stored —
and every write lands in the same audit log the Activity page reads, marked
`CLI`. A page that showed accounts appearing with nobody creating them would
be worse than no page.

The password is read from a prompt, never from an argument: a shell history is
a bad place for one, and on a shared server so is the process list.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys

from sqlalchemy import func, select

from ..db import session_scope
from ..models import User
from . import accounts, audit
from .auth import ROLES


def _read_password(confirm: bool = True) -> str:
    pw = getpass.getpass("Password: ")
    accounts.validate_password(pw)
    if confirm and pw != getpass.getpass("Repeat: "):
        raise SystemExit("Passwords did not match.")
    return pw


def _actor() -> str:
    """Who is at the keyboard, as far as the shell knows.

    Not an identity claim — it is whatever `$USER` says on a box people reach
    over SSH. It is recorded because "someone with a shell" is still a more
    useful answer than a null column, and the `CLI` method beside it says
    exactly how much the name is worth.
    """
    return os.environ.get("SUDO_USER") or os.environ.get("USER") or "unknown"


def _record(pending: list[tuple[str, str]], action: str, detail: str) -> None:
    """Queue an audit row for after the transaction commits.

    Not written inline: `audit.record` opens its own session, so writing it
    from inside `session_scope` would either deadlock on SQLite or — worse on
    Postgres — record a change that the enclosing transaction then rolled back.
    An audit log that describes work which did not happen is not a smaller
    problem than one that misses work which did.
    """
    pending.append((action, detail))


def _flush(pending: list[tuple[str, str]]) -> None:
    actor = f"{_actor()} (cli)"
    for action, detail in pending:
        audit.record(
            action=action,
            user_email=actor,
            method=audit.CLI_METHOD,
            detail=detail,
        )


def _find(db, email: str) -> User:
    """Look the account up, exactly first and case-insensitively second.

    Rows created before emails were normalised on write exist, and they are
    the ones somebody reaches for this CLI to repair — an account stored as
    `Emir@example.com` cannot sign in, because `authenticate()` looks for the
    lowercase form. You cannot fix what you cannot find, so the fallback finds
    it whichever casing was typed.
    """
    address = (email or "").strip().lower()
    user = db.scalar(select(User).where(User.email == address))
    if user is None:
        user = db.scalar(select(User).where(func.lower(User.email) == address))
    if user is None:
        raise SystemExit(f"no such user: {email}")
    return user


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    add = sub.add_parser("add", help="create a user")
    add.add_argument("email")
    add.add_argument("--name", default=None)
    add.add_argument("--role", choices=ROLES, default="planning")

    pw = sub.add_parser("passwd", help="change a password")
    pw.add_argument("email")

    role = sub.add_parser("role", help="change a role")
    role.add_argument("email")
    role.add_argument("role", choices=ROLES)

    disable = sub.add_parser("disable", help="stop an account signing in")
    disable.add_argument("email")

    enable = sub.add_parser("enable", help="let an account sign in again")
    enable.add_argument("email")

    sub.add_parser("list", help="list users")

    args = ap.parse_args(argv)

    pending: list[tuple[str, str]] = []
    try:
        return _run(args, pending)
    except accounts.AccountError as exc:
        # The rules speak in whole sentences; there is nothing to add.
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        # After `_run` — and so after `session_scope` committed or rolled back.
        # `pending` is empty on any path that did not get that far.
        _flush(pending)


def _run(args: argparse.Namespace, pending: list[tuple[str, str]]) -> int:
    with session_scope() as db:
        if args.cmd == "list":
            users = accounts.list_users(db)
            if not users:
                print("no users — create one with `add`")
                return 0
            developers = accounts.active_developers(db)
            for u in users:
                flag = "" if u.is_active else "  (disabled)"
                print(f"  {u.email:38} {u.role:10} {u.name or '':20}{flag}")
            if developers == 0:
                print(
                    "\nNo active developer. Nobody can reach Users, Sync Runs "
                    "or the Activity log — fix with `role <email> developer`.",
                    file=sys.stderr,
                )
            return 0

        if args.cmd == "add":
            user = accounts.create_user(
                db,
                email=args.email,
                password=_read_password(),
                role=args.role,
                name=args.name,
            )
            print(f"created {user.email} ({user.role})")
            _record(pending, audit.CREATED_USER, f"{user.email} as {user.role}")
            return 0

        user = _find(db, args.email)

        if args.cmd == "passwd":
            accounts.set_password(db, user, _read_password())
            print(f"password updated for {user.email}")
            _record(pending, audit.RESET_PASSWORD, f"for {user.email}")
            return 0

        if args.cmd in ("role", "disable", "enable"):
            # `force=True` lifts both lockout guards on purpose — see the
            # module docstring. Locking yourself out is exactly the situation
            # this command exists to undo, so it is the one caller that must
            # be able to walk past them.
            changes = accounts.update_user(
                db,
                user,
                actor_id=None,
                role=args.role if args.cmd == "role" else None,
                is_active={"disable": False, "enable": True}.get(args.cmd),
                force=True,
            )
            if not changes:
                print(f"{user.email} was already like that — nothing changed")
                return 0
            summary = ", ".join(changes)
            print(f"{user.email}: {summary}")
            _record(pending, audit.UPDATED_USER, f"{user.email}: {summary}")
            if accounts.active_developers(db) == 0:
                print(
                    "\nThat leaves no active developer. The panel's admin "
                    "pages are now unreachable from the browser.",
                    file=sys.stderr,
                )
            return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
