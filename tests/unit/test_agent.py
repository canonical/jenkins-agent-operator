# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.
#
# Learn more about testing at: https://juju.is/docs/sdk/testing

"""Test for agent relations."""

import pathlib
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, PropertyMock

import ops.testing
import pytest
from charms.operator_libs_linux.v1 import systemd

import service
from charm_state import AGENT_RELATION

if TYPE_CHECKING:
    from charm import JenkinsAgentCharm


def test_agent_relation_joined(harness: ops.testing.Harness, agent_relation_data: dict):
    """
    arrange: initialized jenkins-agent charm.
    act: add relation to the jenkins-k8s charm.
    assert: The agent set the correct information in the unit's relation databag.
    """
    harness.begin()
    relation_id = harness.add_relation(
        AGENT_RELATION, "jenkins-k8s", unit_data=agent_relation_data
    )
    charm: JenkinsAgentCharm = harness.charm
    assert (
        harness.get_relation_data(relation_id, app_or_unit="jenkins-agent/0")
        == charm.state.agent_meta.as_dict()
    )


def test_agent_relation_changed_service_restart(
    harness_with_agent_relation: ops.testing.Harness,
    monkeypatch: pytest.MonkeyPatch,
    agent_relation_data: dict,
):
    """
    arrange: initialized jenkins-agent charm related to jenkins-k8s charm with relation data.
    act: Trigger _on_agent_relation_changed hook.
    assert: The charm should be in active state.
    """
    harness = harness_with_agent_relation
    harness.begin()
    charm: JenkinsAgentCharm = harness.charm
    monkeypatch.setattr(charm.jenkins_agent_service, "restart", MagicMock())

    charm.on.agent_relation_changed.emit(harness.model.get_relation(AGENT_RELATION))

    assert charm.state.agent_relation_credentials
    assert (
        charm.state.agent_relation_credentials.secret
        == agent_relation_data["jenkins-agent-0_secret"]
    )
    assert charm.state.agent_relation_credentials.address == agent_relation_data["url"]
    assert charm.unit.status.name == ops.ActiveStatus.name


def test_agent_relation_changed_service_restart_error(
    harness_with_agent_relation: ops.testing.Harness,
    monkeypatch: pytest.MonkeyPatch,
    agent_relation_data: dict,
):
    """
    arrange: initialized jenkins-agent charm related to jenkins-k8s charm with relation data.
    act: Trigger _on_agent_relation_changed hook with restart throwing an exception.
    assert: The charm should be in error state with the correct error message.
    """
    harness = harness_with_agent_relation
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
    with pytest.raises(RuntimeError, match=r"Error restarting the agent service\."):
        harness.charm.on.agent_relation_changed.emit(harness.model.get_relation(AGENT_RELATION))


def test_agent_relation_changed_service_already_active(
    harness_with_agent_relation: ops.testing.Harness,
    monkeypatch: pytest.MonkeyPatch,
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
    harness = harness_with_agent_relation
    harness.begin()

    harness.charm.on.agent_relation_changed.emit(harness.model.get_relation(AGENT_RELATION))

    assert service_is_active_mock.call_count == 1
    assert service_restart_mock.call_count == 0


def test_agent_relation_departed_service_stop_error(
    harness_with_agent_relation: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch
):
    """
    arrange: initialized jenkins-agent charm related to jenkins-k8s charm with relation data.
    act: remove the relation, raising an error when stopping the agent service.
    assert: The charm falls into BlockedStatus with the correct message.
    """
    monkeypatch.setattr(systemd, "service_stop", MagicMock(side_effect=systemd.SystemdError))

    harness = harness_with_agent_relation
    harness.begin()

    relation = harness.model.get_relation(AGENT_RELATION)
    assert relation
    harness.remove_relation(relation_id=relation.id)

    charm: JenkinsAgentCharm = harness.charm
    assert charm.unit.status.name == ops.BlockedStatus.name
    assert charm.unit.status.message == "Error stopping the agent service"


def test_agent_relation_departed(
    harness_with_agent_relation: ops.testing.Harness,
    monkeypatch: pytest.MonkeyPatch,
):
    """
    arrange: initialized jenkins-agent charm related to jenkins-k8s charm with relation data.
    act: remove the relation.
    assert: The charm falls into BlockedStatus with the correct message.
    """
    monkeypatch.setattr(systemd, "service_stop", MagicMock())
    path_unlink_mock = MagicMock()
    monkeypatch.setattr(pathlib.Path, "unlink", path_unlink_mock)

    harness = harness_with_agent_relation
    harness.begin()

    relation = harness.model.get_relation(AGENT_RELATION)
    assert relation
    harness.remove_relation(relation_id=relation.id)

    charm: JenkinsAgentCharm = harness.charm
    assert charm.unit.status.name == ops.BlockedStatus.name
    assert charm.unit.status.message == "Waiting for config/relation."
    path_unlink_mock.assert_called_once()
