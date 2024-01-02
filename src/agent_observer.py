# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.

"""The agent relation observer module."""


import logging
import os
from pathlib import Path

import ops

import service
from charm_state import AGENT_RELATION, State

logger = logging.getLogger()
JENKINS_WORKDIR = Path("/var/snap/jenkins-agent/common/agent")
AGENT_READY_PATH = Path(JENKINS_WORKDIR / ".ready")


class Observer(ops.Object):
    """The Jenkins agent relation observer."""

    def __init__(
        self,
        charm: ops.CharmBase,
        state: State,
        jenkins_agent_service: service.JenkinsAgentService,
    ):
        """Initialize the observer and register event handlers.

        Args:
            charm: The parent charm to attach the observer to.
            state: The charm state.
            jenkins_agent_service: Service manager that controls Jenkins agent service.
        """
        super().__init__(charm, "agent-observer")
        self.charm = charm
        self.state = state
        self.jenkins_agent_service = jenkins_agent_service
        charm.framework.observe(
            charm.on[AGENT_RELATION].relation_joined, self._on_agent_relation_joined
        )
        charm.framework.observe(
            charm.on[AGENT_RELATION].relation_changed, self._on_agent_relation_changed
        )
        charm.framework.observe(
            charm.on[AGENT_RELATION].relation_departed, self._on_agent_relation_departed
        )

    def _on_agent_relation_joined(self, event: ops.RelationJoinedEvent) -> None:
        """Handle agent relation joined event.

        Args:
            event: The event fired when an agent has joined the relation.
        """
        self.charm.unit.status = ops.MaintenanceStatus(
            f"Setting up '{event.relation.name}' relation."
        )

        relation_data = self.state.agent_meta.get_jenkins_agent_v0_interface_dict()
        logger.debug("Agent relation data set: %s", relation_data)
        event.relation.data[self.charm.unit].update(relation_data)

    def _on_agent_relation_changed(self, event: ops.RelationChangedEvent) -> None:
        """Handle agent relation changed event.

        Args:
            event: The event fired when the agent relation data has changed.
        """
        logger.info("%s relation changed.", event.relation.name)

        # Check if the pebble service has started and set agent ready.
        if os.path.exists(str(AGENT_READY_PATH)) and self.jenkins_agent_service.is_active:
            logger.warning("Given agent already registered. Skipping.")
            return

        if not self.state.agent_relation_credentials:
            self.charm.unit.status = ops.WaitingStatus("Waiting for complete relation data.")
            logger.info("Waiting for complete relation data.")
            return

        self.charm.unit.status = ops.MaintenanceStatus("Starting jenkins agent service")
        try:
            self.jenkins_agent_service.restart()
        except service.ServiceRestartError as e:
            logger.debug("Error restarting the agent service %s", e)
            self.charm.unit.status = ops.BlockedStatus("Agent service failed to start")
            return

        if not self.jenkins_agent_service.is_active:
            # The jenkins server sets credentials one by one, hence if the current credentials are
            # not for this particular agent, the agent operator should wait until it receives one
            # designated for it.
            logger.warning(
                "Failed credential for agent %s, will wait for next credentials to be set",
                self.state.agent_meta.name,
            )
            self.charm.unit.status = ops.WaitingStatus(
                "Failed to start the service. Waiting for another set of credentials"
            )
            return

        self.charm.unit.status = ops.ActiveStatus()

    def _on_agent_relation_departed(self, _: ops.RelationDepartedEvent) -> None:
        """Handle agent relation departed event."""
        self.jenkins_agent_service.stop()
        self.charm.unit.status = ops.BlockedStatus("Waiting for config/relation.")
