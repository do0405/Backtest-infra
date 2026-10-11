"""Content-addressed immutable JSON results and input-bound replay."""
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import tempfile
from backtest_research.contracts import canonical_hash, canonical_value


def seal_result(payload):
    body = canonical_value(deepcopy(payload))
    body.pop('result_id', None)
    body['result_id'] = canonical_hash(body)
    return body


def verify_result(result):
    if not isinstance(result, dict) or result.get('result_id') != seal_result(result)['result_id']:
        raise ValueError('result content hash mismatch')
    return True


class ImmutableResultStore:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def _path(self, result_id):
        if not isinstance(result_id, str) or not re.fullmatch('[0-9a-f]{64}', result_id):
            raise ValueError('invalid result identifier')
        return self.directory / (result_id + '.json')

    def put(self, result):
        verify_result(result)
        path = self._path(result['result_id'])
        body = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
        # Windows rename refuses an existing target; POSIX hard-link creation
        # has the same no-replacement guarantee. Publish only the complete file.
        descriptor, temporary_name = tempfile.mkstemp(prefix='.result-', dir=self.directory)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                if os.name == 'nt':
                    os.rename(temporary, path)
                else:
                    os.link(temporary, path)
            except FileExistsError:
                if self.get(result['result_id']) != result:
                    raise ValueError('immutable result collision')
        finally:
            temporary.unlink(missing_ok=True)
        return result['result_id']

    def get(self, result_id):
        result = json.loads(self._path(result_id).read_text(encoding='utf-8'))
        verify_result(result)
        if result['result_id'] != result_id:
            raise ValueError('result identifier hash mismatch')
        return result


def replay_result(result, dataset):
    verify_result(result)
    if result['dataset_id'] != dataset.dataset_id or result['fingerprint']['input_hash'] != dataset.content_hash:
        raise ValueError('replay input hash mismatch')
    from backtest_research.engines import run_backtest
    replayed = run_backtest(dataset, **result['run_config'])
    if replayed['result_id'] != result['result_id']:
        raise ValueError('replay engine/code/result fingerprint mismatch')
    return replayed
