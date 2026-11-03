"""Alembic environment. Uses the connection handed over by tgmt5.persistence.db.Database."""
from alembic import context
from sqlalchemy import engine_from_config, pool

from tgmt5.persistence.models import Base

config = context.config
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(url=config.get_main_option("sqlalchemy.url"), target_metadata=target_metadata,
                      literal_binds=True, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is None:
        connectable = engine_from_config(config.get_section(config.config_ini_section) or
                                         {"sqlalchemy.url": config.get_main_option("sqlalchemy.url")},
                                         prefix="sqlalchemy.", poolclass=pool.NullPool)
        with connectable.connect() as connection:
            context.configure(connection=connection, target_metadata=target_metadata,
                              render_as_batch=True)
            with context.begin_transaction():
                context.run_migrations()
    else:
        context.configure(connection=connection, target_metadata=target_metadata,
                          render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
