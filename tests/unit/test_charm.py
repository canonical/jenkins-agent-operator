# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.
#
# Learn more about testing at: https://juju.is/docs/sdk/testing

"""Test for charm reconcile handler."""

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, PropertyMock

import ops
import ops.testing
import pytest

import charm_state
import service

if TYPE_CHECKING:
    from charm import JenkinsAgentCharm


def test___init___invalid_state(harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch):
    """
    arrange: patched State.from_charm that raises an InvalidState Error.
    act: when the JenkinsAgentCharm is initialized.
    assert: The agent falls into BlockedStatus.
    """
    monkeypatch.setattr(
        charm_state.State,
        "from_charm",
        MagicMock(side_effect=charm_state.InvalidStateError("Invalid executor message")),
    )

    harness.begin()
    harness.charm.on.install.emit()

    charm: JenkinsAgentCharm = harness.charm
    assert charm.unit.status.name == ops.BlockedStatus.name
    assert charm.unit.status.message == "Invalid executor message"


def test_reconcile_installs_on_every_hook(
    harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch
):
    """
    arrange: a charm with a mocked agent service install.
    act: emit the install hook to trigger reconcile.
    assert: the agent service install method is called once.
    """
    install_mock = MagicMock()
    monkeypatch.setattr(service.JenkinsAgentService, "install", install_mock)
    harness.begin()

    charm: JenkinsAgentCharm = harness.charm
    charm.on.install.emit()

    assert install_mock.call_count == 1


def test_reconcile_reinstalls_on_upgrade(
    harness: ops.testing.Harness, service_mocks: SimpleNamespace
):
    """
    arrange: an installed charm.
    act: emit the upgrade-charm hook to trigger reconcile.
    assert: the agent service install method is called so upgraded files sync.
    """
    harness.begin()

    harness.charm.on.upgrade_charm.emit()

    assert service_mocks.install.call_count == 1


def test_reconcile_install_error(harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch):
    """
    arrange: a charm whose install raises PackageInstallError.
    act: emit the install hook to trigger reconcile.
    assert: reconcile raises RuntimeError.
    """
    monkeypatch.setattr(
        service.JenkinsAgentService,
        "install",
        MagicMock(side_effect=service.PackageInstallError),
    )
    harness.begin()

    with pytest.raises(RuntimeError, match=r"Error installing the agent service"):
        harness.charm.on.install.emit()


def test_reconcile_no_relation_blocked(
    harness: ops.testing.Harness, service_mocks: SimpleNamespace
):
    """
    arrange: an installed charm without a relation to jenkins.
    act: emit the update-status hook to trigger reconcile.
    assert: the charm is blocked waiting for a relation.
    """
    harness.begin()

    harness.charm.on.update_status.emit()

    assert harness.charm.unit.status.name == ops.BlockedStatus.name
    assert harness.charm.unit.status.message == "Waiting for relation."


def test_reconcile_no_relation_stops_active_service(
    harness: ops.testing.Harness,
    monkeypatch: pytest.MonkeyPatch,
    service_mocks: SimpleNamespace,
):
    """
    arrange: an installed charm with an active service but no relation.
    act: emit the update-status hook to trigger reconcile.
    assert: the charm stops the service and is blocked waiting for a relation.
    """
    monkeypatch.setattr(service.JenkinsAgentService, "is_ready", PropertyMock(return_value=True))
    monkeypatch.setattr(service.JenkinsAgentService, "is_running", PropertyMock(return_value=True))
    harness.begin()

    harness.charm.on.update_status.emit()

    assert service_mocks.reset.call_count >= 1
    assert harness.charm.unit.status.name == ops.BlockedStatus.name


def test_reconcile_incomplete_credentials_waiting(
    harness_with_agent_relation: ops.testing.Harness,
    monkeypatch: pytest.MonkeyPatch,
    service_mocks: SimpleNamespace,
):
    """
    arrange: an installed charm related to jenkins but without complete credentials.
    act: emit the update-status hook to trigger reconcile.
    assert: the charm waits for complete relation data.
    """
    harness = harness_with_agent_relation
    harness.begin()
    harness.charm.on.install.emit()

    charm: JenkinsAgentCharm = harness.charm
    incomplete_state = replace(
        charm_state.State.from_charm(charm), agent_relation_credentials=None
    )
    monkeypatch.setattr(charm_state.State, "from_charm", lambda charm: incomplete_state)
    charm.on.update_status.emit()

    assert charm.unit.status.name == ops.WaitingStatus.name


