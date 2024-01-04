#!/usr/bin/env python3

# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

"""Charm jenkins agent."""


import logging
import typing

import ops
from ops.main import main

import agent_observer
import service
from charm_state import AGENT_RELATION, InvalidStateError, State

logger = logging.getLogger()


class JenkinsAgentCharm(ops.CharmBase):
    """Charm Jenkins agent."""

    def __init__(self, *args: typing.Any):
        """Initialize the charm and register event handlers.

        Args:
            args: Arguments to initialize the charm base.
        """
        super().__init__(*args)
        try:
            self.state = State.from_charm(self)
        except InvalidStateError as e:
            logger.debug("Error parsing charm_state %s", e)
            self.unit.status = ops.BlockedStatus(e.msg)
            return

        self.jenkins_agent_service = service.JenkinsAgentService(self.state)
        self.agent_observer = agent_observer.Observer(self, self.state, self.jenkins_agent_service)

        self.framework.observe(self.on.install, self._on_install)
        self.framework.observe(self.on.start, self._on_start)
        self.framework.observe(self.on.config_changed, self._on_config_changed)
        self.framework.observe(self.on.upgrade_charm, self._on_upgrade_charm)

    def _on_install(self, _: ops.InstallEvent) -> None:
        """Handle install event, setup the agent service.

        Raises:
            RuntimeError: when the installation of the agent service fails
        """
        try:
            self.jenkins_agent_service.install()
        except service.PackageInstallError as exc:
            logger.error("Error installing the agent service %s", exc)
            raise RuntimeError("Error installing the agent service") from exc

    def _on_config_changed(self, _: ops.ConfigChangedEvent) -> None:
        """Handle config changed event. Update the agent's label in the relation's databag."""
        if agent_relation := self.model.get_relation(AGENT_RELATION):
            relation_data = self.state.agent_meta.model_dump()
            agent_relation.data[self.unit].update(relation_data)

    def _on_upgrade_charm(self, event: ops.UpgradeCharmEvent) -> None:
        """Handle upgrade charm event.

        Args:
            event: The event fired on upgrade charm.
        """
        self._restart(event)

    def _on_start(self, event: ops.EventBase) -> None:
        """Handle on start event.

        Args:
            event: The event fired on upgrade charm.
        """
        self._restart(event)

    def _restart(self, _: ops.EventBase) -> None:
        """Restart the jenkins agent charm.

        Raises:
            RuntimeError: when the service fails to properly start.
        """
        if not self.model.get_relation(AGENT_RELATION):
            self.model.unit.status = ops.BlockedStatus("Waiting for relation.")
            return

        if not self.state.agent_relation_credentials:
            self.model.unit.status = ops.WaitingStatus("Waiting for complete relation data.")
            logger.info("Waiting for complete relation data.")
            return

        self.model.unit.status = ops.MaintenanceStatus("Starting agent service.")
        try:
            self.jenkins_agent_service.restart()
        except service.ServiceRestartError as exc:
            logger.error("Error restarting the agent service %s", exc)
            raise RuntimeError("Error restarting the agent service") from exc

        self.model.unit.status = ops.ActiveStatus()


if __name__ == "__main__":  # pragma: no cover
    main(JenkinsAgentCharm)
