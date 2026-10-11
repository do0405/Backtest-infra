"""Read existing local provider configuration; never persist credential values."""
from collections.abc import Iterable
from dataclasses import dataclass, field
import os
from pathlib import Path

import yaml


@dataclass(frozen=True, repr=False)
class Secret:
    _value: str = field(repr=False)

    def reveal(self) -> str:
        return self._value

    def __bool__(self):
        return bool(self._value)

    def __repr__(self):
        return "<redacted>"

    __str__ = __repr__


@dataclass(frozen=True)
class Credentials:
    kis_app_key: Secret
    kis_app_secret: Secret
    opendart_api_key: Secret
    alpaca_key: Secret
    alpaca_secret: Secret
    sec_user_agent: str = field(repr=False)
    krx_api_key: Secret = field(default_factory=lambda: Secret(""))
    alpha_vantage_api_key: Secret = field(default_factory=lambda: Secret(""))


def _read_config(path: Path) -> dict:
    try:
        if not path.is_file():
            return {}
        size = path.stat().st_size
    except OSError:
        raise ValueError("Provider configuration could not be read") from None
    if size > 1024 * 1024:
        raise ValueError("Provider configuration exceeds size limit")
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, yaml.YAMLError, RecursionError):
        # YAML parser diagnostics can include the original credential text.
        raise ValueError("Provider configuration could not be read") from None
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise ValueError("Provider configuration must be a mapping")
    return payload


def _get(names: tuple[str, ...], config: dict, aliases: tuple[str, ...]) -> Secret:
    for name in names:
        if value := os.environ.get(name, "").strip():
            return Secret(value)
    for name in aliases:
        if value := str(config.get(name, "") or "").strip():
            return Secret(value)
    return Secret("")


def load_credentials(*, sources: Iterable[str] | None = None) -> Credentials:
    """Load selected providers, or every provider when no scope is supplied."""
    known_sources = {"kis", "kis_overseas", "opendart", "sec", "alpaca", "krx", "alpha_vantage"}
    selected_sources = known_sources if sources is None else set(sources)
    if selected_sources - known_sources:
        raise ValueError("Unknown credential source")
    include_kis = bool(selected_sources & {"kis", "kis_overseas"})
    kis, dart, krx, alpaca, alpha = {}, {}, {}, {}, {}
    if include_kis:
        kis_path = Path(os.environ.get("KIS_CONFIG_PATH") or os.environ.get("INVEST_PROTO_KIS_CONFIG_PATH") or Path.home() / "KIS/config/kis_devlp.yaml")
        kis = _read_config(kis_path.expanduser())
    if "opendart" in selected_sources:
        dart_path = Path(os.environ.get("OPENDART_CONFIG_PATH") or os.environ.get("OPEN_DART_CONFIG_PATH") or Path.home() / "OpenDART/config/opendart.yaml")
        dart = _read_config(dart_path.expanduser())
    if "krx" in selected_sources:
        krx_path = Path(os.environ.get("KRX_CONFIG_PATH") or Path.home() / "KRX/config/krx.yaml")
        krx = _read_config(krx_path.expanduser())
    if "alpaca" in selected_sources:
        alpaca_path = Path(os.environ.get("ALPACA_CONFIG_PATH") or os.environ.get("APCA_CONFIG_PATH") or Path.home() / "Alpaca/config/alpaca.yaml")
        alpaca = _read_config(alpaca_path.expanduser())
    if 'alpha_vantage' in selected_sources:
        alpha_path = Path(os.environ.get('ALPHAVANTAGE_CONFIG_PATH') or os.environ.get('ALPHA_VANTAGE_CONFIG_PATH') or Path.home() / 'AlphaVantage/config/alphavantage.yaml')
        alpha = _read_config(alpha_path.expanduser())
    return Credentials(
        _get(("KIS_APP_KEY",), kis, ("my_app", "app_key", "appkey")) if include_kis else Secret(""),
        _get(("KIS_APP_SECRET",), kis, ("my_sec", "app_secret", "appsecret")) if include_kis else Secret(""),
        _get(("OPENDART_API_KEY", "DART_API_KEY", "OPEN_DART_API_KEY"), dart,
             ("OPENDART_API_KEY", "OPEN_DART_API_KEY", "DART_API_KEY", "api_key", "opendart_api_key")) if "opendart" in selected_sources else Secret(""),
        _get(("APCA_API_KEY_ID", "ALPACA_API_KEY"), alpaca, ("APCA_API_KEY_ID", "ALPACA_API_KEY")) if "alpaca" in selected_sources else Secret(""),
        _get(("APCA_API_SECRET_KEY", "ALPACA_API_SECRET"), alpaca, ("APCA_API_SECRET_KEY", "ALPACA_API_SECRET")) if "alpaca" in selected_sources else Secret(""),
        (os.environ.get("BACKTEST_SEC_USER_AGENT") or os.environ.get("SEC_USER_AGENT", "")) if "sec" in selected_sources else "",
        _get(("KRX_API_KEY",), krx, ("KRX_API_KEY",)) if "krx" in selected_sources else Secret(""),
        _get(('ALPHAVANTAGE_API_KEY', 'ALPHA_VANTAGE_API_KEY'), alpha, ('ALPHAVANTAGE_API_KEY', 'ALPHA_VANTAGE_API_KEY', 'api_key')) if 'alpha_vantage' in selected_sources else Secret(''),
    )
