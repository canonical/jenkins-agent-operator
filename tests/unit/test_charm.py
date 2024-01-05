# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.
#
# Learn more about testing at: https://juju.is/docs/sdk/testing

# pylint: disable=protected-access
"""Test for charm hooks."""

from unittest.mock import MagicMock

import ops
import ops.testing
import pytest

import charm_state
import service
from charm import JenkinsAgentCharm


def raise_exception(exception: Exception):
    """Raise exception function for monkeypatching.

    Args:
        exception: The exception to raise.

    Raises:
        exception: .
    """
    raise exception


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

    jenkins_charm: JenkinsAgentCharm = harness.charm
    assert jenkins_charm.unit.status.name == ops.BlockedStatus.name
    assert jenkins_charm.unit.status.message == "Invalid executor message"


def test__on_upgrade_charm(harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch):
    """
    arrange: given a charm with patched agent service that is active.
    act: when _on_upgrade_charm is called.
    assert: The agent falls into waiting status with the correct message.
    """
    monkeypatch.setattr(service.JenkinsAgentService, "is_active", MagicMock(return_value=True))
    monkeypatch.setattr(service.JenkinsAgentService, "restart", MagicMock())
    harness.begin()

    jenkins_charm: JenkinsAgentCharm = harness.charm
    upgrade_charm_event = MagicMock(spec=ops.UpgradeCharmEvent)
    jenkins_charm._on_upgrade_charm(upgrade_charm_event)

    assert jenkins_charm.unit.status.message == "Waiting for relation."
    assert jenkins_charm.unit.status.name == ops.BlockedStatus.name


def test__on_config_changed(harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch):
    """
    arrange: given a charm with patched relation.
    act: when _on_config_changed is called.
    assert: The charm correctly updates the relation databag.
    """
    harness.begin()
    config_changed_event = MagicMock(spec=ops.ConfigChangedEvent)
    get_relation_mock = MagicMock()
    monkeypatch.setattr(ops.Model, "get_relation", get_relation_mock)

    jenkins_charm: JenkinsAgentCharm = harness.charm
    jenkins_charm._on_config_changed(config_changed_event)

    agent_relation = get_relation_mock.return_value
    assert agent_relation.data[harness._unit_name].update.call_count == 1
