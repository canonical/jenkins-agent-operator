# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.
#
# Learn more about testing at: https://juju.is/docs/sdk/testing

# pylint: disable=protected-access
"""Test for charm hooks."""

import unittest
import unittest.mock
from unittest.mock import patch

import ops
import ops.testing

import charm_state
from charm import JenkinsAgentCharm

ACTIVE_STATUS_NAME = "active"
BLOCKED_STATUS_NAME = "blocked"
MAINTENANCE_STATUS_NAME = "maintenance"
WAITING_STATUS_NAME = "waiting"
ERROR_STATUS_NAME = "error"


def raise_exception(exception: Exception):
    """Raise exception function for monkeypatching.

    Args:
        exception: The exception to raise.

    Raises:
        exception: .
    """
    raise exception


class TestCharm(unittest.TestCase):
    """Test class for unit testing."""

    def setUp(self):
        """Initialize the test class."""
        self.harness = ops.testing.Harness(JenkinsAgentCharm)
        self.addCleanup(self.harness.cleanup)

    @patch(
        "charm_state.State.from_charm",
        side_effect=charm_state.InvalidStateError("Invalid executor message"),
    )
    def test___init___invalid_state(self, _):
        """
        arrange: patched State.from_charm that raises an InvalidState Error.
        act: when the JenkinsAgentCharm is initialized.
        assert: The agent falls into BlockedStatus.
        """
        self.harness.begin()

        jenkins_charm: JenkinsAgentCharm = self.harness.charm
        assert jenkins_charm.unit.status.name == BLOCKED_STATUS_NAME
        assert jenkins_charm.unit.status.message == "Invalid executor message"

    @patch("service.JenkinsAgentService.restart")
    @patch("service.JenkinsAgentService.is_active", return_value=True)
    @patch("ops.UpgradeCharmEvent")
    def test__on_upgrade_charm(self, _service_restart, _service_is_active, _upgrade_charm_event):
        """
        arrange: given a charm with patched agent service that is active.
        act: when _on_upgrade_charm is called.
        assert: The agent falls into waiting status with the correct message.
        """
        self.harness.begin()

        jenkins_charm: JenkinsAgentCharm = self.harness.charm
        jenkins_charm._on_upgrade_charm(_upgrade_charm_event)

        assert jenkins_charm.unit.status.message == "Waiting for relation."
        assert jenkins_charm.unit.status.name == BLOCKED_STATUS_NAME

    @patch("ops.ConfigChangedEvent")
    @patch("ops.Model.get_relation")
    def test__on_config_changed(self, _get_relation_mock, _config_changed_event):
        """
        arrange: given a charm with patched relation.
        act: when _on_config_changed is called.
        assert: The charm correctly updates the relation databag.
        """
        self.harness.begin()
        jenkins_charm: JenkinsAgentCharm = self.harness.charm
        jenkins_charm._on_config_changed(_config_changed_event)

        agent_relation = _get_relation_mock.return_value
        assert agent_relation.data[self.harness._unit_name].update.call_count == 1
