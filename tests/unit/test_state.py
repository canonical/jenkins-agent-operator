# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.
#
# Learn more about testing at: https://juju.is/docs/sdk/testing

"""Test for charm state."""

# Need access to protected functions for testing
# pylint:disable=protected-access

import os
from unittest.mock import MagicMock

import ops
import ops.testing
import pytest

import charm_state
from charm import JenkinsAgentCharm
from charm_state import APT_PACKAGES_CONFIG


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

    with pytest.raises(charm_state.InvalidStateError, match="Invalid executor state."):
        charm_state.State.from_charm(charm=charm)


@pytest.mark.parametrize(
    "packages, expected",
    [
        pytest.param("", tuple(), id="empty"),
        pytest.param("git,bzr", ("git", "bzr"), id="has packages"),
    ],
)
def test__get_packages_to_install(
    harness: ops.testing.Harness, packages: str, expected: tuple[str, ...]
):
    """
    arrange: given charm apt-packages config.
    act: when _get_packages_to_install is called.
    assert: packages are correctly parsed.
    """
    harness.begin()
    harness.update_config({APT_PACKAGES_CONFIG: packages})
    charm: JenkinsAgentCharm = harness.charm

    assert charm_state._get_packages_to_install(charm=charm) == expected
