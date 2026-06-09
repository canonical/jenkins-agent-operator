# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""The agent relation observer module."""

import logging

import ops

import service
from charm_state import AGENT_RELATION, State

logger = logging.getLogger()


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

        relation_data = self.state.agent_meta.as_dict()
        logger.debug("Setting agent relation unit data: %s", relation_data)
        event.relation.data[self.charm.unit].update(relation_data)

    def _on_agent_relation_changed(self, _: ops.RelationChangedEvent) -> None:
        """Handle agent relation changed event.

        Raises:
            RuntimeError: when the service fails to properly start.
        """
        # If relation data is not yet available to this unit, set its status to waiting
        if not self.state.agent_relation_credentials:
            self.charm.unit.status = ops.WaitingStatus("Waiting for complete relation data.")
            logger.info("Waiting for complete relation data.")
            return

        # If the service is already active, check whether the server URL has changed.
        # A Jenkins server pod restart assigns a new IP, requiring agents to reconnect.
        if self.jenkins_agent_service.is_active:
            if not self.jenkins_agent_service.credentials_changed(
                self.state.agent_relation_credentials
            ):
                logger.info("Agent service running with current credentials. No restart needed.")
                return
            logger.info("Server credentials changed. Restarting agent service.")

        # Try to start the service with the obtained credentials from relation data
        self.charm.unit.status = ops.MaintenanceStatus("Starting jenkins agent service")
        try:
            self.jenkins_agent_service.restart()
        except service.ServiceRestartError as exc:
            logger.error("Error restarting the agent service %s", exc)
            raise RuntimeError("Error restarting the agent service.") from exc

        self.charm.unit.status = ops.ActiveStatus()

    def _on_agent_relation_departed(self, _: ops.RelationDepartedEvent) -> None:
        """Handle agent relation departed event."""
        try:
            self.jenkins_agent_service.reset()
        except service.ServiceStopError:
            self.charm.unit.status = ops.BlockedStatus("Error stopping the agent service")
            return
        self.charm.unit.status = ops.BlockedStatus("Waiting for config/relation.")