def test_reconcile_restart_error(
    harness_with_agent_relation: ops.testing.Harness,
    service_mocks: SimpleNamespace,
):
    """
    arrange: an installed charm related to jenkins whose service restart fails.
    act: emit the update-status hook to trigger reconcile.
    assert: reconcile raises RuntimeError.
    """
    service_mocks.restart.side_effect = service.ServiceRestartError
    harness = harness_with_agent_relation
    harness.begin()

    with pytest.raises(RuntimeError, match=r"Error restarting the agent service"):
        harness.charm.on.update_status.emit()


def test_reconcile_active_resets_failed_state(
    harness_with_agent_relation: ops.testing.Harness,
    monkeypatch: pytest.MonkeyPatch,
    service_mocks: SimpleNamespace,
):
    """
    arrange: an installed charm related to jenkins with an active, unchanged service.
    act: emit the update-status hook to trigger reconcile.
    assert: the charm resets the failed state and becomes active.
    """
    monkeypatch.setattr(service.JenkinsAgentService, "is_ready", PropertyMock(return_value=True))
    service_mocks.credentials_changed.return_value = False
    harness = harness_with_agent_relation
    harness.begin()

    harness.charm.on.update_status.emit()

    assert service_mocks.reset_failed_state.call_count == 1
    assert harness.charm.unit.status.name == ops.ActiveStatus.name


def test_reconcile_config_changed_updates_databag(
    harness_with_agent_relation: ops.testing.Harness, service_mocks: SimpleNamespace
):
    """
    arrange: an installed charm related to jenkins.
    act: emit the config-changed hook to trigger reconcile.
    assert: the charm publishes agent metadata to the relation databag.
    """
    harness = harness_with_agent_relation
    harness.begin()

    charm: JenkinsAgentCharm = harness.charm
    relation = harness.model.get_relation(charm_state.AGENT_RELATION)
    assert relation
    charm.on.config_changed.emit()

    assert (
        harness.get_relation_data(relation.id, app_or_unit="jenkins-agent/0")
        == charm_state.State.from_charm(charm).agent_meta.as_dict()
    )


def test_invalid_config_remains_blocked_across_reconcile_events(
    harness_with_agent_relation: ops.testing.Harness,
    monkeypatch: pytest.MonkeyPatch,
    service_mocks: SimpleNamespace,
):
    """Do not resume the old valid state while invalid config persists."""
    monkeypatch.setattr(service.JenkinsAgentService, "is_ready", PropertyMock(return_value=True))
    monkeypatch.setattr(service.JenkinsAgentService, "is_running", PropertyMock(return_value=True))
    harness = harness_with_agent_relation
    harness.begin()
    harness.charm.on.install.emit()
    harness.update_config({"agent_user": "bad/user"})
    harness.charm.on.config_changed.emit()

    assert harness.charm.unit.status.name == ops.BlockedStatus.name
    assert service_mocks.reset.call_count >= 1

    harness.charm.on.update_status.emit()
    assert harness.charm.unit.status.name == ops.BlockedStatus.name


def test_invalid_config_reset_error_blocks_service(
    harness_with_agent_relation: ops.testing.Harness,
    service_mocks: SimpleNamespace,
):
    """Invalid desired configuration remains blocked without a service object."""
    harness = harness_with_agent_relation
    harness.begin()
    harness.update_config({"agent_user": "bad/user"})
    harness.charm.on.config_changed.emit()

    assert harness.charm.unit.status.name == ops.BlockedStatus.name


def test_service_configuration_reset_error_blocks_service(
    harness_with_agent_relation: ops.testing.Harness,
    monkeypatch: pytest.MonkeyPatch,
    service_mocks: SimpleNamespace,
):
    """Surface a stop failure while applying a valid service configuration change."""
    monkeypatch.setattr(service.JenkinsAgentService, "is_ready", PropertyMock(return_value=True))
    monkeypatch.setattr(service.JenkinsAgentService, "is_running", PropertyMock(return_value=True))
    service_mocks.reset.side_effect = service.ServiceStopError
    harness = harness_with_agent_relation
    harness.begin()
    with pytest.raises(RuntimeError, match="Error stopping the agent service"):
        harness.update_config({"jenkins_home": "/srv/jenkins-agent"})


