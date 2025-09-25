# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Integration tests for jenkins-agent-k8s-operator charm."""

import logging
import textwrap

import jenkinsapi.jenkins
import jubilant
import pytest

logger = logging.getLogger()


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
