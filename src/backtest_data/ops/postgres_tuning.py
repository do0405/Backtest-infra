"""Small reload-only operational profile; ingestion/durability policy stays outside."""
import re
from types import MappingProxyType


RELOAD_PROFILE = MappingProxyType({
    "wal_compression": "on",
    "checkpoint_timeout": "30min",
    "max_wal_size": "8GB",
    "min_wal_size": "2GB",
    "work_mem": "16MB",
    "effective_cache_size": "8GB",
    "track_io_timing": "on",
    "track_wal_io_timing": "on",
})
DURABILITY_SETTINGS = ("fsync", "synchronous_commit", "full_page_writes")
MINIMUM_FREE_BYTES = 100 * 1024 ** 3
_VALUE = re.compile(r"(?:on|off|pglz|lz4|zstd|[0-9]+(?:\.[0-9]+)?(?:kB|MB|GB|ms|s|min|h)?)\Z")


def assert_reload_safe(settings):
    """Reject unknown durability/current contexts before any configuration write."""
    if any(settings.get(name, {}).get("value") != "on" for name in DURABILITY_SETTINGS):
        raise ValueError("existing_server_durability_not_confirmed")
    for name in RELOAD_PROFILE:
        context = settings.get(name, {}).get("context")
        if context == "postmaster":
            raise ValueError("setting_requires_restart")
        if context not in {"user", "superuser", "sighup"}:
            raise ValueError("required_setting_context_unknown")


def tuning_commands(settings):
    """Allow only this profile's keys and simple typed, injection-safe literals.

    The same restricted formatter can restore filtered original values. Neither
    source rows nor caller lease rules are represented by this interface.
    """
    commands = []
    for name, value in settings.items():
        if name not in RELOAD_PROFILE:
            raise ValueError("unapproved_setting")
        if not isinstance(value, str) or not _VALUE.fullmatch(value):
            raise ValueError("unsafe_setting_value")
        commands.append(f"ALTER SYSTEM SET {name} TO '{value}'")
    return tuple(commands)


def assert_capacity(free_bytes):
    if type(free_bytes) is not int or free_bytes < MINIMUM_FREE_BYTES:
        raise ValueError("operational_capacity_reserve_not_met")


def rollback_commands(prior_overrides, changed_names):
    commands = []
    for name in changed_names:
        if name not in RELOAD_PROFILE:
            raise ValueError("unapproved_setting")
        if name in prior_overrides:
            commands.extend(tuning_commands({name: prior_overrides[name]}))
        else:
            commands.append(f"ALTER SYSTEM RESET {name}")
    return tuple(commands)


def settings_match(observed, expected):
    for name, value in expected.items():
        current = observed.get(name, {}).get("value")
        if name == "wal_compression":
            current = "pglz" if current == "on" else current
            value = "pglz" if value == "on" else value
        if current != value:
            return False
    return True


def confirmed_rollback(execute, reload_and_observe, prior_overrides, changed_names, original_settings):
    for command in rollback_commands(prior_overrides, changed_names):
        execute(command)
    expected = {name: original_settings[name]["value"] for name in changed_names}
    observed = reload_and_observe(expected)
    if observed is None or not settings_match(observed, expected):
        raise ValueError("rollback_effective_settings_unconfirmed")
    return observed
