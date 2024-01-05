# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.
"""Fixtures for jenkins-agent charm tests."""


import pytest
from ops.testing import Harness

from charm import JenkinsAgentCharm


@pytest.fixture(scope="function", name="harness")
def harness_fixture():
    """Enable ops test framework harness."""
    harness = Harness(JenkinsAgentCharm)

    yield harness

    harness.cleanup()
