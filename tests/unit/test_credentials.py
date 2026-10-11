from pathlib import Path
import traceback

import pytest

from backtest_data.credentials import Credentials, Secret, _read_config, load_credentials


@pytest.fixture(autouse=True)
def isolated_provider_configuration(tmp_path, monkeypatch):
    """Never read credentials from the developer's environment or home folder."""
    names = (
        "KIS_CONFIG_PATH", "INVEST_PROTO_KIS_CONFIG_PATH", "OPENDART_CONFIG_PATH",
        "OPEN_DART_CONFIG_PATH", "KRX_CONFIG_PATH", "ALPACA_CONFIG_PATH", "APCA_CONFIG_PATH",
        "KIS_APP_KEY", "KIS_APP_SECRET", "OPENDART_API_KEY", "DART_API_KEY",
        "OPEN_DART_API_KEY", "KRX_API_KEY", "KRX_AUTH_KEY", "APCA_API_KEY_ID",
        "APCA_API_SECRET_KEY", "ALPACA_API_KEY", "ALPACA_API_SECRET",
        "BACKTEST_SEC_USER_AGENT", "SEC_USER_AGENT",
        "ALPHAVANTAGE_CONFIG_PATH", "ALPHA_VANTAGE_CONFIG_PATH", "ALPHAVANTAGE_API_KEY", "ALPHA_VANTAGE_API_KEY",
    )
    for name in names:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))


def write_config(path, contents):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents, encoding="utf-8")
    return path

def test_alpha_vantage_scoped_config_and_override(tmp_path, monkeypatch):
    write_config(tmp_path / 'AlphaVantage/config/alphavantage.yaml', 'ALPHAVANTAGE_API_KEY: dummy-alpha\n')
    write_config(tmp_path / 'Alpaca/config/alpaca.yaml', 'invalid: [')
    credentials = load_credentials(sources=['alpha_vantage'])
    assert credentials.alpha_vantage_api_key.reveal() == 'dummy-alpha'
    assert not credentials.alpaca_key
    assert 'dummy-alpha' not in repr(credentials)
    monkeypatch.setenv('ALPHAVANTAGE_API_KEY', 'dummy-env-alpha')
    assert load_credentials(sources=['alpha_vantage']).alpha_vantage_api_key.reveal() == 'dummy-env-alpha'


def test_existing_shared_configs_and_secret_repr(tmp_path, monkeypatch):
    kis = tmp_path / "kis.yaml"
    dart = tmp_path / "dart.yaml"
    kis.write_text("my_app: dummy-key\nmy_sec: dummy-secret\n", encoding="utf-8")
    dart.write_text("OPENDART_API_KEY: dummy-dart-key\n", encoding="utf-8")
    monkeypatch.setenv("KIS_CONFIG_PATH", str(kis))
    monkeypatch.setenv("OPENDART_CONFIG_PATH", str(dart))
    credentials = load_credentials()
    assert credentials.kis_app_key.reveal() == "dummy-key"
    assert credentials.opendart_api_key.reveal() == "dummy-dart-key"
    assert "dummy" not in repr(credentials)


def test_provider_configs_follow_existing_home_folder_convention(tmp_path):
    write_config(tmp_path / "KIS/config/kis_devlp.yaml", "my_app: dummy-kis-key\nmy_sec: dummy-kis-secret\n")
    write_config(tmp_path / "OpenDART/config/opendart.yaml", "OPENDART_API_KEY: dummy-dart-key\n")
    write_config(tmp_path / "KRX/config/krx.yaml", "KRX_API_KEY: dummy-krx-key\n")
    write_config(tmp_path / "Alpaca/config/alpaca.yaml", "APCA_API_KEY_ID: dummy-alpaca-key\nAPCA_API_SECRET_KEY: dummy-alpaca-secret\n")

    credentials = load_credentials()

    assert credentials.kis_app_key.reveal() == "dummy-kis-key"
    assert credentials.kis_app_secret.reveal() == "dummy-kis-secret"
    assert credentials.opendart_api_key.reveal() == "dummy-dart-key"
    assert credentials.krx_api_key.reveal() == "dummy-krx-key"
    assert credentials.alpaca_key.reveal() == "dummy-alpaca-key"
    assert credentials.alpaca_secret.reveal() == "dummy-alpaca-secret"
    assert "dummy" not in repr(credentials)


def test_krx_config_path_overrides_default(tmp_path, monkeypatch):
    write_config(tmp_path / "KRX/config/krx.yaml", "KRX_API_KEY: dummy-default-key\n")
    override = write_config(tmp_path / "override.yaml", "KRX_API_KEY: dummy-override-key\n")
    monkeypatch.setenv("KRX_CONFIG_PATH", str(override))

    assert load_credentials().krx_api_key.reveal() == "dummy-override-key"


