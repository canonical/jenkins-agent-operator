# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.
#
# Learn more about testing at: https://juju.is/docs/sdk/testing

"""Test for service interaction."""

import os
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import ops.testing
import pytest
from charms.operator_libs_linux.v0 import apt
from charms.operator_libs_linux.v1 import systemd

import service
from charm_state import AGENT_RELATION

if TYPE_CHECKING:
    from charm import JenkinsAgentCharm


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
    test_systemd_path = tmp_path / "systemd"
    test_script_path = tmp_path / "script"
    harness.begin()
    charm: JenkinsAgentCharm = harness.charm
    monkeypatch.setattr(service, "JENKINS_AGENT_SYSTEMD_PATH", test_systemd_path)
    monkeypatch.setattr(service, "JENKINS_AGENT_START_SCRIPT_PATH", test_script_path)
    monkeypatch.setattr(os, "chmod", MagicMock())
    monkeypatch.setattr(os, "chown", MagicMock())
    monkeypatch.setattr(apt, "add_package", MagicMock())
    monkeypatch.setattr(apt, f, MagicMock(side_effect=[error_thrown]))

    with pytest.raises(RuntimeError, match="Error installing the agent service"):
        charm.on.install.emit()


def test_on_install(harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """
    arrange: Harness with mocked apt module.
    act: run initial hook.
    assert: The installation should pass without error and charm in blocked state.
    """
    apt_repository_mapping_mock = MagicMock()
    apt_add_package_mock = MagicMock()
    apt_repository_mapping_mock.__contains__.return_value = False
    test_systemd_path = tmp_path / "systemd"
    test_script_path = tmp_path / "script"
    monkeypatch.setattr(service, "JENKINS_AGENT_SYSTEMD_PATH", test_systemd_path)
    monkeypatch.setattr(service, "JENKINS_AGENT_START_SCRIPT_PATH", test_script_path)
    monkeypatch.setattr(os, "chmod", MagicMock())
    monkeypatch.setattr(os, "chown", MagicMock())
    monkeypatch.setattr(apt, "add_package", apt_add_package_mock)

    harness.begin_with_initial_hooks()

    assert apt_add_package_mock.call_count == 1
    assert apt_add_package_mock.call_args_list[0][0][0] == ["openjdk-21-jre"]

    assert harness.charm.unit.status.name == ops.BlockedStatus.name


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
    monkeypatch.setattr(os.path, "exists", MagicMock(return_value=True))

    harness.add_relation(AGENT_RELATION, "jenkins-k8s", unit_data=agent_relation_data)
    harness.begin()
    charm: JenkinsAgentCharm = harness.charm
    charm.on.start.emit()

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
        match="Error interacting with the filesystem when rendering configuration file",
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
        match=f"Error starting the agent service:\n{systemd_error_message}",
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
    monkeypatch.setattr(systemd, "service_running", MagicMock(side_effect=SystemError))
    charm: JenkinsAgentCharm = harness.charm

    assert not charm.jenkins_agent_service.is_active
