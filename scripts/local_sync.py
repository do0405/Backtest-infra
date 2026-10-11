"""Local private-folder or configured private GitHub artifact -> existing SQL."""
import argparse
import json
import re
from pathlib import Path
import subprocess
import shutil
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from backtest_data.config import Settings
from backtest_data.db import make_engine
from backtest_data.remote.local_sync import pull_and_import

def gh_binary():
    existing=shutil.which('gh')
    portable=Path(__file__).resolve().parents[1]/'artifacts/github-setup/cli/bin/gh.exe'
    if existing:return existing
    if portable.is_file():return str(portable)
    raise ValueError('GitHub CLI login is required')

def require_artifact_visibility(metadata,allow_public_results):
    if metadata.get('private') is not True and not allow_public_results:
        raise ValueError('Public result download requires explicit authorization')

def gh_json(args):
    result=subprocess.run([gh_binary(),*args],check=True,capture_output=True,text=True,timeout=60)
    return json.loads(result.stdout)

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--private-store',type=Path,required=True)
    parser.add_argument('--incoming',type=Path,required=True);parser.add_argument('--repository')
    parser.add_argument('--max-private-bytes',type=int,default=8*1024**3)
    parser.add_argument('--allow-public-results',action='store_true')
    args=parser.parse_args()
    if args.repository:
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',args.repository):raise ValueError('Exact GitHub owner/repository required')
        metadata=gh_json(['api','repos/'+args.repository])
        require_artifact_visibility(metadata,args.allow_public_results)
        inventory=gh_json(['api','repos/'+args.repository+'/actions/artifacts?per_page=100'])
        states=[r for r in inventory['artifacts'] if r['name']=='remote-private-state' and not r['expired']]
        if not states:raise ValueError('No committed private remote state artifact')
        state=max(states,key=lambda r:r['created_at'])
        if state['size_in_bytes']>args.max_private_bytes:raise ValueError('Private download bound exceeded')
        archive=args.incoming/'private-remote-state.zip';archive.parent.mkdir(parents=True,exist_ok=True)
        with archive.open('wb') as target:
            subprocess.run([gh_binary(),'api',f"repos/{args.repository}/actions/artifacts/{state['id']}/zip"],check=True,
                stdout=target,stderr=subprocess.PIPE,timeout=300)
        if archive.stat().st_size>args.max_private_bytes:raise ValueError('Downloaded artifact exceeds declared bound')
        if args.private_store.exists():raise ValueError('Use a fresh private-store directory for each artifact download')
        subprocess.run([sys.executable,str(Path(__file__).with_name('remote_restore.py')),str(archive),str(args.private_store),
            '--max-bytes',str(args.max_private_bytes)],check=True,timeout=300,capture_output=True)
    settings=Settings.from_env();engine=make_engine(settings.database_url)
    try:print(json.dumps(pull_and_import(engine,args.private_store,args.incoming,settings.raw_root,max_retained_bytes=args.max_private_bytes)))
    finally:engine.dispose()
if __name__=='__main__':main()
