"""Source FK closure export and atomic, idempotent local PostgreSQL import."""
from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path
import shutil
from sqlalchemy import select, text, cast, Text, JSON
from sqlalchemy.dialects.postgresql import insert
from .bundle import digest, iter_bundle_rows, read_manifest, safe_path, write_bundle, MAX_EXPANDED_BYTES
from .codec import dumps, pack, PRIVATE_KEY, PaginationCursor
from .models import RemoteImportReceipt, RemoteStreamCursor
from .registry import tables, validate_row, wire_row, database_values, logical_parents


def _pk(table, values):
    columns = list(table.primary_key.columns)
    if len(columns) != 1: raise ValueError("only registered single-column source IDs supported")
    return values[columns[0].name]


def _original_row(session,table,condition):
    """Decode JSONB numeric text exactly instead of driver Python floats."""
    from decimal import Decimal
    columns=[cast(column,Text).label(column.name) if isinstance(column.type,JSON) else column for column in table.c]
    row=session.execute(select(*columns).where(condition)).mappings().one_or_none()
    if row is None:return None
    result=dict(row)
    for column in table.c:
        if isinstance(column.type,JSON) and result[column.name] is not None:
            result[column.name]=json.loads(result[column.name],parse_float=Decimal)
    return result


def _original_rows(session, table, column, identities, *, batch_size=1000):
    """Bound FK reads without one round trip per catalog member."""
    from decimal import Decimal
    identities=list(dict.fromkeys(identities))
    columns=[cast(c,Text).label(c.name) if isinstance(c.type,JSON) else c for c in table.c]
    result=[]
    for offset in range(0,len(identities),batch_size):
        for row in session.execute(select(*columns).where(column.in_(identities[offset:offset+batch_size]))).mappings():
            value=dict(row)
            for c in table.c:
                if isinstance(c.type,JSON) and value[c.name] is not None:
                    value[c.name]=json.loads(value[c.name],parse_float=Decimal)
            result.append(value)
    return result


def export_delta(session, roots, output_root, *, stream_id, sequence, scope, checkpoint=None,
                 previous_bundle_id=None, raw_root=None, max_rows=100_000, already_exported=None):
    """Export explicit original row IDs and all their recursively referenced parents."""
    registry = tables(); pending = []; found = {};known=set(already_exported or ())
    for name, ids in roots.items():
        if name not in registry: raise ValueError("table outside source allowlist")
        table = registry[name]
        requested=set(identity for identity in ids if (name,identity) not in known)
        rows=_original_rows(session,table,list(table.primary_key.columns)[0],requested)
        if {_pk(table,value) for value in rows}!=requested:raise ValueError("export root ID missing")
        pending.extend((name,value) for value in rows)
    while pending:
        references={};wave=pending;pending=[]
        for name,values in wave:
            table=registry[name];identity=_pk(table,values)
            if (name,identity) in found or (name,identity) in known:continue
            if len(found)>=max_rows:raise ValueError("export row bound exceeded")
            validate_row(wire_row({"table":name,"values":values}));found[(name,identity)]=values
        for name,values in wave:
            table=registry[name]
            for fk in table.foreign_keys:
                target=values[fk.parent.name];parent_name=fk.column.table.name
                if target is None:continue
                if parent_name not in registry:raise ValueError("parent outside source allowlist")
                if fk.column.primary_key and ((parent_name,target) in known or (parent_name,target) in found):continue
                references.setdefault((parent_name,fk.column.name),set()).add(target)
            for parent_name,target in logical_parents({'table':name,'values':values}):
                if (parent_name,target) not in known and (parent_name,target) not in found:
                    references.setdefault((parent_name,list(registry[parent_name].primary_key.columns)[0].name),set()).add(target)
        for (name,column_name),requested in references.items():
            table=registry[name];rows=_original_rows(session,table,table.c[column_name],requested)
            if {value[column_name] for value in rows}!=requested:raise ValueError("source parent missing")
            pending.extend((name,value) for value in rows)
    raw_files = {}
    for (name, identity), values in found.items():
        if name == "raw_object":
            if raw_root is None: raise ValueError("raw source evidence root required")
            storage_key = values["storage_key"]; source = safe_path(raw_root, storage_key)
            _verify_raw(source, values)
            raw_files[storage_key] = source
    rows = (wire_row({"table": name, "values": values}) for (name, _), values in sorted(found.items()))
    return write_bundle(output_root, rows, stream_id=stream_id, sequence=sequence, scope=scope,
                        checkpoint=checkpoint, previous_bundle_id=previous_bundle_id, raw_files=raw_files, max_rows=max_rows)


