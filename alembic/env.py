from alembic import context

from backtest_data.config import Settings
from backtest_data.db import Base, load_models, make_engine

load_models()
target_metadata = Base.metadata


def configure(connection=None):
    if connection is None:
        context.configure(url=Settings.from_env().database_url, target_metadata=target_metadata, literal_binds=True)
    else:
        context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    configure()
elif context.config.attributes.get("connection") is not None:
    configure(context.config.attributes["connection"])
else:
    engine = make_engine(Settings.from_env().database_url)
    with engine.connect() as conn:
        configure(conn)
    engine.dispose()
