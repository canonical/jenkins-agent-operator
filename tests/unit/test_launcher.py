# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Tests for the rendered Jenkins agent launcher."""

from __future__ import annotations

import os
import shutil
import subprocess  # nosec B404 - tests execute disposable fake commands
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

import service
from charm_state import State


@dataclass
class LauncherRun:
    """Result and observable files from one launcher execution."""

    result: subprocess.CompletedProcess[str]
    curl_output: Path
    java_called: Path


def _install_fake_command(fake_bin: Path, name: str) -> None:
    """Copy an executable command fixture into the test's private PATH."""
    source = Path(__file__).parent / "data" / "launcher" / name
    target = fake_bin / name
    shutil.copy2(source, target)
    target.chmod(0o755)


def _run_launcher(
    tmp_path: Path,
    home: Path,
    *,
    curl_mode: str = "success",
    move_failure: str = "none",
) -> LauncherRun:
    """Render and run the launcher with explicit fake-command modes."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _install_fake_command(fake_bin, "curl")
    _install_fake_command(fake_bin, "java")
    if move_failure != "none":
        _install_fake_command(fake_bin, "mv")
    curl_output = tmp_path / "curl-output"
    java_called = tmp_path / "java-called"
    state = cast(
        State, SimpleNamespace(agent_user="jenkins", jenkins_home=home, websocket_mode=True)
    )
    _, script = service.JenkinsAgentService(state)._render_service_files()
    launcher = tmp_path / "jenkins-agent"
    launcher.write_text(script)
    launcher.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "JENKINS_HOME": str(home),
        "JENKINS_URL": "http://jenkins.test",
        "JENKINS_AGENT": "test-agent",
        "JENKINS_TOKEN": "test-token",
        "FAKE_CURL_OUTPUT": str(curl_output),
        "FAKE_JAVA_CALLED": str(java_called),
        "FAKE_CURL_MODE": curl_mode,
        "FAKE_MV_FAILURE": move_failure,
    }
    result = subprocess.run(  # nosec B603 - launcher and commands are test fixtures
        [str(launcher)], capture_output=True, text=True, env=env, check=False
    )
    return LauncherRun(result=result, curl_output=curl_output, java_called=java_called)


def test_launcher_replaces_unwritable_legacy_agent_jar(tmp_path: Path):
    """
    Arrange: an old agent JAR is present and direct download is rejected.
    Act: run the launcher.
    Assert: a same-directory temporary file replaces the old JAR.
    """
    home = tmp_path / "jenkins-home"
    home.mkdir()
    agent_jar = home / "agent.jar"
    agent_jar.write_text("legacy-agent")
    agent_jar.chmod(0o444)

    run = _run_launcher(tmp_path, home, curl_mode="reject-direct")

    assert run.result.returncode == 0, run.result.stderr
    assert agent_jar.read_text() == "new-agent"
    download_path = Path(run.curl_output.read_text().strip())
    assert download_path.parent == home
    assert download_path != agent_jar
    assert not list(home.glob(".agent.jar.*"))
    assert run.java_called.exists()


@pytest.mark.parametrize("curl_mode", ["interrupted", "http-error"])
def test_launcher_preserves_previous_jar_on_download_failure(tmp_path: Path, curl_mode: str):
    """
    Arrange: an old agent JAR is present and the download fails.
    Act: run the launcher with an interrupted or HTTP-error response.
    Assert: the old JAR remains and Java is not started.
    """
    home = tmp_path / "jenkins-home"
    home.mkdir()
    agent_jar = home / "agent.jar"
    agent_jar.write_text("legacy-agent")

    run = _run_launcher(tmp_path, home, curl_mode=curl_mode)

    assert run.result.returncode == 1
    assert "Unable to download agent binary" in run.result.stderr
    assert agent_jar.read_text() == "legacy-agent"
    assert not list(home.glob(".agent.jar.*"))
    assert not run.java_called.exists()


def test_launcher_replaces_agent_jar_symlink_without_writing_target(tmp_path: Path):
    """
    Arrange: agent.jar is a symlink to an outside directory.
    Act: run the launcher.
    Assert: the symlink entry is replaced and its target remains unchanged.
    """
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
    """
    Arrange: the readiness marker is a symlink to an outside file.
    Act: run the launcher.
    Assert: the marker entry is replaced without changing its target.
    """
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


def test_launcher_reports_agent_jar_replace_failure_and_cleans_temporary_file(
    tmp_path: Path,
):
    """
    Arrange: the agent JAR replacement command fails.
    Act: run the launcher.
    Assert: the old JAR is preserved and the temporary file is removed.
    """
    home = tmp_path / "jenkins-home"
    home.mkdir()
    agent_jar = home / "agent.jar"
    agent_jar.write_text("legacy-agent")

    run = _run_launcher(tmp_path, home, move_failure="agent-jar")

    assert run.result.returncode == 1
    assert "Unable to install agent binary" in run.result.stderr
    assert agent_jar.read_text() == "legacy-agent"
    assert not list(home.glob(".agent.jar.*"))
    assert not run.java_called.exists()


def test_launcher_reports_ready_marker_replace_failure_and_cleans_temporary_file(
    tmp_path: Path,
):
    """
    Arrange: the readiness marker replacement command fails after JAR installation.
    Act: run the launcher.
    Assert: the previous marker is preserved and Java is not started.
    """
    home = tmp_path / "jenkins-home"
    home.mkdir()
    ready_marker = home / ".ready"
    ready_marker.write_text("legacy-ready")

    run = _run_launcher(tmp_path, home, move_failure="ready")

    assert run.result.returncode == 1
    assert "Unable to install readiness marker" in run.result.stderr
    assert ready_marker.read_text() == "legacy-ready"
    assert not list(home.glob(".ready.*"))
    assert not run.java_called.exists()


def test_launcher_quotes_home_paths(tmp_path: Path):
    """
    Arrange: Jenkins home contains spaces and shell metacharacters.
    Act: run the launcher.
    Assert: the agent starts and writes files in the exact home path.
    """
    home = tmp_path / "home with spaces;and$chars"
    home.mkdir()

    run = _run_launcher(tmp_path, home)

    assert run.result.returncode == 0, run.result.stderr
    assert (home / "agent.jar").read_text() == "new-agent"
    assert run.java_called.exists()
