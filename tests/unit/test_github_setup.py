import importlib.util
from pathlib import Path
from types import SimpleNamespace
import pytest

spec = importlib.util.spec_from_file_location('github_setup', Path(__file__).parents[2] / 'scripts/github_setup.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_secret_uses_stdin_not_arguments_or_result():
    calls = []
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout='', stderr='')
    result = module.set_secret('gh', 'owner/repo', 'API_KEY', 'synthetic-private-value', run=run)
    assert 'synthetic-private-value' not in str(calls[0][0])
    assert calls[0][1]['input'] == 'synthetic-private-value'
    assert 'synthetic-private-value' not in str(result)
    assert result['registered']


def test_error_does_not_echo_process_diagnostics_or_secret():
    result = module.set_secret('gh', 'owner/repo', 'API_KEY', 'synthetic-private-value',
        run=lambda *a, **k: SimpleNamespace(returncode=1, stderr='synthetic-private-value', stdout=''))
    assert not result['registered']
    assert 'synthetic-private-value' not in str(result)


def test_invalid_name_never_starts_process():
    with pytest.raises(ValueError):
        module.set_secret('gh', 'owner/repo', 'BAD NAME', 'synthetic-private-value', run=lambda *a, **k: pytest.fail())
