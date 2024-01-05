# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

"""Fixtures for Jenkins-agent-k8s-operator charm integration tests."""

import logging
import secrets
import typing
import ops

import jenkinsapi.jenkins
import pytest
import pytest_asyncio
from juju.action import Action
from juju.application import Application
from juju.client._definitions import FullStatus, UnitStatus
from juju.model import Controller, Model
from juju.unit import Unit
from pytest_operator.plugin import OpsTest

logger = logging.getLogger(__name__)

NUM_AGENT_UNITS = 1

@pytest_asyncio.fixture(scope="module", name="charm")
async def charm_fixture(request: pytest.FixtureRequest, ops_test: OpsTest) -> str:
    """The path to charm."""
    charm = request.config.getoption("--charm-file")
    if not charm:
        charm = await ops_test.build_charm(".")
    else:
        charm = f"./{charm}"

    return charm


@pytest.fixture(scope="module", name="model")
def model_fixture(ops_test: OpsTest) -> Model:
    """The testing model."""
    assert ops_test.model
    return ops_test.model


@pytest.fixture(scope="module", name="num_agents")
def num_agents_fixture() -> int:
    """The number of agents to deploy."""
    return 1


@pytest.mark.parametrize(
    "series",
    [
        pytest.param("focal"),
        pytest.param("jammy"),
    ],
)
@pytest_asyncio.fixture(scope="module", name="jenkins_agent_application")
async def application_fixture(
    model: Model, charm: str, num_agents: int, series: str
) -> typing.AsyncGenerator[Application, None]:
    """Build and deploy the charm."""
    # Deploy the charm and wait for blocked status
    application = await model.deploy(charm, num_units=num_agents, series=series)
    await model.wait_for_idle(apps=[application.name], status=ops.BlockedStatus.name)

    yield application

    await model.remove_application(application.name, block_until_done=True, force=True)


@pytest_asyncio.fixture(scope="module", name="k8s_controller")
async def jenkins_server_k8s_controller_fixture() -> typing.AsyncGenerator[Controller, None]:
    """The juju controller on microk8s.
    The controller is bootstrapped in "pre_run_script.sh".
    """
    controller = Controller()
    await controller.connect("controller")
    cloud = await controller.get_cloud()
    logger.info("Creating jenkins server controller on cloud %s", cloud)

    yield controller

    await controller.disconnect()


@pytest_asyncio.fixture(scope="module", name="jenkins_server_model")
async def jenkins_server_model_fixture(
    k8s_controller: Controller,
) -> typing.AsyncGenerator[Model, None]:
    """The model for jenkins-k8s charm."""
    model_name = f"jenkins-k8s-{secrets.token_hex(2)}"
    cloud = await k8s_controller.get_cloud()
    logger.info("Adding model %s on %s", model_name, cloud)
    model = await k8s_controller.add_model(model_name)

    yield model

    await k8s_controller.destroy_models(
        model.name, destroy_storage=True, force=True, max_wait=10 * 60
    )


@pytest_asyncio.fixture(scope="module", name="jenkins_server")
async def jenkins_server_fixture(jenkins_server_model: Model) -> Application:
    """The jenkins machine server."""
    jenkins = await jenkins_server_model.deploy("jenkins-k8s")
    await jenkins_server_model.wait_for_idle(
        apps=[jenkins.name],
        timeout=20 * 60,
        wait_for_active=True,
        idle_period=30,
        raise_on_error=False,
    )

    return jenkins


@pytest_asyncio.fixture(scope="module", name="server_unit_ip")
async def server_unit_ip_fixture(jenkins_server_model: Model, jenkins_server: Application):
    """Get Jenkins machine server charm unit IP."""
    status: FullStatus = await jenkins_server_model.get_status([jenkins_server.name])
    try:
        unit_status: UnitStatus = next(
            iter(status.applications[jenkins_server.name].units.values())
        )
        assert unit_status.address, "Invalid unit address"
        return unit_status.address
    except StopIteration as exc:
        raise StopIteration("Invalid unit status") from exc


@pytest_asyncio.fixture(scope="module", name="web_address")
async def web_address_fixture(server_unit_ip: str):
    """Get Jenkins machine server charm web address."""
    return f"http://{server_unit_ip}:8080"


@pytest_asyncio.fixture(scope="module", name="jenkins_client")
async def jenkins_client_fixture(
    jenkins_server: Application,
    web_address: str,
) -> jenkinsapi.jenkins.Jenkins:
    """The Jenkins API client."""
    jenkins_unit: Unit = jenkins_server.units[0]
    action: Action = await jenkins_unit.run_action("get-admin-password")
    await action.wait()
    assert action.status == "completed", "Failed to get credentials."
    password = action.results["password"]

    # Initialization of the jenkins client will raise an exception if unable to connect to the
    # server.
    return jenkinsapi.jenkins.Jenkins(
        baseurl=web_address, username="admin", password=password, timeout=60
    )
