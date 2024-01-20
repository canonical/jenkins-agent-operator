# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.
#
# Learn more about testing at: https://juju.is/docs/sdk/testing

# pylint: disable=protected-access
"""Test for charm state."""

import os
from unittest.mock import MagicMock

import ops
import ops.testing
import pytest

import charm_state
from charm import JenkinsAgentCharm


def test_from_charm_invalid_metadata(
    harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch
):
    """
    arrange: patched State.from_charm that raises an InvalidState Error.
    act: when the JenkinsAgentCharm is initialized.
    assert: The agent falls into BlockedStatus.
    """
    monkeypatch.setattr(os, "cpu_count", MagicMock(return_value=0))
    harness.begin()
    charm: JenkinsAgentCharm = harness.charm
    with pytest.raises(charm_state.InvalidStateError, match="Invalid executor state."):
        charm_state.State.from_charm(charm=charm)
