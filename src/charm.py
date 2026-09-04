#!/usr/bin/env python3

# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Jenkins agent charm."""

import logging
import typing
from pathlib import Path

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
        self.framework.observe(
            self.on.migrate_runtime_directory_action,
            self._on_migrate_runtime_directory_action,
        )
        self.framework.observe(self.on.upgrade_charm, self._on_upgrade_charm)
        for event in (
            self.on.install,
            self.on.start,
            self.on.config_changed,
            self.on.update_status,
            self.on[AGENT_RELATION].relation_joined,
            self.on[AGENT_RELATION].relation_changed,
            self.on[AGENT_RELATION].relation_departed,
            self.on[AGENT_RELATION].relation_broken,
        ):
            self.framework.observe(event, self._reconcile)

    @staticmethod
    def _path_contains_symlink(path: Path) -> bool:
        """Return whether any existing component of an absolute path is a symlink."""
        current = Path(path.anchor)
        for part in path.parts[1:]:
            current /= part
            if current.is_symlink():
                return True
        return False

    def _action_directory(self, state: State, event: ops.ActionEvent) -> Path:
        """Validate and resolve the optional ownership-action directory."""
        requested = str(event.params.get("directory") or state.jenkins_home)
        directory = Path(requested)
        home = state.jenkins_home
        if ".." in directory.parts:
            raise ValueError("directory must not contain '..'")
        if not directory.is_absolute():
            raise ValueError("directory must be an absolute path")
        if directory == Path("/"):
            raise ValueError("directory must not be the filesystem root")
        if self._path_contains_symlink(home) or self._path_contains_symlink(directory):
            raise ValueError(f"directory {directory} contains a symbolic link")

        try:
            resolved_home = home.resolve(strict=False)
            resolved_directory = directory.resolve(strict=False)
        except OSError as exc:
            raise ValueError(f"Unable to resolve directory {directory}") from exc
        if resolved_directory != resolved_home and resolved_home not in resolved_directory.parents:
            raise ValueError("directory must be under the configured Jenkins home")
        if not directory.exists() or not directory.is_dir():
            raise ValueError(f"directory {directory} must be an existing directory")
        return directory

    def _on_migrate_runtime_directory_action(self, event: ops.ActionEvent) -> None:
        """Migrate an operator-selected Jenkins directory to the service user."""
        service_restarted = False
        try:
            state = State.from_charm(self)
            directory = self._action_directory(state, event)
            agent_service = service.JenkinsAgentService(state)
            was_running = agent_service.is_running
            if was_running:
                try:
                    agent_service.reset()
                except service.ServiceStopError as exc:
                    raise RuntimeError("Error stopping the agent service") from exc
            try:
                agent_service.migrate_directory(directory)
            except service.RuntimeDirectoryError:
                # Leave a previously running service stopped when migration fails.
                raise
            if state.agent_relation_credentials:
                unsafe_directories = agent_service.unsafe_runtime_directories()
                if unsafe_directories:
                    raise service.RuntimeDirectoryError(
                        "Runtime directories remain unsafe: "
                        f"{', '.join(unsafe_directories)}. Migrate the complete Jenkins home "
                        "or run the action for each unsafe directory."
                    )
                # A successful repair resumes the desired service state, including
                # services that were already stopped when the action began.
                try:
                    agent_service.restart()
                except service.ServiceRestartError as exc:
                    try:
                        agent_service.reset()
                    except service.ServiceStopError:
                        logger.exception("Failed to leave the agent service stopped")
                    raise RuntimeError("Error restarting the agent service") from exc
                service_restarted = True
        except (InvalidStateError, RuntimeError, ValueError, service.RuntimeDirectoryError) as exc:
            event.fail(str(exc))
            return
        message = "Directory ownership migrated in place"
        if not service_restarted:
            message += (
                "; service remains stopped and will start on the next reconciliation "
                "when prerequisites are ready"
            )
        event.set_results(
            {
                "directory": str(directory),
                "user": state.agent_user,
                "service-restarted": service_restarted,
                "message": message,
            }
        )

    def _on_upgrade_charm(self, event: ops.UpgradeCharmEvent) -> None:
        """Reconcile an upgrade and repair legacy runtime ownership when needed."""
        self._reconcile(event, automatic_runtime_migration=True)

    def _block_automatic_runtime_migration(self, error: Exception) -> None:
        """Set actionable blocked status after automatic migration cannot proceed."""
        logger.error("Automatic runtime ownership migration failed: %s", error)
        if isinstance(error, service.ServiceStopError):
            guidance = (
                "Fix the service stop failure, then retry the upgrade or run the "
                "migrate-runtime-directory action."
            )
        else:
            guidance = (
                "Fix the reported filesystem issue, then run the migrate-runtime-directory action."
            )
        self.unit.status = ops.BlockedStatus(
            f"Automatic runtime ownership migration failed: {error}. {guidance}"
        )

    def _is_automatic_runtime_migration_successful(
        self, agent_service: service.JenkinsAgentService
    ) -> bool:
        """Repair known legacy runtime directories before an upgraded service starts."""
        # UpgradeCharmEvent does not expose the previous charm revision. Unsafe
        # runtime ownership is the observable legacy signature left by revision 265.
        if agent_service.state.agent_user == "root":
            return True
        if agent_service.runtime_directories_usable():
            return True

        try:
            if agent_service.is_running:
                agent_service.reset()
            agent_service.migrate_runtime_directories()
        except (service.RuntimeDirectoryError, service.ServiceStopError) as exc:
            self._block_automatic_runtime_migration(exc)
            return False
        return True

    def _reconcile(self, _: ops.EventBase, *, automatic_runtime_migration: bool = False) -> None:
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
                if automatic_runtime_migration:
                    self._block_automatic_runtime_migration(exc)
                    return
                raise RuntimeError("Error stopping the agent service") from exc

        service_files_changed = self._reconcile_installation(desired_service)
        if automatic_runtime_migration and not self._is_automatic_runtime_migration_successful(
            desired_service
        ):
            return
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

        if not agent_service.runtime_directories_usable():
            self.unit.status = ops.BlockedStatus(
                "Run the migrate-runtime-directory action to repair legacy runtime ownership."
            )
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
