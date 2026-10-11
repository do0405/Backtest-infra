"""Restore only the bounded private artifact tree, never a SQL dump/config."""
import argparse
from pathlib import Path
import stat
import sys
import zipfile
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from backtest_data.remote.bundle import safe_path, MAX_BUNDLE_BYTES


def restore(archive, root, max_bytes):
    total = 0
    with zipfile.ZipFile(archive) as z:
        if len(z.infolist()) > 200_000: raise ValueError("private state member bound exceeded")
        for member in z.infolist():
            if member.is_dir(): continue
            name = member.orig_filename
            if not name.startswith(("bundles/", "heads/")) or stat.S_ISLNK(member.external_attr >> 16):
                raise ValueError("only private data bundles and heads may be restored")
            target = safe_path(root, name)
            total += member.file_size
            if member.file_size > MAX_BUNDLE_BYTES or total > max_bytes or member.file_size > 500 * max(1,member.compress_size):
                raise ValueError("private state expansion bound exceeded")
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(member) as source, target.open("xb") as output:
                count = 0
                while block := source.read(1024 * 1024):
                    count += len(block)
                    if count > member.file_size: raise ValueError("private state file bound exceeded")
                    output.write(block)


if __name__ == "__main__":
    p=argparse.ArgumentParser();p.add_argument("archive",type=Path);p.add_argument("root",type=Path);p.add_argument("--max-bytes",type=int,required=True)
    a=p.parse_args();restore(a.archive,a.root,a.max_bytes)
