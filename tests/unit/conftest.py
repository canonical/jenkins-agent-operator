# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.
"""Fixtures for jenkins-agent charm tests."""


import secrets

import pytest
from ops.testing import Harness

from charm import JenkinsAgentCharm
from charm_state import AGENT_RELATION


@pytest.fixture(scope="module", name="agent_relation_data")
def agent_relation_data_fixture() -> dict:
    """Mock relation data for agent relation."""
    return {"url": "http://example.com", "jenkins-agent-0_secret": secrets.token_hex(4)}


@pytest.fixture(scope="module", name="service_configuration_template")
def service_configuration_template_fixture(agent_relation_data: dict) -> str:
    """Mock service environment variables configuration for jenkins-agent."""
    return f'''[Service]
Environment="JENKINS_TOKEN={agent_relation_data.get('jenkins-agent-0_secret')}"
Environment="JENKINS_URL={agent_relation_data.get('url')}"
Environment="JENKINS_AGENT=jenkins-agent-0"'''


@pytest.fixture(scope="function", name="harness")
def harness_fixture():
    """Enable ops test framework harness."""
    harness = Harness(JenkinsAgentCharm)

    yield harness

    harness.cleanup()


@pytest.fixture(scope="function", name="harness_with_agent_relation")
def harness_with_agent_relation_fixture(harness: Harness, agent_relation_data: dict) -> Harness:
    """Harness with agent relation to jenkins-k8s.

    Args:
        harness the default testing harness.

    Returns:
        The harness with agent relation established.
    """
    harness.add_relation(AGENT_RELATION, "jenkins-k8s", unit_data=agent_relation_data)
    return harness
