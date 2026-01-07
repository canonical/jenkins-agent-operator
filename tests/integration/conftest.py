# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Fixtures for Jenkins-agent-k8s-operator charm integration tests."""

import json
import logging
import platform
import socket
import textwrap
import time
import typing
from dataclasses import dataclass

import docker
import jenkinsapi
import jubilant
import pytest
import pytest_asyncio

logger = logging.getLogger(__name__)

JENKINS_APPLICATION_NAME = "jenkins-k8s"
JENKINS_AGENT_APPLICATION_NAME = "jenkins-agent"
ANY_CHARM_APPLICATION_NAME = "any-charm"


@pytest_asyncio.fixture(scope="module", name="charm")
async def charm_fixture(request: pytest.FixtureRequest) -> str:
    """The path to charm."""
    charm = request.config.getoption("--charm-file")
    assert charm, "Charm file not provided"
    return charm


@pytest.fixture(scope="module", name="keep_models")
def keep_models_fixture(request: pytest.FixtureRequest):
    """Whether to keep models after testing."""
    return request.config.option.keep_models


@pytest.fixture(scope="module", name="juju")
def juju_fixture(keep_models: bool):
    """The Jubilant Juju object."""
    with jubilant.temp_model(keep=keep_models) as juju:
        yield juju


@pytest.fixture(scope="module", name="use_docker")
def use_docker_fixture(request: pytest.FixtureRequest):
    """Whether to use Docker to host Jenkins for testing."""
    return request.config.getoption("--use-docker")


@pytest.fixture(scope="module", name="arch")
def arch_fixture():
    """Get the current machine architecture."""
    arch = platform.uname().processor
    if arch in ("aarch64", "arm64"):
        return "arm64"
    if arch in ("ppc64le", "ppc64el"):
        return "ppc64el"
    if arch in ("x86_64", "amd64"):
        return "amd64"
    if arch in ("s390x",):
        return "s390x"
    raise NotImplementedError(f"Unimplemented arch {arch}")


@pytest.fixture(scope="module", name="microk8s_juju")
def microk8s_juju_fixture(use_docker: bool, keep_models: bool):
    """The Jubilant Juju object."""
    if use_docker:
        yield None
        return

    with jubilant.temp_model(controller="microk8s", keep=keep_models) as juju:
        yield juju


@dataclass
class JenkinsServer:
    """Information about Jenkins server.

    Attributes:
        address: Server address.
        port: Server port.
        username: Server admin username.
        password: Server admin password.
    """

    address: str
    username: str
    password: str
    port: str = "8080"


def _get_juju_jenkins_server_password(juju: jubilant.Juju, application: str):
    """Get Juju deployed Jenkins server password."""
    result = juju.run(f"{application}/0", "get-admin-password")
    password = result.results.get("password", "")
    assert password, f"Failed to get password from results: {result}"
    return password


def _deploy_jenkins_server_juju(agent_juju: jubilant.Juju, microk8s_juju: jubilant.Juju):
    """Deploy Jenkins k8s server as agent relation provider."""
    microk8s_juju.deploy(JENKINS_APPLICATION_NAME, channel="latest/edge")
    microk8s_juju.wait(jubilant.all_active)
    unit_status = (
        microk8s_juju.status()
        .get_units(JENKINS_APPLICATION_NAME)
        .get(f"{JENKINS_APPLICATION_NAME}/0")
    )
    assert unit_status, f"Unit status not found for {JENKINS_APPLICATION_NAME}"
    password = _get_juju_jenkins_server_password(
        juju=microk8s_juju, application=JENKINS_APPLICATION_NAME
    )
    microk8s_model = microk8s_juju.model
    assert microk8s_model, "microk8s model not found"
    microk8s_model_name = microk8s_model.removeprefix("microk8s:")
    microk8s_juju.cli(
        "offer",
        "--controller",
        "microk8s",
        f"{microk8s_model_name}.{JENKINS_APPLICATION_NAME}:agent",
        JENKINS_APPLICATION_NAME,
        include_model=False,
    )
    agent_juju.cli(
        "consume", f"microk8s:{microk8s_model_name}.{JENKINS_APPLICATION_NAME}", include_model=True
    )
    # The following has a bug where the controller is prefixed and Juju will error out such as
    # ERROR model name "microk8s:jubilant-fbaddf08" not valid
    # agent_juju.offer(
    #     f"{juju.model}.{JENKINS_APPLICATION_NAME}",
    #     controller="microk8s",
    #     endpoint="agent",
    #     name=offer_name,
    # )
    agent_juju.consume(f"{microk8s_juju.model}.{JENKINS_APPLICATION_NAME}")
    return JenkinsServer(address=unit_status.address, username="admin", password=password)