@pytest.mark.parametrize("path_variable", ["ALPACA_CONFIG_PATH", "APCA_CONFIG_PATH"])
def test_alpaca_config_path_overrides_default(tmp_path, monkeypatch, path_variable):
    write_config(tmp_path / "Alpaca/config/alpaca.yaml", "APCA_API_KEY_ID: dummy-default-key\n")
    override = write_config(tmp_path / "override.yaml", "APCA_API_KEY_ID: dummy-override-key\nAPCA_API_SECRET_KEY: dummy-override-secret\n")
    monkeypatch.setenv(path_variable, str(override))

    credentials = load_credentials()

    assert credentials.alpaca_key.reveal() == "dummy-override-key"
    assert credentials.alpaca_secret.reveal() == "dummy-override-secret"


def test_alpaca_config_path_takes_precedence_over_apca_alias(tmp_path, monkeypatch):
    preferred = write_config(tmp_path / "preferred.yaml", "APCA_API_KEY_ID: dummy-preferred-key\n")
    alternate = write_config(tmp_path / "alternate.yaml", "APCA_API_KEY_ID: dummy-alternate-key\n")
    monkeypatch.setenv("ALPACA_CONFIG_PATH", str(preferred))
    monkeypatch.setenv("APCA_CONFIG_PATH", str(alternate))

    assert load_credentials().alpaca_key.reveal() == "dummy-preferred-key"


@pytest.mark.parametrize(
    ("environment_name", "attribute", "value"),
    [
        ("KRX_API_KEY", "krx_api_key", "dummy-env-krx-key"),
        ("APCA_API_KEY_ID", "alpaca_key", "dummy-env-alpaca-key"),
        ("APCA_API_SECRET_KEY", "alpaca_secret", "dummy-env-alpaca-secret"),
        ("ALPACA_API_KEY", "alpaca_key", "dummy-env-alpaca-alias-key"),
        ("ALPACA_API_SECRET", "alpaca_secret", "dummy-env-alpaca-alias-secret"),
    ],
)
def test_environment_credentials_override_yaml(tmp_path, monkeypatch, environment_name, attribute, value):
    write_config(tmp_path / "KRX/config/krx.yaml", "KRX_API_KEY: dummy-file-krx-key\n")
    write_config(tmp_path / "Alpaca/config/alpaca.yaml", "APCA_API_KEY_ID: dummy-file-alpaca-key\nAPCA_API_SECRET_KEY: dummy-file-alpaca-secret\n")
    monkeypatch.setenv(environment_name, f"  {value}  ")

    assert getattr(load_credentials(), attribute).reveal() == value


def test_alpaca_primary_environment_names_take_precedence_over_aliases(monkeypatch):
    monkeypatch.setenv("APCA_API_KEY_ID", "dummy-primary-key")
    monkeypatch.setenv("ALPACA_API_KEY", "dummy-alias-key")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "dummy-primary-secret")
    monkeypatch.setenv("ALPACA_API_SECRET", "dummy-alias-secret")

    credentials = load_credentials()

    assert credentials.alpaca_key.reveal() == "dummy-primary-key"
    assert credentials.alpaca_secret.reveal() == "dummy-primary-secret"


def test_blank_environment_values_fall_back_to_yaml(tmp_path, monkeypatch):
    write_config(tmp_path / "KRX/config/krx.yaml", "KRX_API_KEY: '  dummy-file-krx-key  '\n")
    write_config(tmp_path / "Alpaca/config/alpaca.yaml", "APCA_API_KEY_ID: dummy-file-alpaca-key\nAPCA_API_SECRET_KEY: dummy-file-alpaca-secret\n")
    for name in ("KRX_API_KEY", "APCA_API_KEY_ID", "APCA_API_SECRET_KEY"):
        monkeypatch.setenv(name, "   ")

    credentials = load_credentials()

    assert credentials.krx_api_key.reveal() == "dummy-file-krx-key"
    assert credentials.alpaca_key.reveal() == "dummy-file-alpaca-key"
    assert credentials.alpaca_secret.reveal() == "dummy-file-alpaca-secret"


def test_missing_provider_files_leave_credentials_empty():
    credentials = load_credentials()

    assert not credentials.krx_api_key
    assert not credentials.alpaca_key
    assert not credentials.alpaca_secret


def test_existing_six_positional_credentials_arguments_remain_compatible():
    credentials = Credentials(*(Secret("") for _ in range(5)), "dummy-user-agent")

    assert not credentials.krx_api_key
    assert credentials.sec_user_agent == "dummy-user-agent"


def test_malformed_yaml_error_does_not_expose_source_or_exception_context(tmp_path):
    path = write_config(tmp_path / "invalid.yaml", "KRX_API_KEY: [dummy-sensitive-token\n")

    with pytest.raises(ValueError, match="Provider configuration") as error:
        _read_config(path)

    rendered = "".join(traceback.format_exception(error.value))
    assert "dummy-sensitive-token" not in rendered
    assert str(path) not in str(error.value)


