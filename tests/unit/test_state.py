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
