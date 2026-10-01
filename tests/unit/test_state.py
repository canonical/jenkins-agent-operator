# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.
#
# Learn more about testing at: https://juju.is/docs/sdk/testing

"""Test for charm state."""

import os
from unittest.mock import MagicMock

import ops
import ops.testing
import pytest

import charm_state


def test_agent_meta_normalizes_comma_separated_labels_for_jenkins():
    """Relation metadata preserves the documented comma-separated label format."""
    metadata = charm_state.AgentMeta(
        executors=1,
        labels="ownership-upgrade,migration-test",
        name="agent",
    )

    assert metadata.as_dict()["labels"] == "ownership-upgrade,migration-test"


@pytest.mark.parametrize(
    ("configured_executors", "cpu_count", "expected"),
    [(3, 8, 3), (0, 4, 4)],
)
def test_from_charm_uses_configured_or_cpu_count(
    harness: ops.testing.Harness,
    service_mocks,
    monkeypatch: pytest.MonkeyPatch,
    configured_executors: int,
    cpu_count: int,
    expected: int,
):
    """Use the configured count or fall back to the host CPU count for zero."""
    monkeypatch.setattr(os, "cpu_count", MagicMock(return_value=cpu_count))
    harness.update_config({"jenkins_agent_executors": configured_executors})
    harness.begin()

    assert charm_state.State.from_charm(harness.charm).agent_meta.executors == expected


@pytest.mark.parametrize(
    ("config", "cpu_count"),
    [({"jenkins_agent_executors": -1}, 8), ({}, 0)],
)
def test_from_charm_rejects_invalid_executor_count(
    harness: ops.testing.Harness,
    service_mocks,
    monkeypatch: pytest.MonkeyPatch,
    config: dict,
    cpu_count: int,
):
    """Reject configured or host executor counts that cannot be used by Jenkins."""
    monkeypatch.setattr(os, "cpu_count", MagicMock(return_value=cpu_count))
    harness.update_config(config)
    harness.begin()

    with pytest.raises(charm_state.InvalidStateError, match=r"Invalid executor state\."):
        charm_state.State.from_charm(harness.charm)


@pytest.mark.parametrize(
    "config",
    [
        {"agent_user": "bad/user"},
        {"agent_user": "bad user"},
        {"jenkins_home": "relative/path"},
        {"jenkins_home": "/"},
        {"jenkins_home": "/var/lib/../etc/jenkins"},
    ],
)
def test_from_charm_rejects_unsafe_agent_configuration(harness: ops.testing.Harness, config: dict):
    """Reject user/home values before they reach privileged templates."""
    harness.update_config(config)
    harness.begin()

    with pytest.raises(charm_state.InvalidStateError, match=r"Invalid agent configuration"):
        charm_state.State.from_charm(charm=harness.charm)


def test_agent_meta_uses_configured_jenkins_home_as_remote_fs(
    harness: ops.testing.Harness,
    service_mocks,
):
    """Publish the configured agent home for Jenkins node workspace setup."""
    harness.update_config({"jenkins_home": "/srv/jenkins-agent"})
    harness.begin()
    harness.charm.on.install.emit()

    assert (
        charm_state.State.from_charm(harness.charm).agent_meta.as_dict()["remote_fs"]
        == "/srv/jenkins-agent"
    )


def test_default_jenkins_home_omits_remote_fs_relation_metadata(
    harness: ops.testing.Harness,
    service_mocks,
):
    """The fallback local home must not claim controller remoteFS ownership."""
    harness.begin()
    harness.charm.on.install.emit()

    metadata = charm_state.State.from_charm(harness.charm).agent_meta.as_dict()
    assert "remote_fs" not in metadata