def export_deltas(session,roots,output_root,*,stream_id,sequence,scope,checkpoint=None,
                  previous_bundle_id=None,raw_root=None,max_rows=100_000,max_roots_per_bundle=5000):
    """Bounded chunks with closure across the contiguous group, final state only.

    Already emitted parents/raw files are reused by later chunks in this group.
    Consumers therefore import stream sequence order. The transport publishes
    every chunk before advancing its sole checkpoint-bearing final head.
    """
    if type(max_roots_per_bundle) is not int or not 1<=max_roots_per_bundle<=max_rows:
        raise ValueError('bounded root chunk size required')
    flattened=((name,identity) for name,identities in roots.items() for identity in identities)
    from itertools import islice
    current=list(islice(flattened,max_roots_per_bundle));paths=[];known=set();registry=tables()
    def emit(chunk,final):
        nonlocal sequence,previous_bundle_id
        batch={}
        for name,identity in chunk:batch.setdefault(name,[]).append(identity)
        final_checkpoint=dict(checkpoint or {}) if final else None
        if final_checkpoint and 'data_export_cursor' in final_checkpoint:
            final_checkpoint['data_export_cursor']={'stream_id':stream_id,'sequence':sequence,'previous_bundle_id':previous_bundle_id}
        try:
            path=export_delta(session,batch,output_root,stream_id=stream_id,sequence=sequence,scope=scope,
                checkpoint=final_checkpoint,previous_bundle_id=previous_bundle_id,raw_root=raw_root,
                max_rows=max_rows,already_exported=known)
        except ValueError as exc:
            if str(exc)!='export row bound exceeded' or len(chunk)<=1:raise
            middle=len(chunk)//2;emit(chunk[:middle],False);emit(chunk[middle:],final);return
        for row in iter_bundle_rows(path):known.add((row['table'],_pk(registry[row['table']],row['values'])))
        paths.append(path);previous_bundle_id=read_manifest(path)['bundle_id'];sequence+=1
    if not current:emit([],True)
    while current:
        following=list(islice(flattened,max_roots_per_bundle));emit(current,not following);current=following
    return paths


def _verify_raw(path, raw):
    check = hashlib.sha256(); size = 0; sample = bytearray()
    with gzip.open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            size += len(block)
            if size > MAX_EXPANDED_BYTES or size > raw["byte_count"]: raise ValueError("raw evidence expansion bound exceeded")
            check.update(block)
            if len(sample) < 32 * 1024 * 1024: sample.extend(block)
    if size != raw["byte_count"] or check.hexdigest() != raw["content_hash"]: raise ValueError("raw evidence hash mismatch")
    # JSON source evidence with authentication/pagination state is deliberately
    # blocked; its original bytes are never rewritten to manufacture provenance.
    if sample.lstrip().startswith((b"{", b"[")):
        if size > 32 * 1024 * 1024: raise ValueError("raw JSON privacy validation size bound exceeded")
        try:
            from decimal import Decimal
            value = json.loads(sample, parse_float=Decimal)
        except (UnicodeDecodeError, json.JSONDecodeError): raise ValueError("raw JSON privacy validation failed") from None
        if isinstance(value,dict) and any(k in value for k in ('bars','trades','corporate_actions')) and 'next_page_token' in value:
            value=dict(value)
            cursor=value.pop('next_page_token')
            if cursor is not None:value['next_page_token']=PaginationCursor(cursor)
        pack(value)


def _install_raw(bundle, manifest, records, raw_root):
    included = {r["values"]["storage_key"]: r["values"] for r in records if r["table"] == "raw_object"}
    entries = {f["storage_key"]: f for f in manifest["files"] if f["kind"] == "raw"}
    if set(included) != set(entries): raise ValueError("raw parent evidence closure missing or extra")
    if included and raw_root is None: raise ValueError("local raw evidence root required")
    for key, raw in included.items():
        source = safe_path(bundle, entries[key]["path"]); _verify_raw(source, raw)
        destination = safe_path(raw_root, key); destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            _verify_raw(destination, raw)
            continue
        pending = destination.with_suffix(".remote.partial")
        shutil.copyfile(source, pending); pending.replace(destination)


