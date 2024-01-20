# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.
#
# Learn more about testing at: https://juju.is/docs/sdk/testing

# pylint: disable=protected-access
"""Test for agent relations."""

import secrets
from unittest.mock import MagicMock, PropertyMock

import ops.testing
import pytest
from charms.operator_libs_linux.v1 import systemd

import service
from charm import JenkinsAgentCharm

agent_relation_data = {"url": "http://example.com", "jenkins-agent-0_secret": secrets.token_hex(4)}


def test_agent_relation_joined(harness: ops.testing.Harness):
    """
    arrange: initialized jenkins-agent charm.
    act: add relation to the jenkins-k8s charm.
    assert: The agent set the correct information in the unit's relation databag.
    """
    harness.begin()
    relation_id = harness.add_relation("agent", "jenkins-k8s", unit_data=agent_relation_data)
    charm: JenkinsAgentCharm = harness.charm
    assert (
        harness.get_relation_data(relation_id, app_or_unit="jenkins-agent/0")
        == charm.state.agent_meta.as_dict()
    )


def test_agent_relation_changed_service_restart(
    harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch
):
    """
    arrange: initialized jenkins-agent charm related to jenkins-k8s charm with relation data.
    act: Trigger _on_agent_relation_changed hook.
    assert: The charm should be in active state.
    """
    _ = harness.add_relation("agent", "jenkins-k8s", unit_data=agent_relation_data)
    harness.begin()
    charm: JenkinsAgentCharm = harness.charm
    assert charm.state.agent_relation_credentials
    assert (
        charm.state.agent_relation_credentials.secret
        == agent_relation_data["jenkins-agent-0_secret"]
    )
    assert charm.state.agent_relation_credentials.address == agent_relation_data["url"]

    monkeypatch.setattr(charm.jenkins_agent_service, "restart", MagicMock())
    charm.agent_observer._on_agent_relation_changed(MagicMock())
    assert charm.unit.status.name == ops.ActiveStatus.name


def test_agent_relation_changed_service_restart_error(
    harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch
):
    """
    arrange: initialized jenkins-agent charm related to jenkins-k8s charm with relation data.
    act: Trigger _on_agent_relation_changed hook with restart throwing an exception.
    assert: The charm should be in error state with the correct error message.
    """
    _ = harness.add_relation("agent", "jenkins-k8s", unit_data=agent_relation_data)
    harness.begin()
    charm: JenkinsAgentCharm = harness.charm
    assert charm.state.agent_relation_credentials
    assert (
        charm.state.agent_relation_credentials.secret
        == agent_relation_data["jenkins-agent-0_secret"]
    )
    assert charm.state.agent_relation_credentials.address == agent_relation_data["url"]

    monkeypatch.setattr(
        charm.jenkins_agent_service, "restart", MagicMock(side_effect=service.ServiceRestartError)
    )
    with pytest.raises(RuntimeError, match="Error restarting the agent service."):
        charm.agent_observer._on_agent_relation_changed(MagicMock())


def test_agent_relation_changed_service_already_active(
    harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch
):
    """
    arrange: initialized jenkins-agent charm related to jenkins-k8s charm with relation data.
    act: Trigger _on_agent_relation_changed hook when the service is already up.
    assert: The charm skips restarting the agent service.
    """
    service_restart_mock = MagicMock()
    service_is_active_mock = PropertyMock(return_value=True)
    monkeypatch.setattr(service.JenkinsAgentService, "restart", service_restart_mock)
    monkeypatch.setattr(service.JenkinsAgentService, "is_active", service_is_active_mock)

    _ = harness.add_relation("agent", "jenkins-k8s", unit_data=agent_relation_data)
    harness.begin()
    charm: JenkinsAgentCharm = harness.charm
    charm.agent_observer._on_agent_relation_changed(MagicMock())
    assert service_is_active_mock.call_count == 1
    assert service_restart_mock.call_count == 0


def test_agent_relation_departed_service_stop_error(
    harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch
):
    """
    arrange: initialized jenkins-agent charm related to jenkins-k8s charm with relation data.
    act: remove the relation, raising an error when stopping the agent service.
    assert: The charm falls into BlockedStatus with the correct message.
    """
    monkeypatch.setattr(systemd, "service_stop", MagicMock(side_effect=systemd.SystemdError))

    relation_id = harness.add_relation("agent", "jenkins-k8s", unit_data=agent_relation_data)
    harness.begin()
    harness.remove_relation(relation_id=relation_id)
    charm: JenkinsAgentCharm = harness.charm
    assert charm.unit.status.name == ops.BlockedStatus.name
    assert charm.unit.status.message == "Error stopping the agent service"


def test_agent_relation_departed(harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch):
    """
    arrange: initialized jenkins-agent charm related to jenkins-k8s charm with relation data.
    act: remove the relation.
    assert: The charm falls into BlockedStatus with the correct message.
    """
    monkeypatch.setattr(systemd, "service_stop", MagicMock())

    relation_id = harness.add_relation("agent", "jenkins-k8s", unit_data=agent_relation_data)
    harness.begin()
    harness.remove_relation(relation_id=relation_id)
    charm: JenkinsAgentCharm = harness.charm
    assert charm.unit.status.name == ops.BlockedStatus.name
    assert charm.unit.status.message == "Waiting for config/relation."