def test_invalid_encoding_error_is_sanitized(tmp_path):
    path = tmp_path / "invalid.yaml"
    path.write_bytes(b"KRX_API_KEY: dummy-sensitive-token\xff")

    with pytest.raises(ValueError, match="Provider configuration") as error:
        _read_config(path)

    assert "dummy-sensitive-token" not in "".join(traceback.format_exception(error.value))


def test_file_read_error_is_sanitized(tmp_path, monkeypatch):
    path = write_config(tmp_path / "unreadable.yaml", "KRX_API_KEY: dummy-sensitive-token\n")

    def unreadable(*args, **kwargs):
        raise OSError("dummy-sensitive-token")

    monkeypatch.setattr(Path, "read_text", unreadable)

    with pytest.raises(ValueError, match="Provider configuration") as error:
        _read_config(path)

    assert "dummy-sensitive-token" not in "".join(traceback.format_exception(error.value))


@pytest.mark.parametrize("contents", ["- dummy-sensitive-token\n", "dummy-sensitive-token\n", "false\n", "0\n", "[]\n"])
def test_non_mapping_configurations_are_rejected_without_values(tmp_path, contents):
    path = write_config(tmp_path / "non-mapping.yaml", contents)

    with pytest.raises(ValueError, match="must be a mapping") as error:
        _read_config(path)

    assert "dummy-sensitive-token" not in str(error.value)


def test_provider_configuration_keeps_one_mib_size_limit(tmp_path):
    path = write_config(tmp_path / "oversized.yaml", "#" + "x" * (1024 * 1024))

    with pytest.raises(ValueError, match="exceeds size limit"):
        _read_config(path)


def test_empty_configuration_is_allowed(tmp_path):
    assert _read_config(write_config(tmp_path / "empty.yaml", "")) == {}


def test_configuration_supports_utf8_bom(tmp_path):
    path = tmp_path / "bom.yaml"
    path.write_bytes(b"\xef\xbb\xbfKRX_API_KEY: dummy-bom-key\n")

    assert _read_config(path) == {"KRX_API_KEY": "dummy-bom-key"}


def test_alpaca_scope_ignores_malformed_unrelated_krx_config(tmp_path):
    write_config(tmp_path / "Alpaca/config/alpaca.yaml", "APCA_API_KEY_ID: dummy-alpaca-key\nAPCA_API_SECRET_KEY: dummy-alpaca-secret\n")
    write_config(tmp_path / "KRX/config/krx.yaml", "KRX_API_KEY: [dummy-sensitive-token\n")

    credentials = load_credentials(sources=["alpaca"])

    assert credentials.alpaca_key.reveal() == "dummy-alpaca-key"
    assert credentials.alpaca_secret.reveal() == "dummy-alpaca-secret"
    assert not credentials.krx_api_key


@pytest.mark.parametrize("source", ["kis", "kis_overseas"])
def test_kis_scopes_ignore_malformed_new_provider_configs(tmp_path, source):
    write_config(tmp_path / "KIS/config/kis_devlp.yaml", "my_app: dummy-kis-key\nmy_sec: dummy-kis-secret\n")
    write_config(tmp_path / "KRX/config/krx.yaml", "KRX_API_KEY: [dummy-sensitive-token\n")
    write_config(tmp_path / "Alpaca/config/alpaca.yaml", "APCA_API_KEY_ID: [dummy-sensitive-token\n")

    credentials = load_credentials(sources=[source])

    assert credentials.kis_app_key.reveal() == "dummy-kis-key"
    assert credentials.kis_app_secret.reveal() == "dummy-kis-secret"
    assert not credentials.krx_api_key
    assert not credentials.alpaca_key
    assert not credentials.alpaca_secret


def test_opendart_scope_ignores_malformed_unrelated_provider_configs(tmp_path):
    write_config(tmp_path / "OpenDART/config/opendart.yaml", "OPENDART_API_KEY: dummy-dart-key\n")
    write_config(tmp_path / "KIS/config/kis_devlp.yaml", "my_app: [dummy-sensitive-token\n")
    write_config(tmp_path / "KRX/config/krx.yaml", "KRX_API_KEY: [dummy-sensitive-token\n")
    write_config(tmp_path / "Alpaca/config/alpaca.yaml", "APCA_API_KEY_ID: [dummy-sensitive-token\n")

    credentials = load_credentials(sources=["opendart"])

    assert credentials.opendart_api_key.reveal() == "dummy-dart-key"
    assert not credentials.kis_app_key
    assert not credentials.krx_api_key
    assert not credentials.alpaca_key


