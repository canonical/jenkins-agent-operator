# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Tests for the rendered Jenkins agent launcher."""

from __future__ import annotations

import os
import subprocess  # nosec B404 - tests execute disposable fake commands
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import service
from charm_state import State


def _write_executable(path: Path, content: str) -> None:
    """Write an executable test command."""
    path.write_text(content)
    path.chmod(0o755)


def _run_launcher(
    tmp_path: Path,
    home: Path,
    *,
    reject_direct_download: bool = False,
    fail_download: bool = False,
    fail_move: bool = False,
) -> SimpleNamespace:
    """Render and run the launcher against deterministic fake curl and java commands."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    curl_output = tmp_path / "curl-output"
    java_called = tmp_path / "java-called"
    _write_executable(
        fake_bin / "curl",
        """#!/bin/bash
set -eu
output=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        -o|--output)
            output="$2"
            shift 2
            ;;
        *) shift ;;
    esac
done
printf '%s\\n' "$output" > "$FAKE_CURL_OUTPUT"
if [ "${FAKE_CURL_REJECT_DIRECT:-false}" = true ] && [ "$output" = "$JENKINS_HOME/agent.jar" ]; then
    echo "legacy agent.jar is not writable" >&2
    exit 23
fi
printf 'new-agent' > "$output"
if [ "${FAKE_CURL_FAIL:-false}" = true ]; then
    echo "simulated interrupted download" >&2
    exit 23
fi
""",
    )
    _write_executable(
        fake_bin / "java",
        """#!/bin/bash
set -eu
[ "$1" = "-jar" ]
[ -f "$2" ]
[ "$(cat -- "$2")" = "new-agent" ]
[ -f "$JENKINS_HOME/.ready" ]
touch -- "$FAKE_JAVA_CALLED"
""",
    )
    if fail_move:
        _write_executable(
            fake_bin / "mv",
            """#!/bin/bash
echo "simulated move failure" >&2
exit 1
""",
        )
    state = cast(
        State, SimpleNamespace(agent_user="jenkins", jenkins_home=home, websocket_mode=True)
    )
    _, script = service.JenkinsAgentService(state)._render_service_files()
    launcher = tmp_path / "jenkins-agent"
    _write_executable(launcher, script)
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "JENKINS_HOME": str(home),
        "JENKINS_URL": "http://jenkins.test",
        "JENKINS_AGENT": "test-agent",
        "JENKINS_TOKEN": "test-token",
        "FAKE_CURL_OUTPUT": str(curl_output),
        "FAKE_JAVA_CALLED": str(java_called),
        "FAKE_CURL_REJECT_DIRECT": str(reject_direct_download).lower(),
        "FAKE_CURL_FAIL": str(fail_download).lower(),
    }
    result = subprocess.run(  # nosec B603 - launcher and commands are test fixtures
        [str(launcher)], capture_output=True, text=True, env=env, check=False
    )
    return SimpleNamespace(result=result, curl_output=curl_output, java_called=java_called)


def test_launcher_replaces_unwritable_legacy_agent_jar(tmp_path: Path):
    """Download beside agent.jar before replacing a legacy unwritable file."""
    home = tmp_path / "jenkins-home"
    home.mkdir()
    agent_jar = home / "agent.jar"
    agent_jar.write_text("legacy-agent")
    agent_jar.chmod(0o444)

    run = _run_launcher(tmp_path, home, reject_direct_download=True)

    assert run.result.returncode == 0, run.result.stderr
    assert agent_jar.read_text() == "new-agent"
    download_path = Path(run.curl_output.read_text().strip())
    assert download_path.parent == home
    assert download_path != agent_jar
    assert not list(home.glob(".agent.jar.*"))
    assert run.java_called.exists()


def test_launcher_cleans_partial_download_and_preserves_previous_jar(tmp_path: Path):
    """An interrupted download leaves the installed agent and home unchanged."""
    home = tmp_path / "jenkins-home"
    home.mkdir()
    agent_jar = home / "agent.jar"
    agent_jar.write_text("legacy-agent")

    run = _run_launcher(tmp_path, home, fail_download=True)

    assert run.result.returncode == 1
    assert "Unable to download agent binary" in run.result.stderr
    assert agent_jar.read_text() == "legacy-agent"
    assert not list(home.glob(".agent.jar.*"))
    assert not run.java_called.exists()


def test_launcher_replaces_agent_jar_symlink_without_writing_target(tmp_path: Path):
    """Replacing agent.jar must not follow a symlink to another directory."""
    home = tmp_path / "jenkins-home"
    home.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_text("unchanged")
    agent_jar = home / "agent.jar"
    agent_jar.symlink_to(outside, target_is_directory=True)

    run = _run_launcher(tmp_path, home)

    assert run.result.returncode == 0, run.result.stderr
    assert not agent_jar.is_symlink()
    assert agent_jar.read_text() == "new-agent"
    assert sentinel.read_text() == "unchanged"
    assert list(outside.iterdir()) == [sentinel]


def test_launcher_replaces_stale_ready_marker_symlink(tmp_path: Path):
    """The readiness marker is replaced without following a stale symlink."""
    home = tmp_path / "jenkins-home"
    home.mkdir()
    outside = tmp_path / "outside-ready"
    outside.write_text("unchanged")
    (home / ".ready").symlink_to(outside)

    run = _run_launcher(tmp_path, home)

    assert run.result.returncode == 0, run.result.stderr
    assert (home / ".ready").is_file()
    assert not (home / ".ready").is_symlink()
    assert outside.read_text() == "unchanged"
    assert run.java_called.exists()


def test_launcher_reports_replace_failure_and_cleans_temporary_files(tmp_path: Path):
    """A failed atomic replacement is clear and leaves no temporary files."""
    home = tmp_path / "jenkins-home"
    home.mkdir()
    agent_jar = home / "agent.jar"
    agent_jar.write_text("legacy-agent")

    run = _run_launcher(tmp_path, home, fail_move=True)

    assert run.result.returncode == 1
    assert "Unable to install agent binary" in run.result.stderr
    assert agent_jar.read_text() == "legacy-agent"
    assert not list(home.glob(".agent.jar.*"))
    assert not list(home.glob(".ready.*"))
    assert not run.java_called.exists()


def test_launcher_quotes_home_paths(tmp_path: Path):
    """Homes containing spaces and shell metacharacters remain usable."""
    home = tmp_path / "home with spaces;and$chars"
    home.mkdir()

    run = _run_launcher(tmp_path, home)

    assert run.result.returncode == 0, run.result.stderr
    assert (home / "agent.jar").read_text() == "new-agent"
    assert run.java_called.exists()


def test_launcher_preserves_legacy_jar_on_move_failure(tmp_path: Path):
    """A failed rename does not destroy the previous agent binary."""
    home = tmp_path / "jenkins-home"
    home.mkdir()
    agent_jar = home / "agent.jar"
    agent_jar.write_text("legacy-agent")

    run = _run_launcher(tmp_path, home, fail_move=True)

    assert run.result.returncode == 1
    assert agent_jar.read_text() == "legacy-agent"
    assert not list(home.glob(".agent.jar.*"))
