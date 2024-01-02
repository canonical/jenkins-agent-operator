#!/usr/bin/env python3

# Copyright 2023 Canonical Ltd.
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
        self.framework.observe(self.on.start, self.reconcile)
        self.framework.observe(self.on.config_changed, self._on_config_changed)
        self.framework.observe(self.on.upgrade_charm, self._on_upgrade_charm)
        self.framework.observe(self.on.update_status, self._on_update_status)

    def _on_install(self, _: ops.InstallEvent) -> None:
        """Handle install event, setup the agent service."""
        try:
            self.jenkins_agent_service.install()
        except service.PackageInstallError as e:
            logger.debug("Error installing the agent service %s", e)
            self.unit.status = ops.ErrorStatus("Error installing the agent service")

    def _on_config_changed(self, _: ops.ConfigChangedEvent) -> None:
        """Handle config changed event. Update the agent's label in the relation's databag."""
        if agent_relation := self.model.get_relation(AGENT_RELATION):
            agent_meta = self.state.agent_meta
            relation_data = {
                "executors": str(agent_meta.num_executors),
                "labels": agent_meta.labels,
                "name": agent_meta.name,
            }
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
        """Restart the jenkins agent charm."""
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
        except service.ServiceRestartError as e:
            logger.debug("Error restarting the agent service %s", e)
            self.model.unit.status = ops.BlockedStatus("Readiness check failed")
            return

        # TODO: handle cases where downloading the binary takes a long time
        self.model.unit.status = ops.WaitingStatus("Waiting for agent service to be up.")
        if not self.jenkins_agent_service.readiness_check():
            self.model.unit.status = ops.BlockedStatus("Readiness check failed.")
            return
        self.model.unit.status = ops.ActiveStatus()

    def _on_update_status(self, _: ops.UpdateStatusEvent) -> None:
        """Check status of the charm and report back to juju."""
        logger.debug(
            "Jenkins agent service is currently up: %s", self.jenkins_agent_service.is_active
        )
        if self.jenkins_agent_service.is_active:
            self.unit.status = ops.ActiveStatus()


if __name__ == "__main__":  # pragma: no cover
    main(JenkinsAgentCharm)
