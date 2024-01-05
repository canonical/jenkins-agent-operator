# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

"""Integration tests for jenkins-agent-k8s-operator charm."""

import logging
import string
import secrets

import jenkinsapi.jenkins
from juju.application import Application
from juju.model import Model

from .conftest import NUM_AGENT_UNITS, assert_job_success

logger = logging.getLogger()

MICROK8S_CONTROLLER = "controller"


def rand_ascii(length: int) -> str:
    """Generate random string containing only ascii characters.

    Args:
        length: length of the generated string.

    Returns:
        Randomly generated ascii string of length {length}.
    """
    return ''.join(secrets.choice(string.ascii_lowercase) for _ in range(length))


async def test_agent_relation(
    jenkins_server: Application,
    jenkins_agent_application: Application,
    jenkins_client: jenkinsapi.jenkins.Jenkins,
):
    """
    arrange: given a cross controller cross model jenkins machine agent.
    act: when the offer is created and relation is setup through the offer.
    assert: the relation succeeds and agents become active.
    """
    agent_cmi_name: str = f"cmi-agent-{rand_ascii(4)}"
    jenkins_server_model: Model = jenkins_server.model
    logger.info("Creating offer %s:%s", jenkins_server.name, agent_cmi_name)
    await jenkins_server_model.create_offer(f"{jenkins_server.name}:agent", agent_cmi_name)
    # Machine model of the jenkins agent
    model: Model = jenkins_agent_application.model
    logger.info(
        "cmr: controller:admin/%s.%s",
        jenkins_server_model.name,
        jenkins_server.name,
    )
    await model.relate(
        f"{jenkins_agent_application.name}:agent",
        f"{MICROK8S_CONTROLLER}:admin/{jenkins_server_model.name}.{agent_cmi_name}",
    )
    await model.wait_for_idle(status="active", timeout=1200)

    nodes = jenkins_client.get_nodes()
    assert all(node.is_online() for node in nodes.values())
    # One of the nodes is the server node.
    assert len(nodes.values()) == NUM_AGENT_UNITS + 1

    assert_job_success(jenkins_client, jenkins_agent_application.name, "machine")
