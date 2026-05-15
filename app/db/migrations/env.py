"""Alembic environment.

Uses sync psycopg even though the app uses asyncpg. asyncpg routes everything
through prepared statements which can't execute multi-statement SQL like our
initial schema.sql snapshot. psycopg has no such limitation.
"""

import os
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool
from sqlalchemy.engine.url import make_url

from alembic import context
from app.db import models  # noqa: F401 -- registers ORM on Base.metadata
from app.db.base import Base
from app.settings.config import get_settings

config = context.config

_url = make_url(os.environ.get("DATABASE_URL") or get_settings().database_url)
_sync_url = _url.set(drivername="postgresql+psycopg")
config.set_main_option("sqlalchemy.url", _sync_url.render_as_string(hide_password=False))

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
