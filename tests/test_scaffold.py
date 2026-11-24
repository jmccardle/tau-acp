import importlib.metadata
import subprocess
import sys

import tau_acp


def test_version_matches_installed_metadata():
    assert isinstance(tau_acp.__version__, str)
    assert tau_acp.__version__
    assert tau_acp.__version__ == importlib.metadata.version("ffwf-tau-acp")


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "tau_acp", *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def test_cli_version():
    result = _run("--version")
    assert result.returncode == 0
    assert result.stdout.strip() == f"tau-acp {tau_acp.__version__}"


def test_no_args_is_not_built():
    result = _run()
    assert result.returncode != 0
    assert "not built" in result.stderr


def test_serve_is_not_built():
    result = _run("serve")
    assert result.returncode != 0
    assert "not built" in result.stderr