def _deploy_jenkins_server_docker():
    """Deploy Jenkins server via host Docker."""
    client = docker.from_env()
    container = client.containers.run(
        image="docker.io/jenkins/jenkins:lts-jdk17",
        name="jenkins",
        detach=True,
        ports={
            "8080": 8080,
            "50000": 50000,
        },
        # Restart is required due to Jenkins requiring restart after plugin installation. When
        # Jenkins server exits, the Docker container will also exit.
        restart_policy={"Name": "always"},
        # Bypass setup wizard to simplify testing
        environment={"JAVA_OPTS": "-Djenkins.install.runSetupWizard=false"},
    )

    attempt = 0
    while (
        run_result := container.exec_run(cmd=["curl", "--fail", "-v", "localhost:8080/health"])
    ).exit_code != 0 and attempt < 10:
        attempt += 1
        time.sleep(1)
    assert run_result.exit_code == 0, "Unable to run Jenkins server in Docker."

    # Required to successfully register agent
    result = container.exec_run(["jenkins-plugin-cli", "--plugins", "instance-identity"])
    assert result.exit_code == 0, "Failed to install instance-identity plugin"

    # Getting host IP address is required to access Jenkins from a charm VM host.
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.connect(("8.8.8.8", 80))
    host_ip_addr = s.getsockname()[0]
    s.close()

    # Ignore B106:hardcoded_password_funcarg, the Docker server is launched without user creds.
    return JenkinsServer(address=str(host_ip_addr), username="", password="")  # nosec: B106


@pytest.fixture(scope="module", name="jenkins_client")
def jenkins_client_fixture(juju: jubilant.Juju, microk8s_juju: jubilant.Juju, use_docker: bool):
    """The Jenkins server client."""
    system_attribs = platform.uname()
    logger.info("system arch: %s", system_attribs.processor)

    if not use_docker:
        logger.info("Deploying Jenkins server via Juju: %s", system_attribs.processor)
        server = _deploy_jenkins_server_juju(agent_juju=juju, microk8s_juju=microk8s_juju)
        return jenkinsapi.jenkins.Jenkins(
            baseurl=f"http://{server.address}:{server.port}",
            username=server.username,
            password=server.password,
        )

    logger.info("Deploying Jenkins server via Docker: %s", system_attribs.processor)
    server = _deploy_jenkins_server_docker()
    client = jenkinsapi.jenkins.Jenkins(baseurl=f"http://{server.address}:{server.port}")
    client.safe_restart()
    return client


@pytest.fixture(scope="module", name="jenkins_agent_application", params=["ubuntu@24.04"])
def jenkins_agent_application_fixture(
    juju: jubilant.Juju, charm: str, request: typing.Any, arch: str
):
    """Build and deploy the charm."""
    juju.deploy(
        charm,
        app=JENKINS_AGENT_APPLICATION_NAME,
        num_units=1,
        base=request.param,
        config={"jenkins_agent_labels": "machine"},
        constraints={"arch": arch},
    )
    juju.wait(jubilant.all_agents_idle, timeout=60 * 15)
    return JENKINS_AGENT_APPLICATION_NAME


def _register_agent_node(jenkins_client: jenkinsapi.jenkins.Jenkins):
    """Register agent node.

    Args:
        jenkins_client: Jenkins server client.
    """
    agent_node_meta = {
        "num_executors": 1,
        "node_description": "Test JNLP Node on Docker",
        "remote_fs": "/var/lib/jenkins",
        "labels": "machine",
        "exclusive": True,
    }
    node_name = f"{JENKINS_AGENT_APPLICATION_NAME}-0"
    jenkins_client.nodes.create_node(node_name, agent_node_meta)
    script = (
        f'println(jenkins.model.Jenkins.getInstance().getComputer("{node_name}").getJnlpMac())'
    )
    secret = jenkins_client.run_groovy_script(script).strip()
    return secret


def _generate_any_charm_src_overwrite(jenkins_server_url: str, agent_node_secret: str):
    """Generate any charm src."""
    return {
        "any_charm.py": textwrap.dedent(
            f"""\
        import logging
        from any_charm_base import AnyCharmBase

        logger = logging.getLogger(__name__)

        AGENT_RELATION="require-jenkins-agent-v0"

        class AnyCharm(AnyCharmBase):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.framework.observe(self.on[AGENT_RELATION].relation_changed, self._reconcile)

            def _reconcile(self, event):
                relation = self.model.get_relation(AGENT_RELATION)
                logger.info("Relation: %s", relation)
                if not relation:
                    return
                relation.data[self.model.unit].update({{"url": "{jenkins_server_url}"}})
                for unit in relation.units:
                    relation.data[self.model.unit].update(
                        {{f"{{unit.name.replace('/', '-')}}_secret": "{agent_node_secret}"}}
                    )
        """
        ),
    }


@pytest.fixture(scope="module", name="jenkins_agent_requirer")
def jenkins_agent_requirer_fixture(
    use_docker: bool,
    jenkins_client: jenkinsapi.jenkins.Jenkins,
    juju: jubilant.Juju,
    arch: str,
):
    """Jenkins agent requirer, the acting Jenkins server."""
    if not use_docker:
        return JENKINS_APPLICATION_NAME

    # Register agent node for AnyCharm
    agent_secret = _register_agent_node(jenkins_client=jenkins_client)
    juju.deploy(
        ANY_CHARM_APPLICATION_NAME,
        channel="latest/beta",
        config={
            "src-overwrite": json.dumps(
                _generate_any_charm_src_overwrite(
                    jenkins_server_url=jenkins_client.base_server_url(),
                    agent_node_secret=agent_secret,
                )
            )
        },
        constraints={"arch": arch},
    )
    juju.wait(jubilant.all_agents_idle, timeout=60 * 15)
    return ANY_CHARM_APPLICATION_NAME
