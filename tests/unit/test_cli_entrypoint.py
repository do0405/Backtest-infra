import os
import subprocess
import sys


def test_module_help_is_usable_without_credentials():
    result = subprocess.run(
        [sys.executable, "-m", "backtest_data", "--help"],
        env={**os.environ, "PYTHONPATH": "src"}, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "collect" in result.stdout
    assert "status" in result.stdout


def test_invalid_command_exits_nonzero():
    result = subprocess.run(
        [sys.executable, "-m", "backtest_data", "not-a-command"],
        env={**os.environ, "PYTHONPATH": "src"}, capture_output=True, text=True,
    )
    assert result.returncode != 0
