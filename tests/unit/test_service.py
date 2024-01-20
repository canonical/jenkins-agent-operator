# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.
"""Test for service interaction."""

import os
from pathlib import Path

# pylint: disable=protected-access
from unittest.mock import MagicMock

import ops.testing
import pytest
from charms.operator_libs_linux.v0 import apt
from charms.operator_libs_linux.v1 import systemd

import service
from charm import JenkinsAgentCharm

from .test_agent import agent_relation_data

service_config_template = f'''[Service]
Environment="JENKINS_TOKEN={agent_relation_data.get('jenkins-agent-0_secret')}"
Environment="JENKINS_URL={agent_relation_data.get('url')}"
Environment="JENKINS_AGENT=jenkins-agent-0"'''


@pytest.mark.parametrize(
    "f,error_thrown",
    [
        ("import_key", apt.GPGKeyError),
        ("add_package", apt.PackageError),
        ("add_package", apt.PackageNotFoundError),
    ],
)
def test_install_apt_package_gpg_key_error(
    harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch, f, error_thrown
):
    """
    arrange: Harness with mocked apt module.
    act: run _on_install hook with methods raising different errors.
    assert: The charm should be in an error state.
    """
    harness.begin()
    charm: JenkinsAgentCharm = harness.charm
    monkeypatch.setattr(apt, "RepositoryMapping", MagicMock())
    monkeypatch.setattr(apt, "import_key", MagicMock())
    monkeypatch.setattr(apt, "update", MagicMock())
    monkeypatch.setattr(apt, "add_package", MagicMock())

    monkeypatch.setattr(apt, f, MagicMock(side_effect=[error_thrown]))

    with pytest.raises(RuntimeError, match="Error installing the agent service"):
        charm._on_install(MagicMock(spec=ops.InstallEvent))


def test_on_install_add_ppa(harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch):
    """
    arrange: Harness with mocked apt module.
    act: run _on_install hook with methods raising different errors.
    assert: The charm should be in an error state.
    """
    apt_repository_mapping_mock = MagicMock()
    apt_import_key_mock = MagicMock()
    apt_update_mock = MagicMock()
    apt_add_package_mock = MagicMock()

    apt_repository_mapping_mock.__contains__.return_value = False
    monkeypatch.setattr(apt, "RepositoryMapping", apt_repository_mapping_mock)
    monkeypatch.setattr(apt, "import_key", apt_import_key_mock)
    monkeypatch.setattr(apt, "update", apt_update_mock)
    monkeypatch.setattr(apt, "add_package", apt_add_package_mock)

    harness.begin_with_initial_hooks()
    assert apt_add_package_mock.call_count == 2


def test_restart_service(harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch):
    """
    arrange: Harness with mocked apt module.
    act: run _on_install hook with methods raising different errors.
    assert: The charm should be in an error state.
    """
    pathlib_write_text_mock = MagicMock()
    monkeypatch.setattr(Path, "write_text", pathlib_write_text_mock)
    monkeypatch.setattr(os, "chmod", MagicMock)
    monkeypatch.setattr(os, "chown", MagicMock)
    monkeypatch.setattr(systemd, "daemon_reload", MagicMock)
    monkeypatch.setattr(systemd, "service_restart", MagicMock)
    monkeypatch.setattr(service.JenkinsAgentService, "is_active", MagicMock)

    harness.add_relation("agent", "jenkins-k8s", unit_data=agent_relation_data)
    harness.begin()
    charm: JenkinsAgentCharm = harness.charm
    charm.jenkins_agent_service.restart()
    assert pathlib_write_text_mock.call_args[0][0] == service_config_template


def test_restart_service_write_config_type_error(
    harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch
):
    """
    arrange: Harness with mocked apt module.
    act: run _on_install hook with methods raising different errors.
    assert: The charm should be in an error state.
    """
    monkeypatch.setattr(Path, "write_text", MagicMock(side_effect=TypeError))
    harness.add_relation("agent", "jenkins-k8s", unit_data=agent_relation_data)
    harness.begin()
    charm: JenkinsAgentCharm = harness.charm
    with pytest.raises(
        service.ServiceRestartError,
        match="Error interacting with the filesystem when rendering configuration file",
    ):
        charm.jenkins_agent_service.restart()


def test_restart_service_systemd_error(
    harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch
):
    """
    arrange: Harness with mocked apt module.
    act: run _on_install hook with methods raising different errors.
    assert: The charm should be in an error state.
    """
    systemd_error_message = "Mock systemd error"
    monkeypatch.setattr(service.JenkinsAgentService, "_render_file", MagicMock)
    monkeypatch.setattr(
        systemd,
        "daemon_reload",
        MagicMock(side_effect=systemd.SystemdError(systemd_error_message)),
    )

    harness.add_relation("agent", "jenkins-k8s", unit_data=agent_relation_data)
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
    arrange: Harness with mocked apt module.
    act: run _on_install hook with methods raising different errors.
    assert: The charm should be in an error state.
    """
    harness.begin()
    monkeypatch.setattr(systemd, "service_running", MagicMock(side_effect=SystemError))
    charm: JenkinsAgentCharm = harness.charm
    assert not charm.jenkins_agent_service.is_active
