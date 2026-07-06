# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.
#
# Learn more about testing at: https://juju.is/docs/sdk/testing

"""Test for charm reconcile handler."""

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
        MagicMock(side_effect=[charm_state.InvalidStateError("Invalid executor message")]),
    )

    harness.begin()

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
    monkeypatch.setattr(service.JenkinsAgentService, "is_active", PropertyMock(return_value=True))
    harness.begin()

    harness.charm.on.update_status.emit()

    assert service_mocks.reset.call_count == 1
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

    charm: JenkinsAgentCharm = harness.charm
    monkeypatch.setattr(charm.state, "agent_relation_credentials", None)
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
    monkeypatch.setattr(service.JenkinsAgentService, "is_active", PropertyMock(return_value=True))
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
        == charm.state.agent_meta.as_dict()
    )
