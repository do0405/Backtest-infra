"""Import only committed private-folder chunks, in stream sequence order."""
import argparse
import json
import os
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from backtest_data.db import make_engine,transaction
from backtest_data.remote.bundle import digest
from backtest_data.remote.folder import FolderTransport
from backtest_data.remote.sql import import_bundle


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--private-store',type=Path,required=True)
    parser.add_argument('--raw-root',type=Path,required=True)
    args=parser.parse_args();engine=make_engine(os.environ['BACKTEST_DATABASE_URL'])
    result={'status':'committed_chunks_imported','bundles':0,'inserted_rows':0}
    try:
        for path in FolderTransport(args.private_store).committed_bundles():
            expected=digest(path/'manifest.json')
            with transaction(engine) as session:
                outcome=import_bundle(session,path,raw_root=args.raw_root,expected_manifest_hash=expected)
            result['bundles']+=1;result['inserted_rows']+=outcome['inserted_rows']
        print(json.dumps(result));return 0
    finally:engine.dispose()


if __name__=='__main__':raise SystemExit(main())
