"""Alembic environment, wired to the app's settings and models.

The DB url and metadata come straight from the application (app.config /
app.models) rather than alembic.ini, so there is a single source of truth and
no credentials live in the ini file.
"""
import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import settings
from app.db import Base
from app.models import Channel, SeenItem, Setting  # noqa: F401  (register tables)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# Detect column type and server-default changes during autogenerate too.
_CONFIGURE = {
    "target_metadata": target_metadata,
    "compare_type": True,
    "compare_server_default": True,
    "render_as_batch": True,  # ALTER COLUMN support on SQLite (no-op on Postgres)
}


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a live DB connection."""
    context.configure(
        url=settings.database_url,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        **_CONFIGURE,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, **_CONFIGURE)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = create_async_engine(settings.database_url, poolclass=pool.NullPool)
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
