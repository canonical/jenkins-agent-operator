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
    monkeypatch.setattr(
        service.JenkinsAgentService, "runtime_directories_usable", MagicMock(return_value=True)
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


def test_runtime_directories_usable_accepts_missing_entries(tmp_path: Path):
    """
    Arrange: the configured Jenkins home has no runtime directories.
    Act: check runtime-directory usability.
    Assert: missing entries are accepted for launcher creation.
    """
    home = tmp_path / "jenkins-home"
    home.mkdir()

    assert _runtime_service(home).runtime_directories_usable()


def test_runtime_directories_usable_rejects_wrong_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """
    Arrange: a runtime directory has the service UID but a different GID.
    Act: check runtime-directory usability.
    Assert: reconciliation blocks until ownership migration repairs the group.
    """
    home = tmp_path / "jenkins-home"
    home.mkdir()
    remoting = home / "remoting"
    remoting.mkdir(mode=0o750)
    service_instance = _runtime_service(home)
    user_info = service_instance._agent_user_info()
    monkeypatch.setattr(
        service_instance,
        "_agent_user_info",
        MagicMock(
            return_value=SimpleNamespace(
                pw_uid=user_info.pw_uid,
                pw_gid=user_info.pw_gid + 1,
            ),
        ),
    )

    assert not service_instance.runtime_directories_usable()


def test_runtime_directories_usable_rejects_inaccessible_entry(tmp_path: Path):
    """
    Arrange: a runtime directory lacks owner write and search access.
    Act: check runtime-directory usability.
    Assert: reconciliation reports that the ownership action is required.
    """
    home = tmp_path / "jenkins-home"
    home.mkdir()
    (home / "remoting").mkdir(mode=0o500)

    assert not _runtime_service(home).runtime_directories_usable()


def test_runtime_directories_usable_rejects_non_directory_entry(tmp_path: Path):
    """
    Arrange: a known runtime entry is a regular file.
    Act: check runtime-directory usability.
    Assert: reconciliation reports that the ownership action is required.
    """
    home = tmp_path / "jenkins-home"
    home.mkdir()
    (home / "workspace").write_text("stale")

    assert not _runtime_service(home).runtime_directories_usable()


def test_runtime_directories_usable_rejects_unknown_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """
    Arrange: resolving the configured service user fails.
    Act: check runtime-directory usability.
    Assert: reconciliation reports that the ownership action is required.
    """
    home = tmp_path / "jenkins-home"
    home.mkdir()
    service_instance = _runtime_service(home)
    monkeypatch.setattr(
        service_instance, "_agent_user_info", MagicMock(side_effect=service.RuntimeDirectoryError)
    )

    assert not service_instance.runtime_directories_usable()


def test_migrate_directory_rejects_hard_linked_regular_file(tmp_path: Path):
    """
    Arrange: a runtime tree contains a regular file linked outside the tree.
    Act: migrate the runtime tree.
    Assert: migration rejects the shared inode before changing its permissions.
    """
    home = tmp_path / "jenkins-home"
    home.mkdir()
    runtime = home / "workspace"
    runtime.mkdir(mode=0o750)
    outside = tmp_path / "outside-state"
    outside.write_text("legacy")
    outside.chmod(0o400)
    linked = runtime / "state"
    os.link(outside, linked)

    with pytest.raises(service.RuntimeDirectoryError, match="hard-linked file"):
        _runtime_service(home).migrate_directory(runtime)

    assert linked.stat().st_ino == outside.stat().st_ino
    assert outside.stat().st_mode & 0o777 == 0o400


def test_migrate_runtime_directories_updates_owner_permissions_without_replacing_data(
    tmp_path: Path,
):
    """
    Arrange: runtime trees contain data and lack the service user's owner permissions.
    Act: migrate the legacy runtime directories in place.
    Assert: data and paths are preserved while owner read/write/search access is restored.
    """
    home = tmp_path / "jenkins-home"
    home.mkdir()
    username = pwd.getpwuid(os.geteuid()).pw_name
    state = cast(
        State,
        SimpleNamespace(agent_user=username, jenkins_home=home, websocket_mode=True),
    )
    service_instance = service.JenkinsAgentService(state)
    files = {}
    for name in ("remoting", "workspace"):
        runtime = home / name
        nested = runtime / "nested"
        nested.mkdir(parents=True)
        data = nested / "state"
        data.write_text(name)
        data.chmod(0o400)
        nested.chmod(0o500)
        runtime.chmod(0o500)
        files[name] = data

    service_instance.migrate_directory(home)

    for name, data in files.items():
        runtime = home / name
        assert data.read_text() == name
        assert runtime.stat().st_mode & 0o700 == 0o700
        assert data.parent.stat().st_mode & 0o700 == 0o700
        assert data.stat().st_mode & 0o600 == 0o600


def test_migrate_runtime_directories_rejects_top_level_symlink(tmp_path: Path):
    """
    Arrange: a runtime directory entry is a symlink.
    Act: migrate the legacy runtime directories.
    Assert: migration fails without following the symlink.
    """
    home = tmp_path / "jenkins-home"
    home.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (home / "workspace").symlink_to(outside, target_is_directory=True)
    username = pwd.getpwuid(os.geteuid()).pw_name
    state = cast(
        State,
        SimpleNamespace(agent_user=username, jenkins_home=home, websocket_mode=True),
    )

    with pytest.raises(service.RuntimeDirectoryError, match="symbolic link"):
        service.JenkinsAgentService(state).migrate_directory(home / "workspace")

    assert (home / "workspace").is_symlink()
    assert not (outside / "state").exists()


def _runtime_service(home: Path) -> service.JenkinsAgentService:
    """Build a service instance using the current test user."""
    username = pwd.getpwuid(os.geteuid()).pw_name
    state = cast(
        State, SimpleNamespace(agent_user=username, jenkins_home=home, websocket_mode=True)
    )
    return service.JenkinsAgentService(state)


def test_migrate_runtime_directories_leaves_usable_tree_untouched(tmp_path: Path):
    """
    Arrange: both runtime trees and their contents already have owner access.
    Act: migrate the legacy runtime directories.
    Assert: the existing paths and inodes are unchanged.
    """
    home = tmp_path / "jenkins-home"
    home.mkdir()
    inodes = {}
    for name in ("remoting", "workspace"):
        runtime = home / name
        runtime.mkdir(mode=0o750)
        data = runtime / "state"
        data.write_text(name)
        data.chmod(0o600)
        inodes[name] = (runtime.stat().st_ino, data.stat().st_ino)

    _runtime_service(home).migrate_directory(home)

    for name in ("remoting", "workspace"):
        runtime = home / name
        assert (runtime.stat().st_ino, (runtime / "state").stat().st_ino) == inodes[name]


def test_migrate_runtime_directories_rejects_non_directory_entry(tmp_path: Path):
    """
    Arrange: a runtime directory entry is a regular file.
    Act: migrate the legacy runtime directories.
    Assert: migration fails without replacing the entry.
    """
    home = tmp_path / "jenkins-home"
    home.mkdir()
    stale = home / "workspace"
    stale.write_text("legacy")

    with pytest.raises(service.RuntimeDirectoryError, match="existing directory"):
        _runtime_service(home).migrate_directory(home / "workspace")

    assert stale.read_text() == "legacy"


def test_migrate_runtime_directories_does_not_follow_nested_symlink(tmp_path: Path):
    """
    Arrange: a runtime tree contains a symlink and an inaccessible directory mode.
    Act: migrate the legacy runtime directories.
    Assert: real entries are repaired and the symlink target is not traversed.
    """
    home = tmp_path / "jenkins-home"
    home.mkdir()
    runtime = home / "remoting"
    runtime.mkdir()
    target = tmp_path / "outside"
    target.mkdir()
    outside_file = target / "state"
    outside_file.write_text("outside")
    (runtime / "link").symlink_to(target, target_is_directory=True)
    runtime.chmod(0o500)
    (home / "workspace").mkdir(mode=0o750)

    _runtime_service(home).migrate_directory(home)

    assert (runtime / "link").is_symlink()
    assert outside_file.read_text() == "outside"
    assert runtime.stat().st_mode & 0o700 == 0o700


def test_migrate_runtime_directories_changes_special_entry_owner_only(tmp_path: Path):
    """
    Arrange: a runtime tree contains a special file with missing owner write access.
    Act: migrate the legacy runtime directories.
    Assert: the special inode is handled without following a path.
    """
    home = tmp_path / "jenkins-home"
    home.mkdir()
    runtime = home / "remoting"
    runtime.mkdir(mode=0o750)
    fifo = runtime / "agent.pipe"
    os.mkfifo(fifo, 0o400)
    runtime.chmod(0o500)
    (home / "workspace").mkdir(mode=0o750)

    _runtime_service(home).migrate_directory(home)

    assert fifo.exists()
    assert fifo.stat().st_mode & 0o777 == 0o400


def test_migrate_runtime_directories_reports_unknown_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """
    Arrange: the configured service user is absent from the passwd database.
    Act: start runtime-directory migration.
    Assert: installation fails with an actionable user error.
    """
    home = tmp_path / "jenkins-home"
    home.mkdir()
    monkeypatch.setattr(pwd, "getpwnam", MagicMock(side_effect=KeyError))

    with pytest.raises(service.RuntimeDirectoryError, match="configured service user"):
        _runtime_service(home).migrate_directory(home)


def test_migrate_runtime_directories_rejects_different_filesystem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """
    Arrange: a runtime directory is reported on a different filesystem.
    Act: migrate the legacy runtime directories.
    Assert: migration fails before changing any entry.
    """
    home = tmp_path / "jenkins-home"
    home.mkdir()
    runtime = home / "workspace"
    runtime.mkdir()
    real_lstat = os.lstat

    def fake_lstat(path):
        result = real_lstat(path)
        if Path(path) == runtime:
            values = list(result)
            values[2] += 1
            return os.stat_result(values)
        return result

    monkeypatch.setattr(service.os, "lstat", fake_lstat)

    with pytest.raises(service.RuntimeDirectoryError, match="different filesystem"):
        _runtime_service(home).migrate_directory(home / "workspace")

    assert runtime.is_dir()


def test_migrate_runtime_directories_reports_walk_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """
    Arrange: walking a runtime tree reports an operating-system error.
    Act: list runtime entries.
    Assert: migration raises a package-install error.
    """
    home = tmp_path / "jenkins-home"
    home.mkdir()
    runtime = home / "workspace"
    runtime.mkdir()

    def failing_walk(*_args, **kwargs):
        kwargs["onerror"](OSError("walk failed"))
        return iter(())

    monkeypatch.setattr(service.os, "walk", failing_walk)

    with pytest.raises(service.RuntimeDirectoryError, match="inspect runtime directory"):
        _runtime_service(home)._runtime_tree_entries(runtime)


def test_migrate_runtime_directories_reports_special_entry_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """
    Arrange: a runtime tree contains a special entry and ownership update fails.
    Act: migrate the legacy runtime directories.
    Assert: migration reports the failed runtime entry.
    """
    home = tmp_path / "jenkins-home"
    home.mkdir()
    runtime = home / "remoting"
    runtime.mkdir(mode=0o750)
    fifo = runtime / "agent.pipe"
    os.mkfifo(fifo, 0o400)
    runtime.chmod(0o500)
    (home / "workspace").mkdir(mode=0o750)
    monkeypatch.setattr(service.os, "chown", MagicMock(side_effect=OSError("chown failed")))

    with pytest.raises(service.RuntimeDirectoryError, match=r"agent\.pipe"):
        _runtime_service(home).migrate_directory(home)

    assert fifo.exists()


def test_runtime_directories_usable_reports_stat_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """
    Arrange: reading a runtime directory entry fails.
    Act: check runtime-directory usability.
    Assert: reconciliation reports that the ownership action is required.
    """
    home = tmp_path / "jenkins-home"
    home.mkdir()
    monkeypatch.setattr(service.os, "lstat", MagicMock(side_effect=OSError("stat failed")))

    assert not _runtime_service(home).runtime_directories_usable()


def test_migrate_directory_reports_root_inspection_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """
    Arrange: inspecting the requested directory fails.
    Act: run the ownership migration.
    Assert: migration raises a package-install error.
    """
    home = tmp_path / "jenkins-home"
    home.mkdir()
    monkeypatch.setattr(service.os, "lstat", MagicMock(side_effect=OSError("stat failed")))

    with pytest.raises(service.RuntimeDirectoryError, match="inspect directory"):
        _runtime_service(home).migrate_directory(home)


def test_migrate_directory_rejects_nested_filesystem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """
    Arrange: a nested runtime entry is reported on another filesystem.
    Act: run the ownership migration.
    Assert: migration fails before changing entries.
    """
    home = tmp_path / "jenkins-home"
    home.mkdir()
    nested = home / "workspace"
    nested.mkdir()
    real_lstat = os.lstat

    def fake_lstat(path):
        result = real_lstat(path)
        if Path(path) == nested:
            values = list(result)
            values[2] += 1
            return os.stat_result(values)
        return result

    monkeypatch.setattr(service.os, "lstat", fake_lstat)

    with pytest.raises(service.RuntimeDirectoryError, match="different filesystem"):
        _runtime_service(home).migrate_directory(home)
