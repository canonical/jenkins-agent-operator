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
        except InvalidStateError as exc:
            self.unit.status = ops.BlockedStatus(exc.msg)
            return

        self.jenkins_agent_service = service.JenkinsAgentService(self.state)
        self.agent_observer = agent_observer.Observer(self, self.state, self.jenkins_agent_service)

        self.framework.observe(self.on.install, self._on_install)
        self.framework.observe(self.on.config_changed, self._on_config_changed)
        self.framework.observe(self.on.upgrade_charm, self._on_upgrade_charm)
        self.framework.observe(self.on.update_status, self._on_update_status)

    def _on_install(self, _: ops.InstallEvent) -> None:
        """Handle install event, setup the agent service."""
        try:
            self.jenkins_agent_service.install()
        except service.SnapInstallError as e:
            logger.debug("Error installing the agent service %s", e)
            self.unit.status = ops.BlockedStatus("Error installing the agent service")

    def _on_config_changed(self, event: ops.ConfigChangedEvent) -> None:
        """Handle config changed event. Update the agent's label in the relation's databag.

        Args:
            event: The event fired on configuration change.
        """
        self.reconcile(event)

    def _on_upgrade_charm(self, event: ops.UpgradeCharmEvent) -> None:
        """Handle upgrade charm event.

        Args:
            event: The event fired on upgrade charm.
        """
        self.reconcile(event)

    def reconcile(self, _: ops.EventBase):
        """Reconciliation for the jenkins agent charm."""
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
        except service.SnapServiceStartError as e:
            logger.debug("Error restarting the agent service %s", e)
            self.model.unit.status = ops.BlockedStatus("Readiness check failed")
        self.model.unit.status = ops.ActiveStatus()

    def _on_update_status(self, _: ops.UpdateStatusEvent) -> None:
        """Check status of the snap and report back to juju."""
        if self.jenkins_agent_service.is_active:
            self.unit.status = ops.ActiveStatus()
        else:
            self.unit.status = ops.BlockedStatus("Agent service not active")


if __name__ == "__main__":  # pragma: no cover
    main(JenkinsAgentCharm)
