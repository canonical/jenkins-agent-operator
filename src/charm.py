#!/usr/bin/env python3

# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Jenkins agent charm."""

import logging
import typing

import ops

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
        self.state = typing.cast(State, None)
        self.jenkins_agent_service = typing.cast(service.JenkinsAgentService, None)

        for event in (
            self.on.install,
            self.on.start,
            self.on.config_changed,
            self.on.upgrade_charm,
            self.on.update_status,
            self.on[AGENT_RELATION].relation_joined,
            self.on[AGENT_RELATION].relation_changed,
            self.on[AGENT_RELATION].relation_departed,
            self.on[AGENT_RELATION].relation_broken,
        ):
            self.framework.observe(event, self._reconcile)

    def _reconcile(self, _: ops.EventBase) -> None:
        """Reconcile the agent to its desired state on every event.

        Raises:
            RuntimeError: when installation or service start fails.
        """
        if self.state is None:
            try:
                self.state = State.from_charm(self)
            except InvalidStateError as exc:
                logger.debug("Error parsing charm_state %s", exc)
                self.unit.status = ops.BlockedStatus(exc.msg)
                return
        if self.jenkins_agent_service is None:
            self.jenkins_agent_service = service.JenkinsAgentService(self.state)

        self._reconcile_installation()
        self._reconcile_relation_data()
        self._reconcile_service()

    def _reconcile_installation(self) -> None:
        """Ensure the agent package and service files are installed and current.

        Raises:
            RuntimeError: when the installation of the agent service fails.
        """
        if self.jenkins_agent_service is None:
            raise RuntimeError("Agent service is not initialized")
        try:
            self.jenkins_agent_service.install()
        except service.PackageInstallError as exc:
            logger.error("Error installing the agent service %s", exc)
            raise RuntimeError("Error installing the agent service") from exc

    def _reconcile_relation_data(self) -> None:
        """Publish agent metadata to the relation databag when related."""
        if self.state is None:
            raise RuntimeError("Agent state is not initialized")
        if agent_relation := self.model.get_relation(AGENT_RELATION):
            agent_relation.data[self.unit].update(self.state.agent_meta.as_dict())

    def _reconcile_service(self) -> None:
        """Converge the jenkins agent systemd service to the desired state.

        Raises:
            RuntimeError: when the service fails to properly start.
        """
        if self.jenkins_agent_service is None or self.state is None:
            raise RuntimeError("Agent state is not initialized")
        agent_service = self.jenkins_agent_service
        if not self.model.get_relation(AGENT_RELATION):
            if agent_service.is_active:
                try:
                    agent_service.reset()
                except service.ServiceStopError:
                    self.unit.status = ops.BlockedStatus("Error stopping the agent service")
                    return
            self.unit.status = ops.BlockedStatus("Waiting for relation.")
            return

        credentials = self.state.agent_relation_credentials
        if not credentials:
            self.unit.status = ops.WaitingStatus("Waiting for complete relation data.")
            logger.info("Waiting for complete relation data.")
            return

        if agent_service.is_active and not agent_service.credentials_changed(credentials):
            logger.info("Agent running with current credentials. No restart needed.")
            agent_service.reset_failed_state()
            self.unit.status = ops.ActiveStatus()
            return

        self.unit.status = ops.MaintenanceStatus("Starting agent service.")
        try:
            agent_service.restart()
        except service.ServiceRestartError as exc:
            logger.error("Error restarting the agent service %s", exc)
            raise RuntimeError("Error restarting the agent service") from exc

        self.unit.status = ops.ActiveStatus()


if __name__ == "__main__":  # pragma: no cover
    ops.main(JenkinsAgentCharm)
