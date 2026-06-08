# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Integration tests for jenkins-agent-k8s-operator charm."""

import logging
import textwrap
import time

import jenkinsapi.jenkins
import jubilant
import pytest

logger = logging.getLogger()

JENKINS_APPLICATION_NAME = "jenkins-k8s"


def _gen_test_job_xml(node_label: str):
    """Generate a job xml with target node label.

    Args:
        node_label: The node label to assign to job to.

    Returns:
        The job XML.
    """
    return textwrap.dedent(
        f"""
        <project>
            <actions/>
            <description/>
            <keepDependencies>false</keepDependencies>
            <properties/>
            <scm class="hudson.scm.NullSCM"/>
            <assignedNode>{node_label}</assignedNode>
            <canRoam>false</canRoam>
            <disabled>false</disabled>
            <blockBuildWhenDownstreamBuilding>false</blockBuildWhenDownstreamBuilding>
            <blockBuildWhenUpstreamBuilding>false</blockBuildWhenUpstreamBuilding>
            <triggers/>
            <concurrentBuild>false</concurrentBuild>
            <builders>
                <hudson.tasks.Shell>
                    <command>echo "hello world"</command>
                    <configuredLocalRules/>
                </hudson.tasks.Shell>
            </builders>
            <publishers/>
            <buildWrappers/>
        </project>
        """
    )


@pytest.fixture(scope="module", name="active_agent")
def active_agent_fixture(
    jenkins_agent_requirer: str, jenkins_agent_application: str, juju: jubilant.Juju
):
    """Agent related to server and active."""
    juju.integrate(jenkins_agent_requirer, jenkins_agent_application)
    juju.wait(jubilant.all_active, timeout=60 * 15)
    return jenkins_agent_application


def assert_job_success(
    *, client: jenkinsapi.jenkins.Jenkins, agent_name: str, test_target_label: str
):
    """Assert that a job can be created and ran successfully.

    Args:
        client: The Jenkins API client.
        agent_name: The registered Jenkins agent node to check.
        test_target_label: The Jenkins agent node label.
    """
    job = client.create_job(agent_name, _gen_test_job_xml(test_target_label))
    queue_item = job.invoke()
    queue_item.block_until_complete()
    build: jenkinsapi.build.Build = queue_item.get_build()
    assert build.get_status() == "SUCCESS"


def test_agent_relation(jenkins_client: jenkinsapi.jenkins.Jenkins, active_agent: str):
    """
    arrange: given a Jenkins server client and the registered agent.
    act: when a job is created.
    assert: the agent is able to run job to completion.
    """
    agent_name = f"{active_agent}-0"
    nodes = jenkins_client.get_nodes()
    assert all(node.is_online() for node in nodes.values())
    assert any(node.name == agent_name for node in nodes.values())

    assert_job_success(
        client=jenkins_client,
        agent_name=agent_name,
        test_target_label="machine",
    )


def _wait_for_agent_online(
    jenkins_client: jenkinsapi.jenkins.Jenkins, agent_name: str, timeout: int = 600
) -> bool:
    """Wait for a Jenkins agent to come online.

    Args:
        jenkins_client: The Jenkins API client.
        agent_name: The agent node name.
        timeout: Maximum wait time in seconds.

    Returns:
        True if the agent came online within the timeout.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            node = jenkins_client.get_node(agent_name)
            if node.is_online():
                return True
        except Exception:  # nosec B110
            pass
        time.sleep(10)
    return False


def test_agent_reconnects_after_server_refresh(
    jenkins_client: jenkinsapi.jenkins.Jenkins,
    active_agent: str,
    juju: jubilant.Juju,
    microk8s_juju: jubilant.Juju,
    use_docker: bool,
):
    """
    arrange: given a Jenkins server and registered agent that is active and online.
    act: when the Jenkins server charm is refreshed (simulating pod restart / URL change).
    assert: the agent reconnects and comes back online without manual intervention.
    """
    if use_docker:
        pytest.skip("Server refresh test requires Juju-deployed Jenkins server")

    agent_name = f"{active_agent}-0"

    # Verify agent is initially online.
    node = jenkins_client.get_node(agent_name)
    assert node.is_online(), f"Agent {agent_name} should be online before refresh"

    # Refresh the Jenkins server charm to trigger pod restart and IP change.
    logger.info("Refreshing Jenkins server charm to trigger pod restart...")
    microk8s_juju.cli("refresh", JENKINS_APPLICATION_NAME, "--channel", "latest/edge")
    microk8s_juju.wait(jubilant.all_agents_idle, timeout=60 * 15)
    microk8s_juju.wait(jubilant.all_active, timeout=60 * 15)

    # The server may have a new IP. Re-create the client with the new address.
    unit_status = (
        microk8s_juju.status()
        .get_units(JENKINS_APPLICATION_NAME)
        .get(f"{JENKINS_APPLICATION_NAME}/0")
    )
    assert unit_status, "Jenkins server unit not found after refresh"
    new_address = unit_status.address
    logger.info("Jenkins server new address after refresh: %s", new_address)

    result = microk8s_juju.run(f"{JENKINS_APPLICATION_NAME}/0", "get-admin-password")
    password = result.results.get("password", "")
    assert password, "Failed to get admin password after refresh"

    new_client = jenkinsapi.jenkins.Jenkins(
        baseurl=f"http://{new_address}:8080",
        username="admin",
        password=password,
        timeout=60,
    )

    # Wait for agent to reconnect (the charm should detect the URL change and restart).
    juju.wait(jubilant.all_agents_idle, timeout=60 * 5)
    assert _wait_for_agent_online(new_client, agent_name, timeout=600), (
        f"Agent {agent_name} did not reconnect after server refresh within 10 minutes"
    )

    # Verify agent is functional by checking it's online.
    node = new_client.get_node(agent_name)
    assert node.is_online(), f"Agent {agent_name} should be online after reconnection"
