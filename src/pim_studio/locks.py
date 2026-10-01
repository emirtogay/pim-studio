"""Locks that hold across processes, and across machines.

Every long job here already refuses to run twice — but each of them refuses in
a way that only its own kind of caller can see. The panel holds a
`threading.Lock`, which one uvicorn process understands and nothing else does.
The cron jobs hold an `flock` on the host's `/tmp`, which the app container's
filesystem does not share. So the two have been unable to see each other, and
the only thing preventing a collision was that the panel could not reach the
brand library at all. Host networking removed that accident, and this replaces
it with something deliberate.

PostgreSQL advisory locks are the natural fit because both sides already share
the database. Nothing else has to be mounted, published, or agreed on: a lock
taken by the panel is visible to a `docker run --network host` container
started by cron, because the lock lives where both of them already look.

**They are held by a connection, not a transaction.** `pg_advisory_lock` is
released when its connection ends, so the connection behind it has to stay
checked out for the whole run — which is why this owns one rather than
borrowing the caller's.

**And it is held in AUTOCOMMIT, which is the part that is easy to get wrong.**
A plain `Session.execute` opens a transaction and leaves it open, so the lock
would be held by a connection sitting *idle in transaction* for the eight
minutes a publish takes. That pins a snapshot and blocks vacuum across the
database for the duration, and worse, a server with
`idle_in_transaction_session_timeout` set would terminate the connection
mid-run — releasing the lock silently while the run carried on believing it
held one. This instance has that timeout at 0 today, and a lock whose
correctness depends on a server setting nobody is watching is not a lock.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .logging import log

#: The lock an image publish holds.
#:
#: Defined here rather than at either caller because the panel and
#: `pim-studio-images.sh` must agree on the string exactly. Two literals that
#: happen to match today are a place for them to stop matching later, and the
#: failure would be silent: the lock would simply stop excluding anything, and
#: nothing anywhere would report that it had.
PUBLISH = "brandlibrary_publish"

#: The lock a push to Omnitron holds. Its own name, not `PUBLISH`: an image
#: publish and a value push can run at once without harm, and sharing one
#: lock would only make the panel report the wrong job as busy.
OMNITRON_PUSH = "omnitron_push"


def key_for(name: str) -> int:
    """A stable 64-bit key for a lock name.

    Hashed rather than enumerated so a new lock needs no registry and two
    callers naming the same thing agree without coordinating. Signed, because
    `pg_try_advisory_lock` takes a bigint and Postgres has no unsigned one — an
    unsigned digest would raise `NumericValueOutOfRange` for half of all names,
    which is the kind of bug that shows up on the day somebody adds a lock.
    """
    return int.from_bytes(
        hashlib.blake2b(name.encode("utf-8"), digest_size=8).digest(), "big", signed=True
    )


class Advisory:
    """A named lock, shared by every process pointed at the same database.

    Not a context manager, deliberately. The panel acquires in the request that
    starts a run and releases in the background thread that finishes it, and
    those are not the same `with` block. The CLI, which does have one scope,
    uses `held` below.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self._key = key_for(name)
        self._conn: Connection | None = None

    def acquire(self) -> bool:
        """Take the lock if it is free. Never waits.

        Waiting would be the wrong shape for both callers: the panel is
        answering a request, and cron would rather report that a run is already
        going than queue a second one behind an eight-minute scan.
        """
        from .db import SessionLocal

        # The sessionmaker's bind rather than the module-level engine, so that
        # a test redirecting `SessionLocal` at a throwaway database is actually
        # followed — otherwise this reaches past the fixture and locks against
        # whatever the environment happens to be configured for.
        engine = SessionLocal.kw["bind"]
        if engine.dialect.name != "postgresql":
            # SQLite: development and the test suite, where there is one
            # process and its in-process lock is the whole story. Granting it
            # keeps the caller's shape identical on both databases; refusing
            # would make every test look like a contended run.
            self._conn = None
            return True
        conn = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
        try:
            got = bool(
                conn.execute(text("select pg_try_advisory_lock(:key)"), {"key": self._key}).scalar()
            )
        except Exception:
            conn.close()
            raise
        if not got:
            conn.close()
            return False
        self._conn = conn
        return True

    def release(self) -> None:
        """Give it back. Safe to call when it was never held."""
        conn, self._conn = self._conn, None
        if conn is None:
            return
        try:
            conn.execute(text("select pg_advisory_unlock(:key)"), {"key": self._key})
        except Exception as exc:  # noqa: BLE001 — closing the connection frees it anyway
            log.warning("advisory_unlock_failed", lock=self.name, error=str(exc))
        finally:
            conn.close()


@contextmanager
def held(name: str) -> Iterator[bool]:
    """`Advisory` for a caller whose acquire and release share one scope.

    Yields whether the lock was taken rather than raising when it was not.
    "Somebody else is publishing" is not an error for the job that discovers
    it — it is a reason to stop quietly — and the caller is better placed than
    this to decide what to say about that.
    """
    lock = Advisory(name)
    got = lock.acquire()
    try:
        yield got
    finally:
        lock.release()
