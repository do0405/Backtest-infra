"""Windowless singleton invocation watchdog; existing collector/importer stay live."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from backtest_data.ops.continuation_manager import atomic_json, read_json
from backtest_data.ops.remote_schedule import GitHubSchedule, advance
from backtest_data.remote.planner import validate_ownership


def singleton(path):
    handle = Path(path).open("a+b")
    if handle.seek(0, 2) == 0:
        handle.write(b"0"); handle.flush()
    handle.seek(0)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise RuntimeError("duplicate_watchdog_prevented") from None
    return handle


def verified_configuration(workspace, configuration):
    slots = validate_ownership(configuration)
    if len(slots) != 1 or slots[0]["source"] != "krx":
        raise ValueError("existing_krx_only_ownership_required")
    metadata = read_json(workspace / "artifacts/kr-full-workers.json")
    entry = metadata["auxiliary"]["krx_quotes"]
    handoff = read_json(entry["remote_handoff_path"])
    manager = read_json(metadata["auxiliary"]["continuation_manager"]["runtime"])
    if manager.get("roles", {}).get("krx_quotes", {}).get("status") != "operator_stopped":
        raise ValueError("local_krx_manager_fence_unconfirmed")
    if (handoff.get("owner") != "remote" or handoff.get("local_enabled") is not False
            or handoff.get("local_manager_fenced") is not True or handoff.get("operator_stop") is not True
            or handoff.get("status") != "remote_account_activated"
            or any(handoff.get(k) != configuration.get(c) for k, c in
                   (("handoff_id", "handoff_id"), ("stream_id", "owner_id"), ("manifest_id", "manifest_id")))):
        raise ValueError("original_krx_handoff_unconfirmed")
    # Pass only the existing directory to gh. Never load or copy credentials.
    directory = Path(configuration["github_config_directory"])
    if not directory.is_absolute() or not directory.is_dir():
        raise ValueError("existing_github_configuration_required")
    os.environ["GH_CONFIG_DIR"] = str(directory)
    return metadata


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--configuration", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--interval-seconds", type=int, default=300)
    parser.add_argument("--initial-budget-seconds", type=int, default=6000)
    args = parser.parse_args()
    if not 60 <= args.interval_seconds <= 3600 or not 30 <= args.initial_budget_seconds <= 6000:
        raise ValueError("bounded_poll_and_batch_required")
    workspace = Path(__file__).resolve().parents[1]
    configuration = read_json(args.configuration)
    verified_configuration(workspace, configuration)
    if not isinstance(configuration.get("owner_id"), str) or not re.fullmatch(r"[a-z0-9-]{1,60}", configuration["owner_id"]):
        raise ValueError("explicit_stream_required")
    root = args.root.resolve()
    expected_root = (workspace / "artifacts/remote-schedule-watchdog" / configuration["owner_id"]).resolve()
    if root != expected_root:
        raise ValueError("singleton_root_must_match_existing_owner")
    root.mkdir(parents=True, exist_ok=True)
    lock = singleton(root / "watchdog.lock")
    path = root / "progress.json"
    state = read_json(path) if path.exists() else {}
    runtime = {"runtime_schema_version": 1, "pid": os.getpid(),
               "started_at": datetime.now(timezone.utc).isoformat(), "command": sys.argv,
               "policy": "continuous_remote_schedule_watchdog_v1",
               "overall_budget_seconds": None, "retry_count_limit": None}
    gh = shutil.which("gh") or str(workspace / "artifacts/github-setup/cli/bin/gh.exe")
    runtime.update(status="checking_remote", checked_at=runtime["started_at"])
    atomic_json(root / "runtime.json", runtime)

    def save(value):
        atomic_json(path, value)
        if value.get("intent"):
            directory = root / "intents"; directory.mkdir(exist_ok=True)
            atomic_json(directory / (value["intent"]["request_id"] + ".json"), value["intent"])

    try:
        while True:
            try:
                configuration = read_json(args.configuration)
                metadata = verified_configuration(workspace, configuration)
                sync = read_json(metadata["auxiliary"]["remote_release_sync"]["progress_path"])
                if sync.get("requires_user") is True:
                    state.update(status="local_sync_action_required", requires_user=True)
                elif shutil.disk_usage(root).free < 50 * 1024**3:
                    state.update(status="storage_space_required", requires_user=True)
                else:
                    client = GitHubSchedule(gh, configuration["repository"])
                    view = client.snapshot()
                    budget = 6000 if state.get("intent") else args.initial_budget_seconds
                    advance(state, view, client.dispatch, save,
                            now=datetime.now(timezone.utc), budget_seconds=budget)
                state.pop("error_type", None)
            except Exception as exc:
                permanent = isinstance(exc, ValueError) and not isinstance(exc, json.JSONDecodeError)
                state.update(status="action_required" if permanent else "waiting_for_read_retry",
                             requires_user=permanent, error_type=type(exc).__name__)
            state.update(checked_at=datetime.now(timezone.utc).isoformat())
            save(state)
            runtime.update(status="running", checked_at=state["checked_at"])
            atomic_json(root / "runtime.json", runtime)
            time.sleep(args.interval_seconds)
    finally:
        runtime.update(status="finished", finished_at=datetime.now(timezone.utc).isoformat())
        atomic_json(root / "runtime.json", runtime)
        lock.close()


if __name__ == "__main__":
    main()
