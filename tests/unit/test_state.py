# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.
#
# Learn more about testing at: https://juju.is/docs/sdk/testing

"""Test for charm state."""

import os
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import ops
import ops.testing
import pytest

import charm_state

if TYPE_CHECKING:
    from charm import JenkinsAgentCharm


def test_from_charm_invalid_metadata(
    harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch
):
    """
    arrange: patched os.cpu_count method returning invalid number of executors.
    act: when the charm is initialized.
    assert: The charm goes into Error state.
    """
    monkeypatch.setattr(os, "cpu_count", MagicMock(return_value=0))
    harness.begin()
    charm: JenkinsAgentCharm = harness.charm

    with pytest.raises(charm_state.InvalidStateError, match=r"Invalid executor state\."):
        charm_state.State.from_charm(charm=charm)


@pytest.mark.parametrize(
    "config",
    [
        {"agent_user": "bad/user"},
        {"agent_user": "bad user"},
        {"jenkins_home": "relative/path"},
        {"jenkins_home": "/"},
        {"jenkins_home": "/var/lib/../etc/jenkins"},
        {"jenkins_home": "/etc"},
        {"jenkins_home": "/etc/jenkins"},
        {"jenkins_home": "/usr"},
        {"jenkins_home": "/usr/local/jenkins"},
        {"jenkins_home": "/var"},
        {"jenkins_home": "/var/lib"},
        {"jenkins_home": "/var/lib/dpkg"},
        {"jenkins_home": "/var/lib/juju"},
        {"jenkins_home": "/var/lib/jenkins-agent"},
        {"jenkins_home": "/srv"},
        {"jenkins_home": f"{os.sep}tmp/jenkins"},
    ],
)
def test_from_charm_rejects_unsafe_agent_configuration(harness: ops.testing.Harness, config: dict):
    """Reject user/home values before they reach privileged templates."""
    harness.update_config(config)
    harness.begin()

    with pytest.raises(charm_state.InvalidStateError, match=r"Invalid agent configuration"):
        charm_state.State.from_charm(charm=harness.charm)


@pytest.mark.parametrize(
    "jenkins_home", ["/home/jenkins", "/opt/jenkins", "/data/jenkins", "/srv/jenkins"]
)
def test_from_charm_accepts_existing_dedicated_home_paths(
    harness: ops.testing.Harness, jenkins_home: str
):
    """Keep previously valid dedicated absolute home paths compatible."""
    harness.update_config({"jenkins_home": jenkins_home})
    harness.begin()

    assert charm_state.State.from_charm(harness.charm).jenkins_home.as_posix() == jenkins_home


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
