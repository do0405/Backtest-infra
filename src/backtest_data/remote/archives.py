"""Bounded source bundle extraction with path and expansion checks."""
from pathlib import Path
import stat
import zipfile
from .bundle import safe_path,MAX_BUNDLE_BYTES


def restore(archive,root,max_bytes):
    total=0
    with zipfile.ZipFile(archive) as z:
        if len(z.infolist())>200_000:raise ValueError('source state member bound exceeded')
        for member in z.infolist():
            if member.is_dir():continue
            name=member.orig_filename
            if not name.startswith(('bundles/','heads/')) or stat.S_ISLNK(member.external_attr>>16):
                raise ValueError('only source data bundles and heads may be restored')
            target=safe_path(root,name);total+=member.file_size
            if member.file_size>MAX_BUNDLE_BYTES or total>max_bytes or member.file_size>500*max(1,member.compress_size):
                raise ValueError('source state expansion bound exceeded')
            target.parent.mkdir(parents=True,exist_ok=True)
            with z.open(member) as source,target.open('xb') as output:
                count=0
                while block:=source.read(1024**2):
                    count+=len(block)
                    if count>member.file_size:raise ValueError('source state file bound exceeded')
                    output.write(block)
