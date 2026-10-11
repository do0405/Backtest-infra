from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from ..db import new_id
from ..ingestion.jobs import stable_hash
from .models import SourceContract


def ensure_contract(session, *, source, domain, market, specification):
    digest = stable_hash(specification)
    version = "v1:" + digest[:16]
    session.execute(insert(SourceContract).values(id=new_id(), source=source, domain=domain, market=market,
        version=version, specification=specification, content_hash=digest, qualification="contract_defined")
        .on_conflict_do_nothing(index_elements=[SourceContract.source, SourceContract.version, SourceContract.domain, SourceContract.market]))
    return session.scalar(select(SourceContract).where(SourceContract.source == source, SourceContract.domain == domain,
                                                       SourceContract.market == market, SourceContract.version == version))
