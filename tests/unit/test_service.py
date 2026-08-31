# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.
#
# Learn more about testing at: https://juju.is/docs/sdk/testing

"""Test for service interaction."""

from __future__ import annotations

import os
import pwd

# Bandit flags subprocess in tests; it is only used to build command lists for unit-test mocks.
import subprocess  # nosec: B404
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import MagicMock, PropertyMock, call

import ops.testing
import pytest
from charms.operator_libs_linux.v0 import apt
from charms.operator_libs_linux.v1 import systemd

import charm_state
import service
from charm_state import AGENT_RELATION, Credentials, State

if TYPE_CHECKING:
    from charm import JenkinsAgentCharm


def _begin_with_lazy_service(harness: ops.testing.Harness, *, run_install: bool = False) -> None:
    """Run a minimal hook so lazy charm state/service initialization occurs."""
    harness.begin()
    if run_install:
        harness.charm.on.install.emit()


def _service(harness: ops.testing.Harness) -> service.JenkinsAgentService:
    """Build the service from the charm's current desired state for direct tests."""
    return service.JenkinsAgentService(charm_state.State.from_charm(harness.charm))


def _mock_install_host(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    package_installed: bool = False,
    mock_fs_ownership: bool = True,
) -> SimpleNamespace:
    """Mock host syscalls so JenkinsAgentService.install runs off-host.

    Args:
        monkeypatch: pytest monkeypatch fixture.
        tmp_path: temporary directory the service files are written to.
        package_installed: whether the required apt package is already present.
        mock_fs_ownership: whether to mock os.chmod/os.chown. Disabling it leaves
            os.chmod untouched but still stubs os.chown, so tests can assert real
            filesystem writes without requiring root privileges to change owners.

    Returns:
        Namespace of the patched systemd entry points (daemon_reload, service_enable)
        and the tmp paths for the unit and script files.
    """
    unit_path = tmp_path / "jenkins-agent.service"
    script_path = tmp_path / "jenkins-agent"
    monkeypatch.setattr(service, "JENKINS_AGENT_SYSTEMD_PATH", unit_path)
    monkeypatch.setattr(service, "JENKINS_AGENT_START_SCRIPT_PATH", script_path)
    monkeypatch.setattr(service, "SUDOERS_DROP_IN_DIR", tmp_path / "sudoers.d")

    real_mkdir = Path.mkdir

    def fake_mkdir(path, *args, **kwargs):
        if path in {Path("/var/lib/jenkins"), Path("/srv/jenkins")}:
            return None
        return real_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", fake_mkdir)
    if mock_fs_ownership:
        monkeypatch.setattr(os, "chmod", MagicMock())
        monkeypatch.setattr(os, "chown", MagicMock())
    else:
        monkeypatch.setattr(os, "chown", MagicMock())
    daemon_reload = MagicMock()
    service_enable = MagicMock()
    monkeypatch.setattr(systemd, "service_running", MagicMock(return_value=False))
    monkeypatch.setattr(systemd, "daemon_reload", daemon_reload)
    monkeypatch.setattr(systemd, "service_enable", service_enable)
    from_installed = (
        MagicMock() if package_installed else MagicMock(side_effect=apt.PackageNotFoundError)
    )
    monkeypatch.setattr(apt.DebianPackage, "from_installed_package", from_installed)
    monkeypatch.setattr(apt, "add_package", MagicMock())
    _make_fake_useradd(monkeypatch)
    return SimpleNamespace(
        daemon_reload=daemon_reload,
        service_enable=service_enable,
        unit_path=unit_path,
        script_path=script_path,
    )


@pytest.mark.parametrize(
    "f,error_thrown",
    [
        ("add_package", apt.PackageError),
        ("add_package", apt.PackageNotFoundError),
    ],
)
def test_install_apt_package_gpg_key_error(
    harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, f, error_thrown
):
    """
    arrange: Harness with mocked apt module.
    act: run _on_install hook with methods raising different errors.
    assert: The charm should be in an error state.
    """
    _mock_install_host(monkeypatch, tmp_path)
    _begin_with_lazy_service(harness, run_install=True)
    charm: JenkinsAgentCharm = harness.charm
    monkeypatch.setattr(apt, "add_package", MagicMock())
    monkeypatch.setattr(apt, f, MagicMock(side_effect=[error_thrown]))

    with pytest.raises(RuntimeError, match=r"Error installing the agent service"):
        charm.on.install.emit()


