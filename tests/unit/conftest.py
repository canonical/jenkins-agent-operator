# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.
"""Fixtures for jenkins-agent charm tests."""

import secrets
from types import SimpleNamespace
from unittest.mock import MagicMock, PropertyMock, patch

import pytest
from ops.testing import Harness

import service
from charm import JenkinsAgentCharm
from charm_state import AGENT_RELATION


@pytest.fixture(scope="module", name="agent_relation_data")
def agent_relation_data_fixture() -> dict:
    """Mock relation data for agent relation."""
    return {"url": "http://example.com", "test-model-jenkins-agent-0_secret": secrets.token_hex(4)}


@pytest.fixture(scope="module", name="service_configuration_template")
def service_configuration_template_fixture(agent_relation_data: dict) -> str:
    """Mock service environment variables configuration for jenkins-agent."""
    return f'''[Service]
Environment="JENKINS_TOKEN={agent_relation_data.get("test-model-jenkins-agent-0_secret")}"
Environment="JENKINS_URL={agent_relation_data.get("url")}"
Environment="JENKINS_AGENT=test-model-jenkins-agent-0"
Environment="JENKINS_HOME=/var/lib/jenkins"'''


@pytest.fixture(autouse=True)
def mock_os_release():
    """Mock /etc/os-release so State.from_charm works on any platform."""
    with patch(
        "charm_state.dotenv_values",
        return_value={"UBUNTU_CODENAME": "noble"},
    ):
        yield


@pytest.fixture(name="service_mocks")
def service_mocks_fixture(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Patch JenkinsAgentService side-effects so reconcile runs without a host.

    Defaults: install is a no-op, service inactive, credentials changed. Individual
    tests override any attribute (e.g. is_ready) as needed.
    """
    mocks = SimpleNamespace(
        install=MagicMock(return_value=False),
        restart=MagicMock(),
        is_running=PropertyMock(return_value=False),
        reset=MagicMock(),
        reset_failed_state=MagicMock(),
        credentials_changed=MagicMock(return_value=True),
        runtime_directories_usable=MagicMock(return_value=True),
        migrate_runtime_directories=MagicMock(),
    )
    monkeypatch.setattr(service.JenkinsAgentService, "is_ready", PropertyMock(return_value=False))
    monkeypatch.setattr(
        service.JenkinsAgentService, "is_running", PropertyMock(return_value=False)
    )
    monkeypatch.setattr(service.JenkinsAgentService, "install", mocks.install)
    monkeypatch.setattr(service.JenkinsAgentService, "restart", mocks.restart)
    monkeypatch.setattr(service.JenkinsAgentService, "reset", mocks.reset)
    monkeypatch.setattr(
        service.JenkinsAgentService, "reset_failed_state", mocks.reset_failed_state
    )
    monkeypatch.setattr(
        service.JenkinsAgentService, "credentials_changed", mocks.credentials_changed
    )
    monkeypatch.setattr(
        service.JenkinsAgentService, "runtime_directories_usable", mocks.runtime_directories_usable
    )
    monkeypatch.setattr(
        service.JenkinsAgentService,
        "migrate_runtime_directories",
        mocks.migrate_runtime_directories,
        raising=False,
    )
    return mocks


@pytest.fixture(scope="function", name="harness")
def harness_fixture():
    """Enable ops test framework harness."""
    harness = Harness(JenkinsAgentCharm)
    harness.set_model_name("test-model")

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
