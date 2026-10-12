"""Local private-folder transport: payload, commit manifest, then stream head."""
import json
from pathlib import Path
import shutil
import uuid
from collections import Counter
from .bundle import digest, iter_bundle_rows, read_manifest, safe_path, _read_manifest


class FolderTransport:
    def __init__(self, root, *, max_retained_bytes=8 * 1024 ** 3):
        self.root = Path(root); self.max_retained_bytes = max_retained_bytes

    def _validate(self,bundle,manifest_hash,counts=None):
        for row in iter_bundle_rows(bundle,expected_manifest_hash=manifest_hash):
            if counts is not None:counts[row['table']]+=1

    def _needed_bytes(self,bundle,manifest,manifest_hash):
        pending=safe_path(self.root,'bundles/.partial-'+manifest['bundle_id'])
        expected={f['path'] for f in manifest['files']}|{'manifest.json'}
        if pending.exists():
            if any(path.relative_to(pending).as_posix() not in expected for path in pending.rglob('*') if path.is_file()):
                raise ValueError('unexpected partial bundle file')
            if (pending/'manifest.json').exists() and digest(pending/'manifest.json')!=manifest_hash:
                raise ValueError('partial bundle manifest collision')
        needed=0
        for entry in manifest['files']:
            target=safe_path(pending,entry['path'])
            if not target.exists() or target.stat().st_size!=entry['bytes'] or digest(target)!=entry['sha256']:needed+=entry['bytes']
        if not (pending/'manifest.json').exists():needed+=(Path(bundle)/'manifest.json').stat().st_size
        return needed

    def publish(self, bundle, *, advance_head=True):
        manifest, manifest_hash = _read_manifest(bundle)
        self._validate(bundle,manifest_hash)
        return self._stage_validated(bundle, manifest, manifest_hash, advance_head=advance_head)

    def _stage_validated(self, bundle, manifest, manifest_hash, *, advance_head):
        # Reusing preflight metadata never admits bytes that changed afterward.
        if digest(Path(bundle)/'manifest.json') != manifest_hash:
            raise ValueError('source bundle manifest changed after validation')
        self.root.mkdir(parents=True, exist_ok=True)
        destination = safe_path(self.root, "bundles/" + manifest["bundle_id"])
        if destination.exists():
            self._validate(destination,manifest_hash)
        else:
            retained = sum(p.stat().st_size for p in self.root.rglob("*") if p.is_file())
            incoming = self._needed_bytes(bundle,manifest,manifest_hash)
            if retained + incoming > self.max_retained_bytes: raise ValueError("private retained-storage bound reached; unreceived data preserved")
            pending = safe_path(self.root, "bundles/.partial-" + manifest["bundle_id"]); pending.mkdir(parents=True, exist_ok=True)
            for item in manifest["files"]:
                target = safe_path(pending, item["path"]); target.parent.mkdir(parents=True, exist_ok=True)
                if not target.exists() or target.stat().st_size!=item['bytes'] or digest(target)!=item['sha256']:
                    shutil.copyfile(safe_path(bundle, item["path"]), target)
            shutil.copyfile(Path(bundle) / "manifest.json", pending / "manifest.json")
            self._validate(pending,manifest_hash)
            pending.rename(destination)
        head = {"bundle_id": manifest["bundle_id"], "sequence": manifest["sequence"], "manifest_hash": manifest_hash}
        if not advance_head:return head
        self._advance_head(manifest['stream_id'],head)
        return head

    def _advance_head(self,stream_id,head):
        target = safe_path(self.root, "heads/" + stream_id + ".json"); target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            current = json.loads(target.read_text())
            if current["sequence"] > head["sequence"]: return
            if current["sequence"] == head["sequence"] and current != head: raise ValueError("stream head sequence collision")
        pending_head = target.with_suffix(".pending"); pending_head.write_text(json.dumps(head), encoding="utf-8"); pending_head.replace(target)

    def publish_group(self,bundles,*,row_counts=None):
        """Stage every contiguous chunk before one atomic final head change."""
        paths=[Path(path) for path in bundles]
        if not paths:raise ValueError('nonempty committed chunk group required')
        if row_counts is not None and (type(row_counts) is not dict or row_counts):
            raise ValueError('empty validated group counts output required')
        metadata=[_read_manifest(path) for path in paths]
        manifests=[value[0] for value in metadata];stream=manifests[0]['stream_id']
        current_path=safe_path(self.root,'heads/'+stream+'.json')
        current=json.loads(current_path.read_text()) if current_path.exists() else None
        expected_sequence=current['sequence']+1 if current else 1
        previous=current['bundle_id'] if current else None
        incoming=0;counts=Counter()
        for path,(manifest,manifest_hash) in zip(paths,metadata):
            if manifest['stream_id']!=stream or manifest['sequence']!=expected_sequence or manifest['previous_bundle_id']!=previous:
                raise ValueError('contiguous independent chunk stream required')
            # Complete preflight includes hashes/expansion/privacy before copying.
            self._validate(path,manifest_hash,counts)
            destination=safe_path(self.root,'bundles/'+manifest['bundle_id'])
            if not destination.exists():incoming+=self._needed_bytes(path,manifest,manifest_hash)
            expected_sequence+=1;previous=manifest['bundle_id']
        retained=sum(p.stat().st_size for p in self.root.rglob('*') if p.is_file()) if self.root.exists() else 0
        if retained+incoming>self.max_retained_bytes:raise ValueError('private retained-storage bound reached; previous checkpoint preserved')
        heads=[self._stage_validated(path,manifest,manifest_hash,advance_head=False)
               for path,(manifest,manifest_hash) in zip(paths,metadata)]
        self._advance_head(stream,heads[-1])
        if row_counts is not None:row_counts.update(counts)
        return heads[-1]

    def pull(self, bundle_id, destination_root, *, expected_manifest_hash):
        try: uuid.UUID(bundle_id)
        except ValueError: raise ValueError("invalid bundle identity") from None
        source = safe_path(self.root, "bundles/" + bundle_id)
        manifest = read_manifest(source, expected_manifest_hash=expected_manifest_hash)
        for _ in iter_bundle_rows(source, expected_manifest_hash=expected_manifest_hash):pass
        root = Path(destination_root); root.mkdir(parents=True, exist_ok=True); target = safe_path(root, bundle_id)
        if target.exists():
            read_manifest(target, expected_manifest_hash=expected_manifest_hash)
            for _ in iter_bundle_rows(target):pass
            return target
        pending = safe_path(root, ".partial-" + bundle_id); pending.mkdir()
        for entry in manifest["files"]:
            file = safe_path(pending, entry["path"]); file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(safe_path(source, entry["path"]), file)
        shutil.copyfile(source / "manifest.json", pending / "manifest.json")
        for _ in iter_bundle_rows(pending, expected_manifest_hash=expected_manifest_hash):pass
        pending.rename(target)
        return target

    def committed_bundles(self):
        """Read only head-reachable chunks, ordered by stream and sequence."""
        result=[]
        for head_path in sorted((self.root/'heads').glob('*.json')):
            head=json.loads(head_path.read_text());identity=head['bundle_id'];seen=set();chain=[]
            expected_sequence=head['sequence'];expected_hash=head['manifest_hash']
            while identity is not None:
                if identity in seen:raise ValueError('cyclic committed bundle chain')
                seen.add(identity);path=safe_path(self.root,'bundles/'+identity)
                manifest=read_manifest(path,expected_manifest_hash=expected_hash)
                if manifest['stream_id']!=head_path.stem or manifest['sequence']!=expected_sequence:
                    raise ValueError('noncontiguous committed bundle chain')
                chain.append(path);identity=manifest['previous_bundle_id'];expected_sequence-=1;expected_hash=None
            if expected_sequence!=0:raise ValueError('committed stream bootstrap missing')
            result.extend(reversed(chain))
        return result