@pytest.mark.parametrize(
    ("source", "path", "contents"),
    [
        ("kis", "KIS/config/kis_devlp.yaml", "my_app: [dummy-sensitive-token\n"),
        ("kis_overseas", "KIS/config/kis_devlp.yaml", "my_app: [dummy-sensitive-token\n"),
        ("opendart", "OpenDART/config/opendart.yaml", "OPENDART_API_KEY: [dummy-sensitive-token\n"),
        ("krx", "KRX/config/krx.yaml", "KRX_API_KEY: [dummy-sensitive-token\n"),
        ("alpaca", "Alpaca/config/alpaca.yaml", "APCA_API_KEY_ID: [dummy-sensitive-token\n"),
    ],
)
def test_selected_malformed_provider_still_raises_sanitized_error(tmp_path, source, path, contents):
    write_config(tmp_path / path, contents)

    with pytest.raises(ValueError, match="Provider configuration") as error:
        load_credentials(sources=[source])

    assert "dummy-sensitive-token" not in "".join(traceback.format_exception(error.value))


def test_krx_scope_loads_only_krx_config(tmp_path):
    write_config(tmp_path / "KRX/config/krx.yaml", "KRX_API_KEY: dummy-krx-key\n")
    write_config(tmp_path / "Alpaca/config/alpaca.yaml", "APCA_API_KEY_ID: [dummy-sensitive-token\n")

    assert load_credentials(sources=["krx"]).krx_api_key.reveal() == "dummy-krx-key"


def test_sec_scope_reads_user_agent_without_reading_provider_files(tmp_path, monkeypatch):
    for path in ("KIS/config/kis_devlp.yaml", "OpenDART/config/opendart.yaml", "KRX/config/krx.yaml", "Alpaca/config/alpaca.yaml"):
        write_config(tmp_path / path, "API_KEY: [dummy-sensitive-token\n")
    monkeypatch.setenv("BACKTEST_SEC_USER_AGENT", "dummy-user-agent")

    credentials = load_credentials(sources=["sec"])

    assert credentials.sec_user_agent == "dummy-user-agent"
    assert not credentials.kis_app_key
    assert not credentials.opendart_api_key
    assert not credentials.krx_api_key
    assert not credentials.alpaca_key


def test_scope_leaves_unrelated_environment_credentials_empty(monkeypatch):
    monkeypatch.setenv("KIS_APP_KEY", "dummy-kis-key")
    monkeypatch.setenv("KIS_APP_SECRET", "dummy-kis-secret")
    monkeypatch.setenv("OPENDART_API_KEY", "dummy-dart-key")
    monkeypatch.setenv("KRX_API_KEY", "dummy-krx-key")
    monkeypatch.setenv("APCA_API_KEY_ID", "dummy-alpaca-key")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "dummy-alpaca-secret")

    credentials = load_credentials(sources=["alpaca"])

    assert credentials.alpaca_key.reveal() == "dummy-alpaca-key"
    assert credentials.alpaca_secret.reveal() == "dummy-alpaca-secret"
    assert not credentials.kis_app_key
    assert not credentials.kis_app_secret
    assert not credentials.opendart_api_key
    assert not credentials.krx_api_key


def test_multiple_sources_load_selected_credentials(tmp_path):
    write_config(tmp_path / "KRX/config/krx.yaml", "KRX_API_KEY: dummy-krx-key\n")
    write_config(tmp_path / "Alpaca/config/alpaca.yaml", "APCA_API_KEY_ID: dummy-alpaca-key\n")

    credentials = load_credentials(sources=(source for source in ["krx", "alpaca"]))

    assert credentials.krx_api_key.reveal() == "dummy-krx-key"
    assert credentials.alpaca_key.reveal() == "dummy-alpaca-key"


def test_explicit_empty_scope_reads_no_provider_files(tmp_path, monkeypatch):
    write_config(tmp_path / "KRX/config/krx.yaml", "KRX_API_KEY: [dummy-sensitive-token\n")
    monkeypatch.setenv("APCA_API_KEY_ID", "dummy-alpaca-key")

    credentials = load_credentials(sources=[])

    assert not credentials.kis_app_key
    assert not credentials.kis_app_secret
    assert not credentials.opendart_api_key
    assert not credentials.krx_api_key
    assert not credentials.alpaca_key
    assert not credentials.alpaca_secret


def test_unknown_source_is_rejected_before_loading_configuration(tmp_path):
    write_config(tmp_path / "KRX/config/krx.yaml", "KRX_API_KEY: [dummy-sensitive-token\n")

    with pytest.raises(ValueError, match="Unknown credential source"):
        load_credentials(sources=["unknown-provider"])


def test_default_scope_preserves_loading_all_provider_files(tmp_path):
    write_config(tmp_path / "KRX/config/krx.yaml", "KRX_API_KEY: [dummy-sensitive-token\n")

    with pytest.raises(ValueError, match="Provider configuration"):
        load_credentials()
