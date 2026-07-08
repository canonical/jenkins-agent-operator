# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.
#
# Learn more about testing at: https://juju.is/docs/sdk/testing

"""Test for service interaction."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, PropertyMock

import ops.testing
import pytest
from charms.operator_libs_linux.v0 import apt
from charms.operator_libs_linux.v1 import systemd

import service
from charm_state import AGENT_RELATION

if TYPE_CHECKING:
    from charm import JenkinsAgentCharm


def _mock_install_host(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, package_installed: bool = False
) -> SimpleNamespace:
    """Mock host syscalls so JenkinsAgentService.install runs off-host.

    Args:
        monkeypatch: pytest monkeypatch fixture.
        tmp_path: temporary directory the service files are written to.
        package_installed: whether the required apt package is already present.

    Returns:
        Namespace of the patched systemd entry points (daemon_reload, service_enable)
        and the tmp paths for the unit and script files.
    """
    unit_path = tmp_path / "jenkins-agent.service"
    script_path = tmp_path / "jenkins-agent"
    monkeypatch.setattr(service, "JENKINS_AGENT_SYSTEMD_PATH", unit_path)
    monkeypatch.setattr(service, "JENKINS_AGENT_START_SCRIPT_PATH", script_path)
    monkeypatch.setattr(os, "chmod", MagicMock())
    monkeypatch.setattr(os, "chown", MagicMock())
    daemon_reload = MagicMock()
    service_enable = MagicMock()
    monkeypatch.setattr(systemd, "daemon_reload", daemon_reload)
    monkeypatch.setattr(systemd, "service_enable", service_enable)
    from_installed = (
        MagicMock() if package_installed else MagicMock(side_effect=apt.PackageNotFoundError)
    )
    monkeypatch.setattr(apt.DebianPackage, "from_installed_package", from_installed)
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
    harness.begin()
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
    assert apt_add_package_mock.call_args_list[0][0][0] == ["openjdk-21-jre"]
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

    harness.begin()
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
    harness.begin()

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
    harness.begin()

    with pytest.raises(RuntimeError, match=r"Error installing the agent service"):
        harness.charm.on.install.emit()


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
        service.JenkinsAgentService, "_startup_check", MagicMock(return_value=True)
    )
    # The reconcile handler also runs install(); it is exercised separately, so
    # stub it here to keep this test focused on the restart/config-render path.
    monkeypatch.setattr(service.JenkinsAgentService, "install", MagicMock())

    harness.add_relation(AGENT_RELATION, "jenkins-k8s", unit_data=agent_relation_data)
    harness.begin()
    charm: JenkinsAgentCharm = harness.charm
    charm.on.config_changed.emit()

    assert pathlib_write_text_mock.call_args[0][0] == service_configuration_template
    assert charm.unit.status.name == ops.ActiveStatus.name


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
    harness.begin()
    charm: JenkinsAgentCharm = harness.charm

    with pytest.raises(
        service.ServiceRestartError,
        match=r"Error interacting with the filesystem when rendering configuration file",
    ):
        charm.jenkins_agent_service.restart()


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
    harness.begin()
    charm: JenkinsAgentCharm = harness.charm

    with pytest.raises(
        service.ServiceRestartError,
        match=rf"Error starting the agent service:\n{systemd_error_message}",
    ):
        charm.jenkins_agent_service.restart()


def test_service_is_active_systemd_error(
    harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch
):
    """
    arrange: Harness with mocked fs-related methods raising an error.
    act: Check if the service is running.
    assert: The call should return false and not raising any exceptions.
    """
    harness.begin()
    # Mock Path.exists to return True for AGENT_READY_PATH so we exercise the
    # systemd.service_running path.
    real_exists = Path.exists

    def _mock_exists(self):
        if str(self) == str(service.AGENT_READY_PATH):
            return True
        return real_exists(self)

    monkeypatch.setattr(Path, "exists", _mock_exists)
    monkeypatch.setattr(systemd, "service_running", MagicMock(side_effect=SystemError))
    charm: JenkinsAgentCharm = harness.charm

    assert not charm.jenkins_agent_service.is_active


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
    harness.begin()
    charm: JenkinsAgentCharm = harness.charm
    from charm_state import Credentials

    result = charm.jenkins_agent_service.credentials_changed(
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
    harness.begin()
    charm: JenkinsAgentCharm = harness.charm

    with pytest.raises(service.ServiceRestartError, match=r"missing configuration"):
        charm.jenkins_agent_service.restart()


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
    monkeypatch.setattr(service.JenkinsAgentService, "is_active", PropertyMock(return_value=False))
    # Collapse the polling loop so the test does not actually sleep.
    monkeypatch.setattr(service, "STARTUP_CHECK_TIMEOUT", 0)
    monkeypatch.setattr(service.time, "sleep", MagicMock())
    harness.add_relation(AGENT_RELATION, "jenkins-k8s", unit_data=agent_relation_data)
    harness.begin()
    charm: JenkinsAgentCharm = harness.charm

    with pytest.raises(
        service.ServiceRestartError, match=r"waiting for the agent service to start"
    ):
        charm.jenkins_agent_service.restart()


def test_startup_check_becomes_active(
    harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch
):
    """
    arrange: a service that reports active on the first poll.
    act: call the startup check.
    assert: it returns True after breaking out of the poll loop.
    """
    monkeypatch.setattr(service.JenkinsAgentService, "is_active", PropertyMock(return_value=True))
    monkeypatch.setattr(service.time, "sleep", MagicMock())
    harness.begin()
    charm: JenkinsAgentCharm = harness.charm

    assert charm.jenkins_agent_service._startup_check() is True


def test_reset_failed_state_logs_on_error(
    harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch
):
    """
    arrange: a service whose systemctl reset-failed raises a SystemdError.
    act: call reset_failed_state.
    assert: the error is swallowed (logged, not raised) as it is non-critical.
    """
    monkeypatch.setattr(systemd, "_systemctl", MagicMock(side_effect=systemd.SystemdError))
    harness.begin()
    charm: JenkinsAgentCharm = harness.charm

    # Should not raise.
    charm.jenkins_agent_service.reset_failed_state()


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
    harness.begin()
    charm: JenkinsAgentCharm = harness.charm

    charm.jenkins_agent_service.reset()

    assert stop_mock.call_count == 1
    assert not override.exists()


def test_reset_stop_error(harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch):
    """
    arrange: a service whose systemctl stop raises a SystemdError.
    act: call reset.
    assert: ServiceStopError is raised.
    """
    monkeypatch.setattr(systemd, "service_stop", MagicMock(side_effect=systemd.SystemdError))
    harness.begin()
    charm: JenkinsAgentCharm = harness.charm

    with pytest.raises(service.ServiceStopError, match=r"failed to stop"):
        charm.jenkins_agent_service.reset()


def test_sync_service_files_read_error(
    harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """
    arrange: a service whose existing on-disk unit file cannot be read.
    act: run install to trigger the file sync.
    assert: FileRenderError is raised when the destination file read fails.
    """
    unit_path = tmp_path / "jenkins-agent.service"
    unit_path.write_text("stale")
    monkeypatch.setattr(service, "JENKINS_AGENT_SYSTEMD_PATH", unit_path)
    monkeypatch.setattr(service, "JENKINS_AGENT_START_SCRIPT_PATH", tmp_path / "jenkins-agent")

    real_read_text = Path.read_text

    def _read_text(self, *args, **kwargs):
        # Fail only when reading the existing on-disk unit file; template reads
        # (relative paths under templates/) must still succeed.
        if self == unit_path:
            raise OSError("disk gone")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _read_text)
    harness.begin()
    charm: JenkinsAgentCharm = harness.charm

    with pytest.raises(service.FileRenderError, match=r"Error reading file"):
        charm.jenkins_agent_service.install()
