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
        try:
            desired_state = State.from_charm(self)
        except InvalidStateError as exc:
            self.unit.status = ops.BlockedStatus(exc.msg)
            return

        desired_service = service.JenkinsAgentService(desired_state)
        credentials = desired_state.agent_relation_credentials
        if desired_service.is_running and desired_service.configuration_changed(credentials):
            try:
                desired_service.reset()
            except service.ServiceStopError as exc:
                raise RuntimeError("Error stopping the agent service") from exc

        service_files_changed = self._reconcile_installation(desired_service)
        self._reconcile_relation_data(desired_state)
        self._reconcile_service(
            desired_state, desired_service, service_files_changed=service_files_changed
        )

    def _reconcile_installation(self, agent_service: service.JenkinsAgentService) -> bool:
        """Ensure the agent package and service files are installed and current.

        Args:
            agent_service: The service built from the current desired state.

        Returns:
            Whether rendered service files changed and require a restart.

        Raises:
            RuntimeError: when the installation of the agent service fails.
        """
        try:
            return agent_service.install()
        except service.PackageInstallError as exc:
            logger.error("Error installing the agent service %s", exc)
            raise RuntimeError("Error installing the agent service") from exc

    def _reconcile_relation_data(self, state: State) -> None:
        """Publish agent metadata to the relation databag when related."""
        if agent_relation := self.model.get_relation(AGENT_RELATION):
            metadata = state.agent_meta.as_dict()
            if state.agent_meta.remote_fs is None:
                agent_relation.data[self.unit].pop("remote_fs", None)
            agent_relation.data[self.unit].update(metadata)

    def _reconcile_service(
        self,
        state: State,
        agent_service: service.JenkinsAgentService,
        service_files_changed: bool = False,
    ) -> None:
        """Converge the jenkins agent systemd service to the desired state.

        Args:
            state: The current desired charm state.
            agent_service: The service built from the current desired state.
            service_files_changed: Whether local service configuration changed.

        Raises:
            RuntimeError: when the service fails to properly start.
        """
        if not self.model.get_relation(AGENT_RELATION):
            if agent_service.is_running:
                try:
                    agent_service.reset()
                except service.ServiceStopError as exc:
                    raise RuntimeError("Error stopping the agent service") from exc
            self.unit.status = ops.BlockedStatus("Waiting for relation.")
            return

        credentials = state.agent_relation_credentials
        if not credentials:
            self.unit.status = ops.WaitingStatus("Waiting for complete relation data.")
            logger.info("Waiting for complete relation data.")
            return

        if (
            agent_service.is_ready
            and not service_files_changed
            and not agent_service.credentials_changed(credentials)
        ):
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