def import_bundle(session, bundle, *, imported_at=None, expected_manifest_hash=None, raw_root=None):
    """All SQL facts, receipt and cursor use one savepoint in caller transaction.

    IDs/clocks are preserved. Natural-key collisions with different IDs and any
    changed existing row fail instead of updating immutable local history.
    Hash-addressed raw files may remain as harmless orphans after SQL rollback.
    """
    imported_at = imported_at or datetime.now(timezone.utc)
    if imported_at.tzinfo is None: raise ValueError("aware local import clock required")
    manifest = read_manifest(bundle, expected_manifest_hash=expected_manifest_hash)
    manifest_hash = digest(Path(bundle) / "manifest.json")
    records = list(iter_bundle_rows(bundle, expected_manifest_hash=manifest_hash))
    registry = tables()
    with session.begin_nested():
        session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:stream))"), {"stream": "remote-import:" + manifest["stream_id"]})
        receipt = session.execute(select(RemoteImportReceipt.bundle_id, RemoteImportReceipt.manifest_hash)
            .where(RemoteImportReceipt.bundle_id == manifest["bundle_id"])).one_or_none()
        if receipt is not None:
            if receipt.manifest_hash != manifest_hash: raise ValueError("replayed bundle manifest hash changed")
            return {"bundle_id": receipt.bundle_id, "inserted_rows": 0, "replayed": True}
        collision = session.scalar(select(RemoteImportReceipt.bundle_id).where(RemoteImportReceipt.stream_id == manifest["stream_id"], RemoteImportReceipt.sequence == manifest["sequence"]))
        if collision is not None: raise ValueError("independent stream sequence collision")
        _install_raw(bundle, manifest, records, raw_root)
        pending = {}; inserted = 0
        for row in records:
            table = validate_row(row); key = (row["table"], _pk(table, row["values"]))
            if key in pending and dumps(pending[key]) != dumps(row): raise ValueError("duplicate source ID with different content")
            pending[key] = row
        # Verify existing originals and parent availability in bounded groups.
        # A common catalog/raw parent is read once, rather than for every quote.
        existing={};references={};dependencies={}
        for name in {key[0] for key in pending}:
            table=registry[name];primary=list(table.primary_key.columns)[0]
            for value in _original_rows(session,table,primary,[key[1] for key in pending if key[0]==name],batch_size=100):
                existing[(name,value[primary.name])]=value
        for key,row in pending.items():
            table=registry[row['table']];deps=set()
            for fk in table.foreign_keys:
                target=row['values'][fk.parent.name]
                if target is None:continue
                name,column=fk.column.table.name,fk.column.name
                if name not in registry:raise ValueError('parent outside source allowlist')
                deps.add((name,column,target))
            for name,identity in logical_parents(row):
                deps.add((name,list(registry[name].primary_key.columns)[0].name,identity))
            dependencies[key]=deps
            for name,column,target in deps:references.setdefault((name,column),set()).add(target)
            if key in existing and dumps(wire_row({'table':key[0],'values':existing[key]})['values'])!=dumps(row['values']):
                raise ValueError('immutable original row collision')
        available=set()
        for (name,column),values in references.items():
            target=registry[name].c[column];values=list(values)
            for offset in range(0,len(values),100):
                available.update((name,column,value) for value in session.scalars(select(target).where(target.in_(values[offset:offset+100]))))
        while pending:
            ready=[key for key in pending if dependencies[key]<=available]
            if not ready: raise ValueError("source parent missing or cyclic closure")
            by_table={}
            for key in ready:
                if key not in existing:by_table.setdefault(key[0],[]).append(pending[key])
            for name,rows in by_table.items():
                table=registry[name];primary=list(table.primary_key.columns)[0]
                # External-disk WAL/index waits must fit the per-statement
                # deadline. Keep the bundle transaction atomic while limiting
                # each physical INSERT to ten original rows.
                for offset in range(0,len(rows),10):
                    batch=rows[offset:offset+10]
                    identities=list(session.scalars(insert(table).values([database_values(row) for row in batch]).on_conflict_do_nothing().returning(primary)))
                    if len(identities)!=len(batch):raise ValueError('source stable ID conflicts with existing natural key')
                    inserted+=len(identities)
            for key in ready:
                row=pending.pop(key)
                for (name,column) in references:
                    if name==key[0]:available.add((name,column,row['values'][column]))
        session.add(RemoteImportReceipt(bundle_id=manifest["bundle_id"], stream_id=manifest["stream_id"], sequence=manifest["sequence"],
            manifest_hash=manifest_hash, imported_at=imported_at, inserted_rows=inserted,
            scope=manifest["scope"], checkpoint=manifest["checkpoint"]))
        session.flush()
        cursor = session.get(RemoteStreamCursor, manifest["stream_id"])
        if cursor is None:
            cursor = RemoteStreamCursor(stream_id=manifest["stream_id"], committed_sequence=0, updated_at=imported_at); session.add(cursor)
        sequences = set(session.scalars(select(RemoteImportReceipt.sequence).where(RemoteImportReceipt.stream_id == manifest["stream_id"])))
        while cursor.committed_sequence + 1 in sequences: cursor.committed_sequence += 1
        cursor.updated_at = imported_at; session.flush()
    return {"bundle_id": manifest["bundle_id"], "inserted_rows": inserted, "replayed": False}
