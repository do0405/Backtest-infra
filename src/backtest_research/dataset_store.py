"""Immutable exact research inputs, with bounded names and integrity checks."""
from dataclasses import asdict
from datetime import date,datetime
from decimal import Decimal
import json
import os
from pathlib import Path
import re
import tempfile
from .contracts import AnalysisDataset,Bar,SeriesIdentity


def wire(value):
    if isinstance(value,Decimal): return str(value)
    if isinstance(value,(date,datetime)): return value.isoformat()
    if hasattr(value,'__dataclass_fields__'): return wire(asdict(value))
    if isinstance(value,dict): return {k:wire(v) for k,v in value.items()}
    if isinstance(value,(tuple,list)): return [wire(v) for v in value]
    return value


def decode(body):
    bars=[]
    for row in body['bars']:
        row=dict(row)
        identity=dict(row.pop('series'));identity['adjustments']=tuple(identity['adjustments'])
        row['series']=SeriesIdentity(**identity)
        row['session']=date.fromisoformat(row['session'])
        for name in ('open','high','low','close','volume','notional'):
            row[name]=Decimal(row[name]) if row.get(name) is not None else None
        for name in ('captured_at','available_at'): row[name]=datetime.fromisoformat(row[name])
        bars.append(Bar(**row))
    result=AnalysisDataset.create(bars=bars,sessions=[date.fromisoformat(d) for d in body['sessions']],
        cutoff=datetime.fromisoformat(body['cutoff']),source_snapshot_at=datetime.fromisoformat(body['source_snapshot_at']),
        manifest_id=body['manifest_id'],manifest_hash=body['manifest_hash'],original_target_count=body['original_target_count'],
        quality_gaps=body['quality_gaps'],origin=body['origin'],calendar_grade=body['calendar_grade'],
        policy_version=body['policy_version'],release_id=body['release_id'],calendar_evidence_refs=body.get('calendar_evidence_refs',()))
    if result.content_hash!=body['content_hash'] or result.dataset_id!=body['dataset_id']:
        raise ValueError('dataset integrity mismatch')
    return result


class DatasetStore:
    def __init__(self,directory):
        self.directory=Path(directory);self.directory.mkdir(parents=True,exist_ok=True)
    def _path(self,identifier):
        if not isinstance(identifier,str) or not re.fullmatch('[0-9a-f]{64}',identifier):
            raise ValueError('invalid dataset identifier')
        return self.directory/(identifier+'.json')
    def put(self,dataset):
        path=self._path(dataset.dataset_id)
        descriptor,name=tempfile.mkstemp(prefix='.dataset-',dir=self.directory)
        temporary=Path(name)
        try:
            with os.fdopen(descriptor,'w',encoding='utf-8') as stream:
                json.dump(wire(dataset),stream,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
                stream.flush();os.fsync(stream.fileno())
            try:
                if os.name=='nt':os.rename(temporary,path)
                else:os.link(temporary,path)
            except FileExistsError:
                if self.get(dataset.dataset_id)!=dataset:raise ValueError('immutable input collision')
        finally:temporary.unlink(missing_ok=True)
        return dataset.dataset_id
    def get(self,identifier):
        path=self._path(identifier)
        if path.stat().st_size>64*1024*1024:raise ValueError('input size exceeds bound')
        result=decode(json.loads(path.read_text(encoding='utf-8')))
        if result.dataset_id!=identifier:raise ValueError('dataset name mismatch')
        return result
