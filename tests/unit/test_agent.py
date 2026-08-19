# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.
#
# Learn more about testing at: https://juju.is/docs/sdk/testing

"""Test for agent relations driving reconcile."""

from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import PropertyMock

import ops
import ops.testing
import pytest

import charm_state
import service
from charm_state import AGENT_RELATION

if TYPE_CHECKING:
    from charm import JenkinsAgentCharm


def test_agent_relation_joined_sets_databag(
    harness: ops.testing.Harness, service_mocks: SimpleNamespace, agent_relation_data: dict
):
    """
    arrange: initialized jenkins-agent charm.
    act: add relation to the jenkins-k8s charm (fires relation events -> reconcile).
    assert: The agent set the correct information in the unit's relation databag.
    """
    harness.begin()
    relation_id = harness.add_relation(
        AGENT_RELATION, "jenkins-k8s", unit_data=agent_relation_data
    )

    charm: JenkinsAgentCharm = harness.charm
    assert (
        harness.get_relation_data(relation_id, app_or_unit="jenkins-agent/0")
        == charm_state.State.from_charm(charm).agent_meta.as_dict()
    )


def test_agent_relation_changed_restarts_service(
    harness_with_agent_relation: ops.testing.Harness,
    service_mocks: SimpleNamespace,
    agent_relation_data: dict,
):
    """
    arrange: jenkins-agent charm related to jenkins-k8s with complete relation data.
    act: emit an event to trigger reconcile.
    assert: The charm restarts the service and becomes active.
    """
    harness = harness_with_agent_relation
    harness.begin()

    charm: JenkinsAgentCharm = harness.charm
    charm.on.config_changed.emit()

    credentials = charm_state.State.from_charm(charm).agent_relation_credentials
    assert credentials
    assert credentials.secret == agent_relation_data["test-model-jenkins-agent-0_secret"]
    assert credentials.address == agent_relation_data["url"]
    assert service_mocks.restart.call_count == 1
    assert charm.unit.status.name == ops.ActiveStatus.name


def test_agent_relation_changed_restart_error(
    harness_with_agent_relation: ops.testing.Harness,
    service_mocks: SimpleNamespace,
):
    """
    arrange: jenkins-agent charm related to jenkins-k8s whose service restart fails.
    act: emit an event to trigger reconcile.
    assert: The charm raises RuntimeError with the correct error message.
    """
    service_mocks.restart.side_effect = service.ServiceRestartError
    harness = harness_with_agent_relation
    harness.begin()

    with pytest.raises(RuntimeError, match=r"Error restarting the agent service"):
        harness.charm.on.config_changed.emit()


def test_agent_relation_no_restart_when_unchanged(
    harness_with_agent_relation: ops.testing.Harness,
    monkeypatch: pytest.MonkeyPatch,
    service_mocks: SimpleNamespace,
):
    """
    arrange: jenkins-agent related to jenkins-k8s, service active with unchanged creds.
    act: emit an event to trigger reconcile.
    assert: The charm does not restart the service and stays active.
    """
    monkeypatch.setattr(service.JenkinsAgentService, "is_active", PropertyMock(return_value=True))
    service_mocks.credentials_changed.return_value = False
    harness = harness_with_agent_relation
    harness.begin()

    harness.charm.on.config_changed.emit()

    assert service_mocks.restart.call_count == 0
    assert harness.charm.unit.status.name == ops.ActiveStatus.name


def test_agent_relation_broken_stops_service(
    harness_with_agent_relation: ops.testing.Harness,
    monkeypatch: pytest.MonkeyPatch,
    service_mocks: SimpleNamespace,
):
    """
    arrange: jenkins-agent related to jenkins-k8s with an active service.
    act: remove the relation (fires departed then broken -> reconcile).
    assert: The charm stops the service and is blocked waiting for a relation.
    """
    monkeypatch.setattr(service.JenkinsAgentService, "is_active", PropertyMock(return_value=True))
    harness = harness_with_agent_relation
    harness.begin()

    relation = harness.model.get_relation(AGENT_RELATION)
    assert relation
    harness.remove_relation(relation.id)

    assert service_mocks.reset.call_count >= 1
    assert harness.charm.unit.status.name == ops.BlockedStatus.name


def test_agent_relation_broken_stop_error(
    harness_with_agent_relation: ops.testing.Harness,
    monkeypatch: pytest.MonkeyPatch,
    service_mocks: SimpleNamespace,
):
    """
    arrange: jenkins-agent related to jenkins-k8s, active service whose stop fails.
    act: remove the relation to trigger reconcile teardown.
    assert: The charm is blocked reporting the stop error.
    """
    monkeypatch.setattr(service.JenkinsAgentService, "is_active", PropertyMock(return_value=True))
    service_mocks.reset.side_effect = service.ServiceStopError
    harness = harness_with_agent_relation
    harness.begin()

    relation = harness.model.get_relation(AGENT_RELATION)
    assert relation
    harness.remove_relation(relation.id)

    assert harness.charm.unit.status.name == ops.BlockedStatus.name
    assert harness.charm.unit.status.message == "Error stopping the agent service"
