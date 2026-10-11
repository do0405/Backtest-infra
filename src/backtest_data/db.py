from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import create_engine, MetaData
from sqlalchemy.orm import DeclarativeBase, Session


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return str(uuid4())


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention={
        "ix": "ix_%(table_name)s_%(column_0_name)s",
        "uq": "uq_%(table_name)s_%(column_0_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    })


def make_engine(url: str):
    if not url.startswith("postgresql"):
        raise ValueError("This dataset requires PostgreSQL")
    return create_engine(url, pool_pre_ping=True, hide_parameters=True)


@contextmanager
def transaction(engine):
    with Session(engine) as session, session.begin():
        yield session


def load_models() -> None:
    from .catalog import models as catalog_models
    from .ingestion import models as ingestion_models
    from .ingestion import quota_models as quota_models
    from .fundamentals import models as fundamental_models
    from .fundamentals import publication_models as publication_models
    from .fundamentals import daily_publication_models as daily_publication_models
    from .prices import models as price_models
    from .prices import supplier_models as supplier_models
    from .prices import supplier_refresh_models as supplier_refresh_models
    from .prices import vwap_input_models as vwap_input_models
    from .prices import krx_quote_models as krx_quote_models
    from .supplemental import models as supplemental_models
    from .releases import models as release_models
    from .coverage import models as coverage_models
    from .coverage import eligibility_models as eligibility_models
    from .remote import models as remote_models
    from .indices import models as index_models
