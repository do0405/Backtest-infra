from dataclasses import dataclass, field
import os
from pathlib import Path


def load_environment(path: Path = Path(".env")) -> None:
    """Load local configuration without overriding explicitly supplied environment."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip().isidentifier():
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


@dataclass(frozen=True)
class Settings:
    database_url: str = field(repr=False)
    raw_root: Path

    @classmethod
    def from_env(cls) -> "Settings":
        load_environment()
        url = os.environ.get("BACKTEST_DATABASE_URL", "")
        if not url.startswith("postgresql"):
            raise ValueError("BACKTEST_DATABASE_URL must configure PostgreSQL")
        return cls(url, Path(os.environ.get("BACKTEST_RAW_ROOT", "data/raw")))
