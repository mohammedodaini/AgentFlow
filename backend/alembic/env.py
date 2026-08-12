"""Alembic runtime configuration.

Run by the `alembic` CLI (see `make migrate` / `make makemigration`), never
imported by the application.

Two jobs, and they are separate on purpose:

1. Tell Alembic what the schema *should* look like — `Base.metadata`, which is
   populated by importing `app.models`.
2. Tell Alembic how to reach the database — taken from `Settings`, so there is
   exactly one definition of the connection URL in the project. A URL written
   into alembic.ini is a second source of truth that drifts, and a password in
   a committed file besides.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.core.config import get_settings

# Importing the package — not the individual modules — is what puts every
# table into Base.metadata. A model whose module is never imported is invisible
# to autogenerate, which then helpfully writes a migration that DROPs it.
from app.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# `%` is ConfigParser's interpolation character, so a password containing one
# would otherwise fail here with a baffling error.
config.set_main_option("sqlalchemy.url", get_settings().database_url.replace("%", "%%"))

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of running it (`alembic upgrade head --sql`).

    This is how migrations reach a database that CI is not allowed to touch: a
    DBA reviews the generated SQL and applies it during a change window.
    """
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Run migrations on an already-established synchronous connection.

    Alembic's internals are synchronous, so the async engine hands them a sync
    facade via `run_sync`. This function is the boundary between the two worlds.
    """
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # Detect column type changes (VARCHAR(50) -> VARCHAR(100)). Off by
        # default, and its absence is why people believe autogenerate "misses"
        # things.
        compare_type=True,
        # Detect changed server defaults too. Noisier, but a default that
        # silently differs between the model and the database is a bug which
        # only ever shows up in production inserts.
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Open a short-lived async engine and run the migrations through it."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        # NullPool: this process runs one migration and exits. Pooling would
        # keep connections open for a lifetime that is about to end anyway.
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Connect and apply migrations — the normal path."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
