"""Pull committed private bundles in stream order, then atomically import SQL."""
from pathlib import Path
import json
from .bundle import read_manifest
from .folder import FolderTransport
from .sql import import_bundle
from ..db import transaction,utcnow

def pull_and_import(engine,private_store,incoming_root,raw_root,*,max_retained_bytes=8*1024**3):
    """No authentication/config/operational table is transported or modified."""
    source=FolderTransport(private_store,max_retained_bytes=max_retained_bytes)
    heads=[]
    for path in (Path(private_store)/'heads').glob('*.json'):
        value=json.loads(path.read_text(encoding='utf-8'));value['stream_id']=path.stem;heads.append(value)
    manifests=[(read_manifest(path),path) for path in source.committed_bundles()]
    results=[]
    for manifest,path in sorted(manifests,key=lambda pair:(pair[0]['stream_id'],pair[0]['sequence'])):
        from .bundle import digest
        expected=digest(path/'manifest.json')
        received=source.pull(manifest['bundle_id'],incoming_root,expected_manifest_hash=expected)
        with transaction(engine) as session:
            result=import_bundle(session,received,raw_root=raw_root,expected_manifest_hash=expected)
        results.append(result)
    ack={'checked_at':utcnow().isoformat(),'streams':heads,'import_results':results,'coverage_complete':False}
    destination=Path(incoming_root);destination.mkdir(parents=True,exist_ok=True)
    pending=destination/'local-import-ack.pending.json';pending.write_text(json.dumps(ack,indent=2,default=str),encoding='utf-8')
    pending.replace(destination/'local-import-ack.json')
    return {'status':'local_sql_synced','bundles':len(results),'inserted_rows':sum(r.get('inserted_rows',0) for r in results),
        'ack_path':str(destination/'local-import-ack.json'),'coverage_complete':False}
