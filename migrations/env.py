"""Alembic migration environment.

Wired to read the application's settings and models so `alembic revision
--autogenerate` picks up schema changes automatically and `alembic upgrade`
runs against whichever database `settings.database_url` points to (SQLite
in dev, Postgres in production).
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# Ensure src/ is on sys.path so `pim_studio` imports work when Alembic is
# invoked as a script from the project root.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from pim_studio.config import settings  # noqa: E402
from pim_studio.models import Base  # noqa: E402

# Alembic's Config object, driven by alembic.ini.
config = context.config

# Override the ini placeholder URL with the live application setting.
config.set_main_option("sqlalchemy.url", settings.database_url)

# Wire the Python logging config Alembic ships with.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# What Alembic compares when autogenerating revisions.
target_metadata = Base.metadata


# This module shares a database with the price-comparison app so that `users`
# — and therefore sign-on — is genuinely one table (05 - Decisions Log,
# 2026-08-18). Two apps in one database need distinct version tables or their
# migration histories collide; that collision was the stated reason for
# keeping them apart, and it is one setting.
VERSION_TABLE = "alembic_version_pm"


#: Tables this module maps but does not own. `users` belongs to the
#: price-comparison app now (05 - Decisions Log, 2026-08-18) — we read it for
#: sign-on and must never create, alter or drop it, because whichever app
#: migrated last would win and a column added by one would surprise the other.
#: `audit_log` is the price app's activity feed; ours is `pm_audit_log` and the
#: two are deliberately separate, so autogenerate must not offer to drop theirs
#: for being absent from our metadata. `alembic_version` is the pre-rename
#: history table, left in place on existing databases and irrelevant to the
#: model.
#:
#: Note this only filters autogenerate and `alembic check`. It does NOT gate
#: what a migration does when it runs — a migration body that touches one of
#: these tables will still touch it, which is how the baseline came to abort on
#: `audit_log` against the shared database.
NOT_OURS = {"users", "audit_log", "alembic_version"}


def include_object(obj, name, type_, reflected, compare_to):
    """Keep autogenerate and `alembic check` out of tables we do not own."""
    if type_ == "table" and name in NOT_OURS:
        return False
    if type_ == "index" and getattr(obj, "table", None) is not None:
        return obj.table.name not in NOT_OURS
    return True


def run_migrations_offline() -> None:
    """Run migrations without an engine — emits SQL to stdout.

    Useful for reviewing what a migration would do before applying it, and for
    generating SQL scripts in environments where the target DB isn't reachable
    from the machine running Alembic.
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        version_table=VERSION_TABLE,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live database connection."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # Render column comments and enum types explicitly on Postgres —
            # a no-op on SQLite, harmless either way.
            render_as_batch=connection.dialect.name == "sqlite",
            version_table=VERSION_TABLE,
            include_object=include_object,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
