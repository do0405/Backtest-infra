"""A fresh hosted runtime must install every base project requirement."""
from pathlib import Path
import re
import tomllib


def test_hosted_runtime_includes_exact_project_dependencies():
    root = Path(__file__).resolve().parents[2]
    project = tomllib.loads((root / 'pyproject.toml').read_text(encoding='utf-8'))
    def normalized(requirement):
        return re.sub(r'\[[^\]]+\]', '', requirement).lower()
    installed = {
        normalized(line.strip())
        for line in (root / 'scripts/remote-requirements.txt').read_text(encoding='utf-8').splitlines()
        if line.strip() and not line.startswith('#')
    }
    assert {normalized(value) for value in project['project']['dependencies']} <= installed
