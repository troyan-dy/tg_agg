import logging

from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

log = logging.getLogger("db")

engine = create_async_engine(settings.database_url, echo=False, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def _migrate_seen_items_channel_id(conn: Connection) -> None:
    """Upgrade a pre-multichannel `seen_items` to the per-channel schema.

    `create_all` never alters an existing table, so a DB created before channels
    existed keeps the old single-column `seen_items` (PK on entry_id, no
    channel_id) and every dedup query then fails. This brings it in line with the
    model: add channel_id, attribute existing rows to the sole/first channel
    (preserving dedup history so old stories aren't reposted), switch to the
    composite primary key and add the FK. Idempotent — a no-op once migrated and
    on a freshly created DB (where create_all already made the new schema).
    PostgreSQL-only (guarded by the caller); prod runs on Postgres.
    """
    has_col = conn.execute(
        text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'seen_items' AND column_name = 'channel_id'"
        )
    ).first()
    if has_col:
        return

    conn.execute(text("ALTER TABLE seen_items ADD COLUMN channel_id integer"))
    first_channel = conn.execute(text("SELECT min(id) FROM channels")).scalar()
    if first_channel is None:
        # No channel to attribute the old rows to — they are useless now, drop them.
        conn.execute(text("DELETE FROM seen_items"))
    else:
        conn.execute(
            text("UPDATE seen_items SET channel_id = :cid"), {"cid": first_channel}
        )
    conn.execute(text("ALTER TABLE seen_items ALTER COLUMN channel_id SET NOT NULL"))
    conn.execute(text("ALTER TABLE seen_items DROP CONSTRAINT IF EXISTS seen_items_pkey"))
    conn.execute(text("ALTER TABLE seen_items ADD PRIMARY KEY (channel_id, entry_id)"))
    conn.execute(
        text(
            "ALTER TABLE seen_items ADD CONSTRAINT seen_items_channel_id_fkey "
            "FOREIGN KEY (channel_id) REFERENCES channels (id) ON DELETE CASCADE"
        )
    )
    log.info(
        "Migrated seen_items to per-channel schema (channel_id backfilled to %s)",
        first_channel,
    )


async def init_db() -> None:
    """Create tables if they do not exist and run lightweight in-place migrations.

    No Alembic: the schema is brought up to date here so a deploy is
    self-contained. create_all makes any missing tables; the migration then
    upgrades a legacy `seen_items` left over from the single-channel era.
    """
    import app.models  # noqa: F401 - ensure models are registered

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        if engine.dialect.name == "postgresql":
            await conn.run_sync(_migrate_seen_items_channel_id)
