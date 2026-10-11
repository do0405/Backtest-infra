import pytest

from backtest_data.ops.postgres_tuning import (
    RELOAD_PROFILE, assert_reload_safe, tuning_commands, assert_capacity, rollback_commands,
    settings_match, confirmed_rollback,
)


def settings():
    return {name: {"value": "on", "context": "sighup"}
            for name in ("fsync", "synchronous_commit", "full_page_writes")}


def test_profile_never_weakens_durability_or_requires_restart():
    forbidden = {"fsync", "synchronous_commit", "full_page_writes", "shared_buffers", "wal_buffers"}
    assert forbidden.isdisjoint(RELOAD_PROFILE)
    assert RELOAD_PROFILE["wal_compression"] == "on"
    commands = tuning_commands(RELOAD_PROFILE)
    assert len(commands) == len(RELOAD_PROFILE)
    assert all(command.startswith("ALTER SYSTEM SET ") for command in commands)


@pytest.mark.parametrize("name", ["fsync", "synchronous_commit", "full_page_writes", "shared_buffers", "wal_buffers", "unknown"])
def test_unapproved_setting_cannot_enter_operation(name):
    with pytest.raises(ValueError, match="setting"):
        tuning_commands({name: "off"})


@pytest.mark.parametrize("value", ["16MB'; DROP TABLE price_observation; --", "-1", "", None])
def test_setting_value_is_a_bounded_sql_literal(value):
    with pytest.raises(ValueError, match="value"):
        tuning_commands({"work_mem": value})


@pytest.mark.parametrize("name", ["fsync", "synchronous_commit", "full_page_writes"])
def test_operation_requires_existing_durable_server(name):
    values = settings()
    values[name]["value"] = "off"
    with pytest.raises(ValueError, match="durability"):
        assert_reload_safe(values)


def test_postmaster_context_is_rejected_before_alter():
    values = settings()
    values.update({name: {"value": "on", "context": "sighup"} for name in RELOAD_PROFILE})
    values["work_mem"]["context"] = "postmaster"
    with pytest.raises(ValueError, match="restart"):
        assert_reload_safe(values)


def test_reloadable_current_profile_passes_with_durability_intact():
    values = settings()
    values.update({name: {"value": "on", "context": "user"} for name in RELOAD_PROFILE})
    assert_reload_safe(values)


def test_prior_units_can_be_restored_without_unapproved_keys():
    commands = tuning_commands({"work_mem": "4MB", "checkpoint_timeout": "5min", "wal_compression": "off"})
    assert "ALTER SYSTEM SET work_mem TO '4MB'" in commands


def test_capacity_reserves_space_beyond_collector_floor_and_soft_wal_limit():
    with pytest.raises(ValueError, match="capacity"):
        assert_capacity(80 * 1024 ** 3)
    assert_capacity(100 * 1024 ** 3)


def test_rollback_restores_existing_override_and_resets_only_introduced_keys():
    commands = rollback_commands({"work_mem": "4MB"}, ["work_mem", "wal_compression"])
    assert commands == ("ALTER SYSTEM SET work_mem TO '4MB'", "ALTER SYSTEM RESET wal_compression")


def test_rollback_cannot_touch_unrelated_configuration():
    with pytest.raises(ValueError, match="setting"):
        rollback_commands({}, ["fsync"])


def test_compression_alias_is_normalized_but_disabled_compression_is_not():
    assert settings_match({"wal_compression": {"value": "pglz"}}, {"wal_compression": "on"})
    assert not settings_match({"wal_compression": {"value": "off"}}, {"wal_compression": "on"})
    assert not settings_match({}, {"wal_compression": "on"})


def test_rollback_is_not_confirmed_by_reload_request_alone():
    seen = []
    original = {"work_mem": {"value": "4MB"}}
    with pytest.raises(ValueError, match="rollback"):
        confirmed_rollback(seen.append, lambda expected: None, {}, ["work_mem"], original)
    assert seen == ["ALTER SYSTEM RESET work_mem"]


def test_rollback_requires_original_effective_values_after_reload():
    with pytest.raises(ValueError, match="rollback"):
        confirmed_rollback(lambda sql: None, lambda expected: {"work_mem": {"value": "16MB"}},
                           {}, ["work_mem"], {"work_mem": {"value": "4MB"}})


def test_restore_failure_propagates_without_false_completion():
    def failed_write(sql):
        raise OSError("write failed")
    with pytest.raises(OSError):
        confirmed_rollback(failed_write, lambda expected: {}, {}, ["work_mem"], {"work_mem": {"value": "4MB"}})
