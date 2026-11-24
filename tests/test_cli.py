import importlib.metadata
import subprocess
import sys

import tau_acp


def run(*args: str, stdin: int | None = None) -> subprocess.CompletedProcess[str]:
    """Run tau-acp with stdin a pipe that is already closed (or ``stdin``)."""
    return subprocess.run(
        [sys.executable, "-m", "tau_acp", *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
        stdin=stdin,
        input="" if stdin is None else None,
    )


def test_version_matches_installed_metadata():
    assert tau_acp.__version__ == importlib.metadata.version("ffwf-tau-acp")


def test_cli_version():
    result = run("--version")
    assert result.returncode == 0
    assert result.stdout.strip() == f"tau-acp {tau_acp.__version__}"


def test_unknown_option_is_refused():
    result = run("--bogus")
    assert result.returncode == 2
    assert "unrecognized arguments" in result.stderr


def test_serve_arguments_after_the_separator_are_not_tau_acps():
    result = run("--", "--bogus")  # stdin is closed: it starts, finds EOF, and exits
    assert result.returncode == 0
    assert result.stdout == ""


def test_stdin_that_is_not_a_pipe_is_refused():
    result = run(stdin=subprocess.DEVNULL)
    assert result.returncode == 2
    assert "stdin must be a pipe" in result.stderr