def test_on_install(harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """
    arrange: Harness with mocked apt module.
    act: run initial hook.
    assert: The installation should pass without error and charm in blocked state.
    """
    apt_add_package_mock = MagicMock()
    host = _mock_install_host(monkeypatch, tmp_path)
    monkeypatch.setattr(apt, "add_package", apt_add_package_mock)

    harness.begin_with_initial_hooks()

    # The package is absent (mock always raises), so every reconcile installs it
    # with the expected package list.
    assert apt_add_package_mock.call_count >= 1
    assert apt_add_package_mock.call_args_list[0][0][0] == ["openjdk-21-jre", "sudo"]
    # The unit file is newly written, so the service is reloaded and enabled for
    # automatic start on reboot.
    assert host.service_enable.call_count >= 1

    assert harness.charm.unit.status.name == ops.BlockedStatus.name


def test_install_skips_apt_when_package_present(
    harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """
    arrange: Harness where the required apt package is already installed.
    act: run the install hook to trigger reconcile.
    assert: apt.add_package is not called (the expensive step is gated on presence).
    """
    apt_add_package_mock = MagicMock()
    _mock_install_host(monkeypatch, tmp_path, package_installed=True)
    monkeypatch.setattr(apt, "add_package", apt_add_package_mock)

    _begin_with_lazy_service(harness, run_install=True)
    harness.charm.on.install.emit()

    assert apt_add_package_mock.call_count == 0


def test_install_skips_reload_when_unit_unchanged(
    harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """
    arrange: Harness where the unit file on disk already matches the template.
    act: run the install hook twice to trigger reconcile.
    assert: the second run does not reload or re-enable the unchanged unit file.
    """
    host = _mock_install_host(monkeypatch, tmp_path, package_installed=True)
    _begin_with_lazy_service(harness, run_install=True)

    harness.charm.on.install.emit()
    first_enable_count = host.service_enable.call_count
    harness.charm.on.install.emit()

    assert first_enable_count == 1
    # Second reconcile sees identical files on disk, so no reload/enable happens.
    assert host.service_enable.call_count == 1


def test_install_enable_error(
    harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """
    arrange: Harness where enabling the systemd unit fails.
    act: run the install hook to trigger reconcile.
    assert: reconcile raises RuntimeError surfacing the enable failure.
    """
    host = _mock_install_host(monkeypatch, tmp_path)
    host.service_enable.side_effect = systemd.SystemdError("boom")
    _begin_with_lazy_service(harness)

    with pytest.raises(RuntimeError, match=r"Error installing the agent service"):
        harness.charm.on.install.emit()


def test_install_renders_user_and_workdir(
    harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """
    arrange: Harness with patched install paths and templates.
    act: run the install hook.
    assert: the systemd unit contains User, Group and WorkingDirectory derived from
        charm config defaults.
    """
    host = _mock_install_host(monkeypatch, tmp_path)
    apt_add_package_mock = MagicMock()
    monkeypatch.setattr(apt, "add_package", apt_add_package_mock)

    harness.begin_with_initial_hooks()
    unit_text = host.unit_path.read_text()

    assert "User=jenkins" in unit_text
    assert "Group=" not in unit_text
    assert "WorkingDirectory=/var/lib/jenkins" in unit_text
    assert 'Environment="JENKINS_HOME=/var/lib/jenkins"' in unit_text
    assert 'Environment="HOME=/var/lib/jenkins"' in unit_text
    assert 'Environment="USER=jenkins"' in unit_text


def test_install_renders_custom_user_and_workdir(
    harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """
    arrange: Harness configured with agent_user=jenkins and jenkins_home=/srv/jenkins.
    act: run the install hook.
    assert: the systemd unit uses the configured user, group and working directory.
    """
    host = _mock_install_host(monkeypatch, tmp_path)
    apt_add_package_mock = MagicMock()
    monkeypatch.setattr(apt, "add_package", apt_add_package_mock)

    harness.update_config({"agent_user": "jenkins", "jenkins_home": "/srv/jenkins"})
    harness.begin_with_initial_hooks()
    unit_text = host.unit_path.read_text()

    assert "User=jenkins" in unit_text
    assert "Group=" not in unit_text
    assert "WorkingDirectory=/srv/jenkins" in unit_text
    assert 'Environment="JENKINS_HOME=/srv/jenkins"' in unit_text
    assert "ExecStopPost=rm -rf /srv/jenkins/.ready" in unit_text


def _write_executable(path: Path, content: str) -> None:
    """Write an executable test command."""
    path.write_text(content)
    path.chmod(0o755)


def _run_test_launcher(
    tmp_path: Path,
    home: Path,
    *,
    reject_direct_download: bool = False,
    fail_download: bool = False,
    fail_move: bool = False,
    require_writable_runtime_dirs: bool = False,
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
printf '%s\n' "$output" > "$FAKE_CURL_OUTPUT"
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
if [ "${FAKE_JAVA_REQUIRE_RUNTIME_DIRS:-false}" = true ]; then
    for directory in remoting workspace; do
        [ -d "$JENKINS_HOME/$directory" ]
        [ -r "$JENKINS_HOME/$directory" ]
        [ -w "$JENKINS_HOME/$directory" ]
        [ -x "$JENKINS_HOME/$directory" ]
    done
fi
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
        State,
        SimpleNamespace(agent_user="jenkins", jenkins_home=home, websocket_mode=True),
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
        "FAKE_JAVA_REQUIRE_RUNTIME_DIRS": str(require_writable_runtime_dirs).lower(),
    }
    result = subprocess.run(  # nosec: B603 - launcher path is created by this test
        [str(launcher)], capture_output=True, text=True, env=env, check=False
    )
    return SimpleNamespace(
        result=result,
        curl_output=curl_output,
        java_called=java_called,
    )


def test_launcher_replaces_unwritable_legacy_agent_jar(tmp_path: Path):
    """Download beside agent.jar before replacing a legacy unwritable file."""
    home = tmp_path / "jenkins-home"
    home.mkdir()
    agent_jar = home / "agent.jar"
    agent_jar.write_text("legacy-agent")
    agent_jar.chmod(0o444)

    run = _run_test_launcher(tmp_path, home, reject_direct_download=True)

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

    run = _run_test_launcher(tmp_path, home, fail_download=True)

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

    run = _run_test_launcher(tmp_path, home)

    assert run.result.returncode == 0, run.result.stderr
    assert not agent_jar.is_symlink()
    assert agent_jar.is_file()
    assert agent_jar.read_text() == "new-agent"
    assert sentinel.read_text() == "unchanged"
    assert list(outside.iterdir()) == [sentinel]


def test_launcher_reports_replace_failure_and_cleans_temporary_jar(tmp_path: Path):
    """A failed atomic replacement is clear and leaves no partial download."""
    home = tmp_path / "jenkins-home"
    home.mkdir()
    agent_jar = home / "agent.jar"
    agent_jar.write_text("legacy-agent")

    run = _run_test_launcher(tmp_path, home, fail_move=True)

    assert run.result.returncode == 1
    assert "Unable to install agent binary" in run.result.stderr
    assert agent_jar.read_text() == "legacy-agent"
    assert not list(home.glob(".agent.jar.*"))
    assert not run.java_called.exists()


def test_launcher_archives_unsafe_legacy_runtime_directories(tmp_path: Path):
    """Preserve root-era state without following runtime-directory symlinks."""
    if os.geteuid() == 0:
        pytest.skip("directory write checks do not model the service user when run as root")
    home = tmp_path / "jenkins-home"
    home.mkdir()
    outside = tmp_path / "outside-remoting"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_text("unchanged")
    (home / "remoting").symlink_to(outside, target_is_directory=True)
    legacy_workspace = home / "workspace"
    legacy_workspace.mkdir()
    (legacy_workspace / "legacy-state").write_text("workspace")
    legacy_workspace.chmod(0o555)

    run = _run_test_launcher(tmp_path, home, require_writable_runtime_dirs=True)

    assert run.result.returncode == 0, run.result.stderr
    for name in ("remoting", "workspace"):
        replacement = home / name
        assert replacement.is_dir()
        assert not replacement.is_symlink()
        assert os.access(replacement, os.R_OK | os.W_OK | os.X_OK)
    archived_remoting = home / ".jenkins-agent-legacy-remoting"
    assert archived_remoting.is_symlink()
    assert archived_remoting.resolve() == outside
    assert sentinel.read_text() == "unchanged"
    assert list(outside.iterdir()) == [sentinel]
    archived_workspace = home / ".jenkins-agent-legacy-workspace"
    assert (archived_workspace / "legacy-state").read_text() == "workspace"
    assert run.java_called.exists()


def test_install_renders_script_with_jenkins_home(
    harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """
    arrange: Harness with patched install paths and templates.
    act: run the install hook.
    assert: the launcher script references the configured JENKINS_HOME.
    """
    host = _mock_install_host(monkeypatch, tmp_path)
    apt_add_package_mock = MagicMock()
    monkeypatch.setattr(apt, "add_package", apt_add_package_mock)

    harness.update_config({"jenkins_home": "/srv/jenkins"})
    harness.begin_with_initial_hooks()
    script_text = Path(host.script_path).read_text()

    assert 'JENKINS_HOME="${JENKINS_HOME:-/srv/jenkins}"' in script_text


def test_install_renders_script_with_default_jenkins_home(
    harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """
    arrange: Harness without any jenkins_home override.
    act: run the install hook.
    assert: the launcher script references the default JENKINS_HOME.
    """
    host = _mock_install_host(monkeypatch, tmp_path)
    apt_add_package_mock = MagicMock()
    monkeypatch.setattr(apt, "add_package", apt_add_package_mock)

    harness.begin_with_initial_hooks()
    script_text = Path(host.script_path).read_text()

    assert 'JENKINS_HOME="${JENKINS_HOME:-/var/lib/jenkins}"' in script_text


def test_install_creates_user_and_home(
    harness: ops.testing.Harness,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    """
    arrange: Harness with agent_user=jenkins and jenkins_home pointing inside tmp_path.
    act: run the install hook.
    assert: the home directory is created and the charm calls os.chown using the
        created jenkins uid/gid.
    """
    home = tmp_path / "jenkins-home"
    _mock_install_host(monkeypatch, tmp_path)
    monkeypatch.setattr(apt, "add_package", MagicMock())
    _make_fake_useradd(monkeypatch)
    chown_mock = MagicMock()
    monkeypatch.setattr(os, "chown", chown_mock)

    harness.update_config({"agent_user": "jenkins", "jenkins_home": str(home)})
    harness.begin_with_initial_hooks()

    assert home.exists()
    jenkins_uid = pwd.getpwnam("jenkins").pw_uid
    jenkins_gid = pwd.getpwnam("jenkins").pw_gid
    chown_mock.assert_any_call(home, uid=jenkins_uid, gid=jenkins_gid)


def test_install_fails_on_useradd_failure(
    harness: ops.testing.Harness,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    """
    arrange: Harness with agent_user=nonexistent-testuser and useradd mocked to fail.
    act: run the install hook.
    assert: the charm reports an installation error.
    """
    username = "nonexistent-testuser"
    home = tmp_path / f"{username}-home"
    _mock_install_host(monkeypatch, tmp_path)
    monkeypatch.setattr(apt, "add_package", MagicMock())
    monkeypatch.setattr(
        subprocess, "run", MagicMock(side_effect=subprocess.CalledProcessError(1, ["useradd"]))
    )

    harness.update_config({"agent_user": username, "jenkins_home": str(home)})
    with pytest.raises(RuntimeError, match=r"Error installing the agent service"):
        harness.begin_with_initial_hooks()


def _make_fake_useradd(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provide an in-memory useradd that creates entries seen by pwd.getpwnam."""
    user_db: dict[str, pwd.struct_passwd] = {}

    def fake_run(args, **_kwargs):
        if args and args[0].endswith("visudo"):
            return subprocess.CompletedProcess(args, 0, "", "")
        # Validate expected useradd shape:
        # /usr/sbin/useradd --home-dir HOME --create-home --shell /bin/bash USER
        if len(args) < 7 or not args[0].endswith("useradd"):
            raise subprocess.CalledProcessError(1, args)
        if "--system" in args:
            raise subprocess.CalledProcessError(1, args)
        if "--create-home" not in args or "--shell" not in args or "/bin/bash" not in args:
            raise subprocess.CalledProcessError(1, args)
        username = args[-1]
        try:
            home = args[args.index("--home-dir") + 1]
        except (ValueError, IndexError) as exc:
            raise subprocess.CalledProcessError(1, args) from exc
        if username in user_db:
            raise subprocess.CalledProcessError(9, args)
        # Pick deterministic fake uid/gid based on username hash to avoid collisions.
        uid = 50000 + hash(username) % 10000
        gid = uid
        user_db[username] = pwd.struct_passwd((username, "x", uid, gid, "", home, "/bin/bash"))
        return subprocess.CompletedProcess(args, 0, "", "")

    real_getpwnam = pwd.getpwnam

    def fake_getpwnam(username: str):
        if username in user_db:
            return user_db[username]
        return real_getpwnam(username)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(pwd, "getpwnam", fake_getpwnam)


def test_render_file_uses_configured_owner(
    harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """
    arrange: Harness with agent_user=jenkins and patched install paths.
    act: run the install hook.
    assert: the systemd unit and launcher script are chowned to root; the home dir
        is chowned to jenkins.
    """
    home = tmp_path / "jenkins-home"
    host = _mock_install_host(monkeypatch, tmp_path)
    monkeypatch.setattr(apt, "add_package", MagicMock())
    _make_fake_useradd(monkeypatch)
    chown_mock = MagicMock()
    monkeypatch.setattr(os, "chown", chown_mock)

    harness.update_config({"agent_user": "jenkins", "jenkins_home": str(home)})
    harness.begin_with_initial_hooks()

    jenkins_uid = pwd.getpwnam("jenkins").pw_uid
    jenkins_gid = pwd.getpwnam("jenkins").pw_gid
    assert call(host.unit_path, uid=0, gid=0) in chown_mock.call_args_list
    assert call(host.script_path, uid=0, gid=0) in chown_mock.call_args_list
    assert call(home, uid=jenkins_uid, gid=jenkins_gid) in chown_mock.call_args_list


def test_ensure_user_does_not_chown_existing_home_contents(
    harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """
    arrange: Harness with agent_user=jenkins, jenkins_home pre-existing with a file.
    act: run the install hook.
    assert: only the home directory is chowned to jenkins.
    """
    home = tmp_path / "jenkins-home"
    home.mkdir(parents=True)
    existing = home / "existing.txt"
    existing.write_text("old")
    _mock_install_host(monkeypatch, tmp_path)
    monkeypatch.setattr(apt, "add_package", MagicMock())
    _make_fake_useradd(monkeypatch)
    chown_mock = MagicMock()
    monkeypatch.setattr(os, "chown", chown_mock)

    harness.update_config({"agent_user": "jenkins", "jenkins_home": str(home)})
    harness.begin_with_initial_hooks()

    jenkins_uid = pwd.getpwnam("jenkins").pw_uid
    jenkins_gid = pwd.getpwnam("jenkins").pw_gid
    assert call(existing, uid=jenkins_uid, gid=jenkins_gid) not in chown_mock.call_args_list
    assert call(home, uid=jenkins_uid, gid=jenkins_gid) in chown_mock.call_args_list


def test_install_grants_passwordless_sudo(
    harness: ops.testing.Harness,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    """Write a validated passwordless sudo rule for the configured agent user."""
    home = tmp_path / "jenkins-home"
    sudoers_d = tmp_path / "sudoers.d"
    _mock_install_host(monkeypatch, tmp_path)
    monkeypatch.setattr(os, "chmod", MagicMock())
    monkeypatch.setattr(service, "SUDOERS_DROP_IN_DIR", sudoers_d)

    harness.update_config({"agent_user": "jenkins", "jenkins_home": str(home)})
    harness.begin_with_initial_hooks()

    drop_in_path = sudoers_d / "99-jenkins-agent"
    assert drop_in_path.read_text() == "jenkins ALL=(ALL:ALL) NOPASSWD: ALL\n"


def test_restart_service(
    harness: ops.testing.Harness,
    monkeypatch: pytest.MonkeyPatch,
    agent_relation_data: dict,
    service_configuration_template: str,
):
    """
    arrange: Harness with mocked systemd and fs-related methods.
    act: add relation with jenkins-k8s with mock relation data and restart the agent service.
    assert: Configuration file content should match the relation data and
    restart should not raise any error and the charm should be in active state.
    """
    pathlib_write_text_mock = MagicMock()
    monkeypatch.setattr(Path, "write_text", pathlib_write_text_mock)
    monkeypatch.setattr(Path, "mkdir", MagicMock)
    monkeypatch.setattr(os, "chmod", MagicMock)
    monkeypatch.setattr(os, "chown", MagicMock)
    monkeypatch.setattr(systemd, "daemon_reload", MagicMock)
    monkeypatch.setattr(systemd, "service_restart", MagicMock)
    monkeypatch.setattr(systemd, "service_running", MagicMock(return_value=True))
    monkeypatch.setattr(
        service.JenkinsAgentService, "configuration_changed", MagicMock(return_value=False)
    )
    monkeypatch.setattr(
        service.JenkinsAgentService, "_startup_check", MagicMock(return_value=True)
    )
    # The reconcile handler also runs install(); it is exercised separately, so
    # stub it here to keep this test focused on the restart/config-render path.
    monkeypatch.setattr(service.JenkinsAgentService, "install", MagicMock())

    harness.add_relation(AGENT_RELATION, "jenkins-k8s", unit_data=agent_relation_data)
    _begin_with_lazy_service(harness, run_install=True)
    charm: JenkinsAgentCharm = harness.charm
    charm.on.config_changed.emit()

    assert pathlib_write_text_mock.call_args[0][0] == service_configuration_template
    assert charm.unit.status.name == ops.ActiveStatus.name


def test_restart_template_preserves_systemd_values(harness: ops.testing.Harness):
    """Systemd templates must not HTML-escape credentials or URLs."""
    _begin_with_lazy_service(harness)
    template = _service(harness)._template_loader.get_template("jenkins_agent_env.conf.j2")

    rendered = template.render(
        environments={"JENKINS_TOKEN": 'a&b<c>"d', "JENKINS_URL": "https://jenkins.test"}
    )

    assert 'Environment="JENKINS_TOKEN=a&b<c>\\"d"' in rendered
    with pytest.raises(ValueError, match=r"control characters"):
        template.render(environments={"JENKINS_TOKEN": "bad\nvalue"})


def test_restart_service_write_config_type_error(
    harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch, agent_relation_data: dict
):
    """
    arrange: Harness with mocked fs-related methods raising an error.
    act: restart the agent service.
    assert: The charm should raise ServiceRestartError.
    """
    monkeypatch.setattr(Path, "write_text", MagicMock(side_effect=TypeError))
    monkeypatch.setattr(Path, "mkdir", MagicMock)
    harness.add_relation(AGENT_RELATION, "jenkins-k8s", unit_data=agent_relation_data)
    _begin_with_lazy_service(harness)

    with pytest.raises(
        service.ServiceRestartError,
        match=r"Error interacting with the filesystem when rendering configuration file",
    ):
        _service(harness).restart()


def test_restart_service_systemd_error(
    harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch, agent_relation_data: dict
):
    """
    arrange: Harness with mocked systemd methods raising an error.
    act: restart the agent service.
    assert: The charm should raise ServiceRestartError.
    """
    systemd_error_message = "Mock systemd error"
    monkeypatch.setattr(service.JenkinsAgentService, "_render_file", MagicMock)
    monkeypatch.setattr(Path, "mkdir", MagicMock)
    monkeypatch.setattr(
        systemd,
        "daemon_reload",
        MagicMock(side_effect=systemd.SystemdError(systemd_error_message)),
    )
    harness.add_relation(AGENT_RELATION, "jenkins-k8s", unit_data=agent_relation_data)
    _begin_with_lazy_service(harness)

    with pytest.raises(
        service.ServiceRestartError,
        match=rf"Error starting the agent service:\n{systemd_error_message}",
    ):
        _service(harness).restart()


def test_service_is_ready_systemd_error(
    harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch
):
    """
    arrange: Harness with mocked fs-related methods raising an error.
    act: Check if the service is running.
    assert: The call should return false and not raising any exceptions.
    """
    _begin_with_lazy_service(harness)
    # Mock Path.exists to return True for AGENT_READY_PATH so we exercise the
    # systemd.service_running path.
    real_exists = Path.exists

    def _mock_exists(self):
        if str(self) == str(service.AGENT_READY_PATH):
            return True
        return real_exists(self)

    monkeypatch.setattr(Path, "exists", _mock_exists)
    monkeypatch.setattr(systemd, "service_running", MagicMock(side_effect=SystemError))

    with pytest.raises(RuntimeError, match="Failed to query the agent service"):
        assert _service(harness).is_ready


def test_parse_systemd_env():
    """
    arrange: A systemd override.conf with Environment directives.
    act: Parse the content.
    assert: The correct key-value pairs are extracted.
    """
    content = (
        "[Service]\n"
        'Environment="JENKINS_TOKEN=abc123"\n'  # nosec: B105
        'Environment="JENKINS_URL=http://10.1.69.130:8080"\n'
        'Environment="JENKINS_AGENT=jenkins-agent-k8s-0"'
    )
    result = service._parse_systemd_env(content)
    assert result == {
        "JENKINS_TOKEN": "abc123",
        "JENKINS_URL": "http://10.1.69.130:8080",
        "JENKINS_AGENT": "jenkins-agent-k8s-0",
    }


def test_parse_systemd_env_unquotes_values():
    """Parse the escaping emitted by the systemd environment template."""
    content = '[Service]\nEnvironment="JENKINS_TOKEN=a%%\\\\b\\"c"'

    assert service._parse_systemd_env(content)["JENKINS_TOKEN"] == 'a%\\b"c'


@pytest.mark.parametrize(
    "override_content,cred_url,cred_secret,expected",
    [
        pytest.param(None, "http://new", "s", True, id="no_override_file"),
        pytest.param(
            "[Service]\n"
            'Environment="JENKINS_TOKEN=secret123"\n'
            'Environment="JENKINS_URL=http://10.1.69.130:8080"\n'
            'Environment="JENKINS_AGENT=test-model-jenkins-agent-0"',
            "http://10.1.69.130:8080",
            "secret123",
            False,
            id="same_credentials",
        ),
        pytest.param(
            "[Service]\n"
            'Environment="JENKINS_TOKEN=secret123"\n'
            'Environment="JENKINS_URL=http://10.1.69.130:8080"\n'
            'Environment="JENKINS_AGENT=test-model-jenkins-agent-0"',
            "http://10.1.69.153:8080",
            "secret123",
            True,
            id="url_differs",
        ),
        pytest.param(
            '[Service]\nEnvironment="JENKINS_TOKEN=a%%\\\\b\\"c"\n'
            'Environment="JENKINS_URL=http://10.1.69.130:8080"',
            "http://10.1.69.130:8080",
            'a%\\b"c',
            False,
            id="escaped_credentials",
        ),
    ],
)
def test_credentials_changed(
    harness: ops.testing.Harness,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    override_content: str | None,
    cred_url: str,
    cred_secret: str,
    expected: bool,
):
    """
    arrange: A service with or without an override.conf file.
    act: Check if credentials changed against given values.
    assert: Returns expected bool depending on file state and content.
    """
    config_dir = tmp_path / "override"
    if override_content is not None:
        config_dir.mkdir()
        (config_dir / "override.conf").write_text(override_content)
    monkeypatch.setattr(service, "SYSTEMD_SERVICE_CONF_DIR", str(config_dir))
    harness.add_relation(
        AGENT_RELATION,
        "jenkins-k8s",
        unit_data={"url": cred_url, "test-model-jenkins-agent-0_secret": cred_secret},
    )
    _begin_with_lazy_service(harness)
    result = _service(harness).credentials_changed(
        Credentials(address=cred_url, secret=cred_secret)
    )
    assert result is expected


def test_restart_missing_credentials(harness: ops.testing.Harness):
    """
    arrange: a service whose state has no agent relation credentials.
    act: call restart directly.
    assert: ServiceRestartError is raised for the missing configuration.
    """
    # The default harness has no agent relation, so credentials resolve to None.
    _begin_with_lazy_service(harness)

    with pytest.raises(service.ServiceRestartError, match=r"missing configuration"):
        _service(harness).restart()


def test_restart_startup_check_timeout(
    harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch, agent_relation_data: dict
):
    """
    arrange: a related service whose agent never becomes active before timeout.
    act: call restart directly.
    assert: ServiceRestartError is raised because the startup check times out.
    """
    monkeypatch.setattr(service.JenkinsAgentService, "_render_file", MagicMock())
    monkeypatch.setattr(Path, "mkdir", MagicMock())
    monkeypatch.setattr(systemd, "daemon_reload", MagicMock())
    monkeypatch.setattr(systemd, "service_restart", MagicMock())
    monkeypatch.setattr(service.JenkinsAgentService, "is_ready", PropertyMock(return_value=False))
    # Collapse the polling loop so the test does not actually sleep.
    monkeypatch.setattr(service, "STARTUP_CHECK_TIMEOUT", 0)
    monkeypatch.setattr(service.time, "sleep", MagicMock())
    harness.add_relation(AGENT_RELATION, "jenkins-k8s", unit_data=agent_relation_data)
    _begin_with_lazy_service(harness)

    with pytest.raises(
        service.ServiceRestartError, match=r"waiting for the agent service to start"
    ):
        _service(harness).restart()


def test_startup_check_becomes_active(
    harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch
):
    """
    arrange: a service that reports active on the first poll.
    act: call the startup check.
    assert: it returns True after breaking out of the poll loop.
    """
    monkeypatch.setattr(service.JenkinsAgentService, "is_ready", PropertyMock(return_value=True))
    monkeypatch.setattr(service.time, "sleep", MagicMock())
    _begin_with_lazy_service(harness)

    assert _service(harness)._startup_check() is True


def test_reset_failed_state_logs_on_error(
    harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch
):
    """
    arrange: a service whose systemctl reset-failed raises a SystemdError.
    act: call reset_failed_state.
    assert: the error is swallowed (logged, not raised) as it is non-critical.
    """
    monkeypatch.setattr(systemd, "_systemctl", MagicMock(side_effect=systemd.SystemdError))
    _begin_with_lazy_service(harness)

    # Should not raise.
    _service(harness).reset_failed_state()


def test_reset_stops_and_clears_config(
    harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """
    arrange: a related service with an existing override.conf file.
    act: call reset.
    assert: the service is stopped and the override.conf file is removed.
    """
    config_dir = tmp_path / "override"
    config_dir.mkdir()
    override = config_dir / "override.conf"
    override.write_text("[Service]")
    monkeypatch.setattr(service, "SYSTEMD_SERVICE_CONF_DIR", str(config_dir))
    stop_mock = MagicMock()
    monkeypatch.setattr(systemd, "service_stop", stop_mock)
    _begin_with_lazy_service(harness)

    _service(harness).reset()

    assert stop_mock.call_count == 1
    assert not override.exists()


def test_reset_stop_error(harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch):
    """
    arrange: a service whose systemctl stop raises a SystemdError.
    act: call reset.
    assert: ServiceStopError is raised.
    """
    monkeypatch.setattr(systemd, "service_stop", MagicMock(side_effect=systemd.SystemdError))
    _begin_with_lazy_service(harness)

    with pytest.raises(service.ServiceStopError, match=r"failed to stop"):
        _service(harness).reset()


def test_sync_service_files_read_error(
    harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """
    arrange: a service whose existing on-disk unit file cannot be read.
    act: run install to trigger the file sync.
    assert: FileRenderError is raised when the destination file read fails.
    """
    host = _mock_install_host(monkeypatch, tmp_path)
    unit_path = host.unit_path
    unit_path.write_text("stale")

    real_read_text = Path.read_text

    def _read_text(self, *args, **kwargs):
        # Fail only when reading the existing on-disk unit file; template reads
        # (relative paths under templates/) must still succeed.
        if self == unit_path:
            raise OSError("disk gone")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _read_text)
    _begin_with_lazy_service(harness)

    with pytest.raises(service.FileRenderError, match=r"Error reading"):
        _service(harness).install()


def test_install_reports_service_file_changes(
    harness: ops.testing.Harness,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    """Return whether reconciliation changed files that require a restart."""
    _mock_install_host(monkeypatch, tmp_path, package_installed=True)
    _begin_with_lazy_service(harness)
    sync_mock = MagicMock(side_effect=[True, False])
    monkeypatch.setattr(_service(harness), "_sync_service_files", sync_mock)

    assert _service(harness).install() is True
    assert _service(harness).install() is False


def test_is_running_queries_systemd(harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch):
    """is_running reports the systemd service state independently of readiness."""
    _begin_with_lazy_service(harness)
    monkeypatch.setattr(systemd, "service_running", MagicMock(return_value=True))
    assert _service(harness).is_running


def test_is_running_systemd_error_raises(
    harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch
):
    """A systemd query failure is retryable rather than silently inactive."""
    _begin_with_lazy_service(harness)
    monkeypatch.setattr(systemd, "service_running", MagicMock(side_effect=SystemError))
    with pytest.raises(RuntimeError, match="Failed to query the agent service"):
        _ = _service(harness).is_running


def test_configuration_changed_compares_desired_override(
    harness: ops.testing.Harness,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    agent_relation_data: dict,
):
    """Compare desired credentials/agent/home with the actual override file."""
    harness.add_relation(AGENT_RELATION, "jenkins-k8s", unit_data=agent_relation_data)
    _begin_with_lazy_service(harness)
    service_instance = _service(harness)
    override_dir = tmp_path / "override"
    override_dir.mkdir()
    monkeypatch.setattr(service, "SYSTEMD_SERVICE_CONF_DIR", str(override_dir))
    monkeypatch.setattr(service_instance, "service_files_changed", MagicMock(return_value=False))
    (override_dir / "override.conf").write_text(
        "[Service]\n"
        f'Environment="JENKINS_TOKEN={agent_relation_data["test-model-jenkins-agent-0_secret"]}"\n'
        f'Environment="JENKINS_URL={agent_relation_data["url"]}"\n'
        'Environment="JENKINS_AGENT=test-model-jenkins-agent-0"\n'
        'Environment="JENKINS_HOME=/var/lib/jenkins"'
    )
    credentials = Credentials(
        address=agent_relation_data["url"],
        secret=agent_relation_data["test-model-jenkins-agent-0_secret"],
    )

    assert not service_instance.configuration_changed(credentials)
    assert not service_instance.configuration_changed()
    (override_dir / "override.conf").unlink()
    assert service_instance.configuration_changed(credentials)
    (override_dir / "override.conf").write_text(
        "[Service]\n"
        f'Environment="JENKINS_TOKEN={agent_relation_data["test-model-jenkins-agent-0_secret"]}"\n'
        f'Environment="JENKINS_URL={agent_relation_data["url"]}"\n'
        'Environment="JENKINS_AGENT=test-model-jenkins-agent-0"\n'
        'Environment="JENKINS_HOME=/srv/jenkins"'
    )
    assert service_instance.configuration_changed(credentials)


def test_service_files_changed_compares_rendered_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """Detect drift in rendered service files before writing them."""
    state = cast(
        State,
        SimpleNamespace(
            agent_user="root", jenkins_home=Path("/var/lib/jenkins"), websocket_mode=True
        ),
    )
    service_instance = service.JenkinsAgentService(state)
    unit_path = tmp_path / "jenkins-agent.service"
    script_path = tmp_path / "jenkins-agent"
    monkeypatch.setattr(service, "JENKINS_AGENT_SYSTEMD_PATH", unit_path)
    monkeypatch.setattr(service, "JENKINS_AGENT_START_SCRIPT_PATH", script_path)
    monkeypatch.setattr(
        service_instance, "_render_service_files", MagicMock(return_value=("unit", "script"))
    )
    unit_path.write_text("unit")
    script_path.write_text("script")

    assert not service_instance.service_files_changed()
    script_path.write_text("changed")
    assert service_instance.service_files_changed()


def test_service_files_changed_read_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Surface an error reading an existing service file."""
    state = cast(
        State,
        SimpleNamespace(
            agent_user="root", jenkins_home=Path("/var/lib/jenkins"), websocket_mode=True
        ),
    )
    service_instance = service.JenkinsAgentService(state)
    unit_path = tmp_path / "jenkins-agent.service"
    script_path = tmp_path / "jenkins-agent"
    monkeypatch.setattr(service, "JENKINS_AGENT_SYSTEMD_PATH", unit_path)
    monkeypatch.setattr(service, "JENKINS_AGENT_START_SCRIPT_PATH", script_path)
    unit_path.write_text("unit")
    monkeypatch.setattr(
        service_instance, "_render_service_files", MagicMock(return_value=("unit", "script"))
    )
    monkeypatch.setattr(Path, "read_text", MagicMock(side_effect=OSError("read failed")))

    with pytest.raises(service.FileRenderError, match="read failed"):
        service_instance.service_files_changed()
