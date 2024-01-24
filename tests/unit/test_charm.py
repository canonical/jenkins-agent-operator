# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.
#
# Learn more about testing at: https://juju.is/docs/sdk/testing

"""Test for charm hooks."""

from unittest.mock import MagicMock

import ops
import ops.testing
import pytest

import charm_state
import service
from charm import JenkinsAgentCharm


def test___init___invalid_state(harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch):
    """
    arrange: patched State.from_charm that raises an InvalidState Error.
    act: when the JenkinsAgentCharm is initialized.
    assert: The agent falls into BlockedStatus.
    """
    monkeypatch.setattr(
        charm_state.State,
        "from_charm",
        MagicMock(side_effect=[charm_state.InvalidStateError("Invalid executor message")]),
    )

    harness.begin()

    charm: JenkinsAgentCharm = harness.charm
    assert charm.unit.status.name == ops.BlockedStatus.name
    assert charm.unit.status.message == "Invalid executor message"


def test__on_upgrade_charm(harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch):
    """
    arrange: given a charm with patched agent service that is active.
    act: when _on_upgrade_charm is called.
    assert: The agent falls into waiting status with the correct message.
    """
    monkeypatch.setattr(service.JenkinsAgentService, "is_active", MagicMock(return_value=True))
    monkeypatch.setattr(service.JenkinsAgentService, "restart", MagicMock())
    harness.begin()

    charm: JenkinsAgentCharm = harness.charm
    charm.on.upgrade_charm.emit()

    assert charm.unit.status.message == "Waiting for relation."
    assert charm.unit.status.name == ops.BlockedStatus.name


def test__on_config_changed(harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch):
    """
    arrange: given a charm with patched relation.
    act: when _on_config_changed is called.
    assert: The charm correctly updates the relation databag.
    """
    harness.begin()
    get_relation_mock = MagicMock()
    monkeypatch.setattr(ops.Model, "get_relation", get_relation_mock)

    charm: JenkinsAgentCharm = harness.charm
    charm.on.config_changed.emit()

    agent_relation = get_relation_mock.return_value
    assert agent_relation.data[charm.unit.name].update.call_count == 1


def test_restart_agent_service(harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch):
    """
    arrange: given a charm with patched relation.
    act: when _on_config_changed is called.
    assert: The charm correctly updates the relation databag.
    """
    get_relation_mock = MagicMock()
    monkeypatch.setattr(ops.Model, "get_relation", get_relation_mock)
    get_credentials_mock = MagicMock()
    restart_mock = MagicMock()
    monkeypatch.setattr(service.JenkinsAgentService, "restart", restart_mock)
    harness.begin()

    charm: JenkinsAgentCharm = harness.charm
    monkeypatch.setattr(charm.state, "agent_relation_credentials", get_credentials_mock)
    charm.restart_agent_service()

    assert restart_mock.call_count == 1
    assert charm.unit.status.name == ops.ActiveStatus.name


def test_restart_agent_service_incomplete_relation_data(
    harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch
):
    """
    arrange: given a charm with patched relation and incomplete relation data.
    act: when restart_agent_service is called.
    assert: The charm skips service restart and waits for complete relation data.
    """
    get_relation_mock = MagicMock()
    monkeypatch.setattr(ops.Model, "get_relation", get_relation_mock)
    harness.begin()

    charm: JenkinsAgentCharm = harness.charm
    monkeypatch.setattr(charm.state, "agent_relation_credentials", None)
    charm.restart_agent_service()

    assert charm.unit.status.name == ops.WaitingStatus.name


def test_restart_agent_service_service_restart_error(
    harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch
):
    """
    arrange: given a charm with patched relation and complete relation data.
    act: when restart_agent_service is called, raising an error.
    assert: The charm goes into error state with the correct error message.
    """
    get_relation_mock = MagicMock()
    monkeypatch.setattr(ops.Model, "get_relation", get_relation_mock)
    get_credentials_mock = MagicMock()
    restart_mock = MagicMock(side_effect=service.ServiceRestartError)
    monkeypatch.setattr(service.JenkinsAgentService, "restart", restart_mock)

    harness.begin()

    charm: JenkinsAgentCharm = harness.charm
    monkeypatch.setattr(charm.state, "agent_relation_credentials", get_credentials_mock)
    with pytest.raises(RuntimeError, match="Error restarting the agent service"):
        charm.restart_agent_service()