def test_reconcile_blocks_until_runtime_ownership_action(
    harness_with_agent_relation: ops.testing.Harness,
    monkeypatch: pytest.MonkeyPatch,
    service_mocks: SimpleNamespace,
):
    """
    Arrange: relation credentials are complete but runtime ownership is not usable.
    Act: reconcile the charm.
    Assert: the service stays stopped and instructs the operator to run the action.
    """
    monkeypatch.setattr(
        service.JenkinsAgentService,
        "runtime_directories_usable",
        MagicMock(return_value=False),
    )
    harness = harness_with_agent_relation
    harness.begin()
    harness.charm.on.update_status.emit()

    assert harness.charm.unit.status.name == ops.BlockedStatus.name
    assert harness.charm.unit.status.message == (
        "Run the migrate-runtime-directory action to repair legacy runtime ownership."
    )
    service_mocks.restart.assert_not_called()


def test_migrate_runtime_directory_action_defaults_to_configured_home(
    harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """
    Arrange: a charm with the default agent user and Jenkins home.
    Act: run the ownership-migration action without a directory parameter.
    Assert: the configured service user and default Jenkins home are passed to the service,
    and the service resumes after migration.
    """
    migration_mock = MagicMock()
    restart_mock = MagicMock()
    monkeypatch.setattr(service.JenkinsAgentService, "migrate_directory", migration_mock)
    monkeypatch.setattr(service.JenkinsAgentService, "restart", restart_mock)
    monkeypatch.setattr(
        service.JenkinsAgentService, "is_running", PropertyMock(return_value=False)
    )
    home = tmp_path / "jenkins-home"
    home.mkdir()
    harness.update_config({"jenkins_home": str(home)})
    harness.begin()

    output = harness.run_action("migrate-runtime-directory")

    migration_mock.assert_called_once_with(home)
    assert output.results["directory"] == str(home)
    assert output.results["user"] == "jenkins"
    assert output.results["service-restarted"] is True
    assert output.results["message"] == "Directory ownership migrated in place"
    restart_mock.assert_called_once_with()


def test_migrate_runtime_directory_action_uses_requested_subdirectory(
    harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """
    Arrange: a charm configured with a Jenkins home.
    Act: run the action with a directory under that home.
    Assert: the requested directory is passed to the ownership migration and the service resumes.
    """
    migration_mock = MagicMock()
    restart_mock = MagicMock()
    monkeypatch.setattr(service.JenkinsAgentService, "migrate_directory", migration_mock)
    monkeypatch.setattr(service.JenkinsAgentService, "restart", restart_mock)
    monkeypatch.setattr(
        service.JenkinsAgentService, "is_running", PropertyMock(return_value=False)
    )
    home = tmp_path / "jenkins-home"
    workspace = home / "workspace"
    workspace.mkdir(parents=True)
    harness.update_config({"jenkins_home": str(home)})
    harness.begin()

    output = harness.run_action("migrate-runtime-directory", {"directory": str(workspace)})

    migration_mock.assert_called_once_with(workspace)
    assert output.results["directory"] == str(workspace)
    assert output.results["user"] == "jenkins"
    assert output.results["service-restarted"] is True
    restart_mock.assert_called_once_with()


@pytest.mark.parametrize(
    ("directory", "error"),
    [
        ("relative/path", "absolute path"),
        ("../etc", "must not contain '..'"),
        ("/", "must not be the filesystem root"),
        ("/etc", "under the configured Jenkins home"),
    ],
)
def test_migrate_runtime_directory_action_rejects_unsafe_path(
    harness: ops.testing.Harness,
    monkeypatch: pytest.MonkeyPatch,
    directory: str,
    error: str,
):
    """
    Arrange: a charm with the default Jenkins home.
    Act: run the action with an unsafe directory path.
    Assert: the action fails without calling the migration service.
    """
    migration_mock = MagicMock()
    monkeypatch.setattr(service.JenkinsAgentService, "migrate_directory", migration_mock)
    monkeypatch.setattr(
        service.JenkinsAgentService, "is_running", PropertyMock(return_value=False)
    )
    harness.begin()

    with pytest.raises(ops.testing.ActionFailed, match=error):
        harness.run_action("migrate-runtime-directory", {"directory": directory})

    migration_mock.assert_not_called()


def test_migrate_runtime_directory_action_restarts_running_service(
    harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """
    Arrange: the agent service is running and the target home exists.
    Act: run the ownership-migration action.
    Assert: the service is stopped before migration and restarted afterward.
    """
    migration_mock = MagicMock()
    reset_mock = MagicMock()
    restart_mock = MagicMock()
    monkeypatch.setattr(service.JenkinsAgentService, "migrate_directory", migration_mock)
    monkeypatch.setattr(service.JenkinsAgentService, "reset", reset_mock)
    monkeypatch.setattr(service.JenkinsAgentService, "restart", restart_mock)
    monkeypatch.setattr(service.JenkinsAgentService, "is_running", PropertyMock(return_value=True))
    home = tmp_path / "jenkins-home"
    home.mkdir()
    harness.update_config({"jenkins_home": str(home)})
    harness.begin()

    output = harness.run_action("migrate-runtime-directory")

    migration_mock.assert_called_once_with(home)
    reset_mock.assert_called_once_with()
    restart_mock.assert_called_once_with()
    assert output.results["directory"] == str(home)
    assert output.results["user"] == "jenkins"
    assert output.results["service-restarted"] is True


def test_migrate_runtime_directory_action_rejects_symlink_parent(
    harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """
    Arrange: the requested path contains a symlink under Jenkins home.
    Act: run the ownership-migration action.
    Assert: the action fails without resolving the symlink target.
    """
    migration_mock = MagicMock()
    monkeypatch.setattr(service.JenkinsAgentService, "migrate_directory", migration_mock)
    monkeypatch.setattr(
        service.JenkinsAgentService, "is_running", PropertyMock(return_value=False)
    )
    home = tmp_path / "jenkins-home"
    home.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (home / "link").symlink_to(outside, target_is_directory=True)
    harness.update_config({"jenkins_home": str(home)})
    harness.begin()

    with pytest.raises(ops.testing.ActionFailed, match="symbolic link"):
        harness.run_action(
            "migrate-runtime-directory", {"directory": str(home / "link" / "workspace")}
        )

    migration_mock.assert_not_called()


def test_migrate_runtime_directory_action_reports_service_error(
    harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """
    Arrange: the ownership service reports an installation error.
    Act: run the ownership-migration action.
    Assert: the action exposes the failure to the operator.
    """
    migration_mock = MagicMock(side_effect=service.RuntimeDirectoryError("migration failed"))
    monkeypatch.setattr(service.JenkinsAgentService, "migrate_directory", migration_mock)
    monkeypatch.setattr(
        service.JenkinsAgentService, "is_running", PropertyMock(return_value=False)
    )
    home = tmp_path / "jenkins-home"
    home.mkdir()
    harness.update_config({"jenkins_home": str(home)})
    harness.begin()

    with pytest.raises(ops.testing.ActionFailed, match="migration failed"):
        harness.run_action("migrate-runtime-directory")


def test_migrate_runtime_directory_action_rejects_missing_directory(
    harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """
    Arrange: the requested directory does not exist under Jenkins home.
    Act: run the ownership-migration action.
    Assert: the action fails before attempting ownership changes.
    """
    migration_mock = MagicMock()
    monkeypatch.setattr(service.JenkinsAgentService, "migrate_directory", migration_mock)
    monkeypatch.setattr(
        service.JenkinsAgentService, "is_running", PropertyMock(return_value=False)
    )
    home = tmp_path / "jenkins-home"
    home.mkdir()
    harness.update_config({"jenkins_home": str(home)})
    harness.begin()

    with pytest.raises(ops.testing.ActionFailed, match="existing directory"):
        harness.run_action("migrate-runtime-directory", {"directory": str(home / "missing")})

    migration_mock.assert_not_called()


def test_migrate_runtime_directory_action_rejects_symlink_home(
    harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """
    Arrange: the configured Jenkins home is a symbolic link.
    Act: run the ownership-migration action.
    Assert: the action refuses to resolve the configured home.
    """
    migration_mock = MagicMock()
    monkeypatch.setattr(service.JenkinsAgentService, "migrate_directory", migration_mock)
    real_home = tmp_path / "real-home"
    real_home.mkdir()
    home_link = tmp_path / "jenkins-home"
    home_link.symlink_to(real_home, target_is_directory=True)
    harness.update_config({"jenkins_home": str(home_link)})
    harness.begin()

    with pytest.raises(ops.testing.ActionFailed, match="symbolic link"):
        harness.run_action("migrate-runtime-directory")

    migration_mock.assert_not_called()


def test_migrate_runtime_directory_action_reports_resolution_error(
    harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """
    Arrange: resolving the configured action path reports an operating-system error.
    Act: run the ownership-migration action.
    Assert: the action fails with a controlled resolution error.
    """
    migration_mock = MagicMock()
    monkeypatch.setattr(service.JenkinsAgentService, "migrate_directory", migration_mock)
    monkeypatch.setattr(Path, "resolve", MagicMock(side_effect=OSError("resolve failed")))
    home = tmp_path / "jenkins-home"
    home.mkdir()
    harness.update_config({"jenkins_home": str(home)})
    harness.begin()

    with pytest.raises(ops.testing.ActionFailed, match="Unable to resolve directory"):
        harness.run_action("migrate-runtime-directory")

    migration_mock.assert_not_called()


def test_migrate_runtime_directory_action_rejects_resolved_path_outside_home(
    harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """
    Arrange: resolving the requested path places it outside Jenkins home.
    Act: run the ownership-migration action.
    Assert: the action rejects the resolved path.
    """
    migration_mock = MagicMock()
    monkeypatch.setattr(service.JenkinsAgentService, "migrate_directory", migration_mock)
    home = tmp_path / "jenkins-home"
    workspace = home / "workspace"
    workspace.mkdir(parents=True)

    def resolve(path: Path, *, strict: bool = False) -> Path:
        return home if path == home else Path("/outside")

    monkeypatch.setattr(Path, "resolve", resolve)
    harness.update_config({"jenkins_home": str(home)})
    harness.begin()

    with pytest.raises(ops.testing.ActionFailed, match="under the configured Jenkins home"):
        harness.run_action("migrate-runtime-directory", {"directory": str(workspace)})

    migration_mock.assert_not_called()


def test_migrate_runtime_directory_action_leaves_service_stopped_on_migration_error(
    harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """
    Arrange: the running service stops successfully but migration fails.
    Act: run the ownership-migration action.
    Assert: the service is not restarted and the action reports the migration error.
    """
    reset_mock = MagicMock()
    migration_mock = MagicMock(side_effect=service.RuntimeDirectoryError("migration failed"))
    restart_mock = MagicMock()
    monkeypatch.setattr(service.JenkinsAgentService, "reset", reset_mock)
    monkeypatch.setattr(service.JenkinsAgentService, "migrate_directory", migration_mock)
    monkeypatch.setattr(service.JenkinsAgentService, "restart", restart_mock)
    monkeypatch.setattr(service.JenkinsAgentService, "is_running", PropertyMock(return_value=True))
    home = tmp_path / "jenkins-home"
    home.mkdir()
    harness.update_config({"jenkins_home": str(home)})
    harness.begin()

    with pytest.raises(ops.testing.ActionFailed, match="migration failed"):
        harness.run_action("migrate-runtime-directory")

    reset_mock.assert_called_once_with()
    restart_mock.assert_not_called()


def test_migrate_runtime_directory_action_reports_stop_error(
    harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """
    Arrange: the running service cannot be stopped.
    Act: run the ownership-migration action.
    Assert: migration is not attempted and the stop error is reported.
    """
    reset_mock = MagicMock(side_effect=service.ServiceStopError("stop failed"))
    migration_mock = MagicMock()
    monkeypatch.setattr(service.JenkinsAgentService, "reset", reset_mock)
    monkeypatch.setattr(service.JenkinsAgentService, "migrate_directory", migration_mock)
    monkeypatch.setattr(service.JenkinsAgentService, "is_running", PropertyMock(return_value=True))
    home = tmp_path / "jenkins-home"
    home.mkdir()
    harness.update_config({"jenkins_home": str(home)})
    harness.begin()

    with pytest.raises(ops.testing.ActionFailed, match="Error stopping the agent service"):
        harness.run_action("migrate-runtime-directory")

    migration_mock.assert_not_called()


def test_migrate_runtime_directory_action_reports_restart_error(
    harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """
    Arrange: migration succeeds but the original service cannot restart.
    Act: run the ownership-migration action.
    Assert: the action reports the restart error and attempts to stop again.
    """
    reset_mock = MagicMock()
    migration_mock = MagicMock()
    restart_mock = MagicMock(side_effect=service.ServiceRestartError("restart failed"))
    monkeypatch.setattr(service.JenkinsAgentService, "reset", reset_mock)
    monkeypatch.setattr(service.JenkinsAgentService, "migrate_directory", migration_mock)
    monkeypatch.setattr(service.JenkinsAgentService, "restart", restart_mock)
    monkeypatch.setattr(service.JenkinsAgentService, "is_running", PropertyMock(return_value=True))
    home = tmp_path / "jenkins-home"
    home.mkdir()
    harness.update_config({"jenkins_home": str(home)})
    harness.begin()

    with pytest.raises(ops.testing.ActionFailed, match="Error restarting the agent service"):
        harness.run_action("migrate-runtime-directory")

    assert reset_mock.call_count == 2
    migration_mock.assert_called_once_with(home)
    restart_mock.assert_called_once_with()
