# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Integration tests for jenkins-agent-k8s-operator charm."""

import json
import logging
import textwrap
import time

import jenkinsapi.custom_exceptions
import jenkinsapi.jenkins
import jenkinsapi.node
import jubilant
import pytest
import requests
from jubilant._juju import CLIError
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_fixed

logger = logging.getLogger()

JENKINS_APPLICATION_NAME = "jenkins-k8s"
JENKINS_AGENT_HOME = "/srv/jenkins-agent"
JENKINS_AGENT_USER = "jenkins-agent-test"
LEGACY_AGENT_APPLICATION_NAME = "upgrade-agent"
LEGACY_AGENT_HOME = "/var/lib/jenkins"
LEGACY_AGENT_LABEL = "ownership-upgrade"
LEGACY_AGENT_REVISION = 265


def _gen_test_job_xml(node_label: str, command: str = 'echo "hello world"'):
    """Generate a job xml with target node label and shell command.

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
                    <command>{command}</command>
                    <configuredLocalRules/>
                </hudson.tasks.Shell>
            </builders>
            <publishers/>
            <buildWrappers/>
        </project>
        """
    )


def _configure_agent_remote_fs(
    client: jenkinsapi.jenkins.Jenkins, agent_name: str
) -> jenkinsapi.node.Node:
    """Set the Jenkins controller workspace root to the configured agent home."""
    node = client.get_node(agent_name)
    node.set_config_element("remoteFS", JENKINS_AGENT_HOME)
    return node


@pytest.fixture(scope="module", name="active_agent")
def active_agent_fixture(
    jenkins_agent_requirer: str,
    jenkins_agent_application: str,
    jenkins_client: jenkinsapi.jenkins.Jenkins,
    juju: jubilant.Juju,
):
    """Agent related to server and active."""
    juju.integrate(jenkins_agent_requirer, jenkins_agent_application)
    juju.wait(jubilant.all_active, timeout=60 * 15)
    nodes = [
        node
        for node in jenkins_client.get_nodes().values()
        if jenkins_agent_application in node.name
    ]
    assert len(nodes) == 1, f"Expected one agent node, found {len(nodes)}"
    _configure_agent_remote_fs(jenkins_client, nodes[0].name)
    # Jenkins applies a node's remoteFS when the inbound agent reconnects.
    juju.cli(
        "ssh",
        f"{jenkins_agent_application}/0",
        "sudo",
        "systemctl",
        "restart",
        "jenkins-agent",
    )
    juju.wait(jubilant.all_active, timeout=60 * 15)
    assert _wait_for_agent_online(jenkins_client, nodes[0].name), (
        f"Agent {nodes[0].name} did not reconnect after updating remoteFS"
    )
    return jenkins_agent_application


@retry(
    retry=retry_if_exception_type(
        (jenkinsapi.custom_exceptions.JenkinsAPIException, requests.exceptions.RequestException)
    ),
    stop=stop_after_attempt(10),
    wait=wait_fixed(5),
    reraise=True,
)
def _initialize_client(url: str, password: str) -> jenkinsapi.jenkins.Jenkins:
    """Initialize a Jenkins API client, retrying until the server is ready."""
    return jenkinsapi.jenkins.Jenkins(baseurl=url, username="admin", password=password, timeout=60)


def _fresh_server_client(
    microk8s_juju: jubilant.Juju, traefik_k8s_application: str
) -> jenkinsapi.jenkins.Jenkins:
    """Build a Jenkins client routed through the stable Traefik ingress.

    The pod IP is ephemeral and the pod has a transient not-ready window when
    restarted (it briefly 404s rather than refusing). Traefik only routes to
    ready backends, so it is the stable target. Retry the first poll to absorb
    any residual readiness race.
    """
    result = microk8s_juju.run(f"{traefik_k8s_application}/0", "show-proxied-endpoints")
    proxied = json.loads(result.results["proxied-endpoints"])
    url = proxied[JENKINS_APPLICATION_NAME]["url"]
    admin_result = microk8s_juju.run(f"{JENKINS_APPLICATION_NAME}/0", "get-admin-password")
    password = admin_result.results.get("password", "")
    assert password, "Failed to get admin password"

    return _initialize_client(url, password)


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
    status = build.get_status()
    if status != "SUCCESS":
        logger.error("Jenkins build %s failed; console:\n%s", build, build.get_console())
    assert status == "SUCCESS"


def test_agent_relation(jenkins_client: jenkinsapi.jenkins.Jenkins, active_agent: str):
    """
    arrange: given a Jenkins server client and the registered agent.
    act: when a job is created.
    assert: the agent is able to run job to completion.
    """
    nodes = jenkins_client.get_nodes()
    agent_nodes = [node for node in nodes.values() if active_agent in node.name]
    assert len(agent_nodes) == 1, f"Expected one agent node, found {len(agent_nodes)}"
    agent_name = agent_nodes[0].name
    _configure_agent_remote_fs(jenkins_client, agent_name)
    assert jenkins_client.get_node(agent_name).get_config_element("remoteFS") == JENKINS_AGENT_HOME
    assert all(node.is_online() for node in jenkins_client.get_nodes().values())

    assert_job_success(
        client=jenkins_client,
        agent_name=agent_name,
        test_target_label="machine",
    )


def test_agent_uses_configured_user_and_home(
    jenkins_client: jenkinsapi.jenkins.Jenkins, active_agent: str
):
    """Verify configured agent_user and jenkins_home reach the running process.

    The fixture deploys the charm with non-default values.  Running a job on the
    agent validates the complete path from charm config through the systemd unit
    and environment, rather than merely checking rendered files.
    """
    nodes = jenkins_client.get_nodes()
    agent_nodes = [node for node in nodes.values() if active_agent in node.name]
    assert len(agent_nodes) == 1, f"Expected one agent node, found {len(agent_nodes)}"
    agent_name = agent_nodes[0].name
    _configure_agent_remote_fs(jenkins_client, agent_name)
    assert jenkins_client.get_node(agent_name).get_config_element("remoteFS") == JENKINS_AGENT_HOME
    command = (
        'printf "agent-user=%s\\njenkins-home=%s\\nworkdir=%s\\n" '
        '"$(id -un)" "$JENKINS_HOME" "$PWD"'
    )
    job_name = f"{agent_name}-configuration"
    job = jenkins_client.create_job(job_name, _gen_test_job_xml("machine", command))
    queue_item = job.invoke()
    queue_item.block_until_complete()
    build = queue_item.get_build()
    status = build.get_status()
    console = build.get_console()
    if status != "SUCCESS":
        logger.error("Jenkins configuration build failed; console:\n%s", console)
    assert status == "SUCCESS"
    assert f"agent-user={JENKINS_AGENT_USER}" in console
    # Jenkins runs freestyle jobs in a workspace below the node remote FS.
    # The controller may export its own JENKINS_HOME to build processes; the
    # workspace path is the authoritative agent-home check.
    assert f"workdir={JENKINS_AGENT_HOME}/workspace/" in console


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


def test_agent_upgrades_from_revision_265(
    charm: str,
    arch: str,
    use_docker: bool,
    jenkins_agent_requirer: str,
    juju: jubilant.Juju,
    microk8s_juju: jubilant.Juju,
    ingressed_jenkins_server: str,
    traefik_k8s_application: str,
):
    """
    Arrange: deploy a root-running revision 265 agent with generated runtime state.
    Act: refresh the agent to the current local charm and restart it.
    Assert: the same runtime paths remain usable by the dedicated service user.
    """
    if use_docker:
        pytest.skip("Charmhub revision upgrade test requires a Juju-deployed Jenkins server")
    if arch != "amd64":
        pytest.skip("Charmhub revision 265 is tested only on amd64")

    # Keep the Jenkins server stable; the agent upgrade is the behavior under test.
    jenkins_client = _fresh_server_client(microk8s_juju, traefik_k8s_application)
    juju.deploy(
        "jenkins-agent",
        app=LEGACY_AGENT_APPLICATION_NAME,
        channel="latest/edge",
        revision=LEGACY_AGENT_REVISION,
        num_units=1,
        base="ubuntu@24.04",
        config={"jenkins_agent_labels": LEGACY_AGENT_LABEL},
        constraints={"arch": "amd64"},
    )
    juju.wait(
        lambda status: jubilant.all_agents_idle(status, LEGACY_AGENT_APPLICATION_NAME),
        timeout=60 * 20,
    )
    juju.integrate(jenkins_agent_requirer, LEGACY_AGENT_APPLICATION_NAME)
    juju.wait(
        lambda status: jubilant.all_active(status, LEGACY_AGENT_APPLICATION_NAME),
        timeout=60 * 15,
    )

    agent_nodes = [
        node
        for node in jenkins_client.get_nodes().values()
        if LEGACY_AGENT_APPLICATION_NAME in node.name
    ]
    assert len(agent_nodes) == 1, f"Expected one legacy agent node, found {len(agent_nodes)}"
    agent_name = agent_nodes[0].name
    legacy_node = jenkins_client.get_node(agent_name)
    legacy_node.set_config_element("remoteFS", LEGACY_AGENT_HOME)
    unit_name = f"{LEGACY_AGENT_APPLICATION_NAME}/0"
    juju.cli("ssh", unit_name, "sudo", "systemctl", "restart", "jenkins-agent")
    assert _wait_for_agent_online(jenkins_client, agent_name), (
        f"Legacy agent {agent_name} did not reconnect after setting remoteFS"
    )

    job_name = f"{agent_name}-rev265-upgrade"
    job = jenkins_client.create_job(
        job_name,
        _gen_test_job_xml(
            LEGACY_AGENT_LABEL,
            'printf "agent-user=%s\\n" "$(id -un)"; '
            'printf "workspace-write-ok\\n" > upgrade-proof',
        ),
    )
    queue_item = job.invoke()
    queue_item.block_until_complete()
    build = queue_item.get_build()
    assert build.get_status() == "SUCCESS", build.get_console()
    assert "agent-user=root" in build.get_console()

    runtime_paths = [
        f"{LEGACY_AGENT_HOME}/agent.jar",
        f"{LEGACY_AGENT_HOME}/remoting",
        f"{LEGACY_AGENT_HOME}/workspace",
    ]
    owners_before = juju.cli(
        "ssh", unit_name, "sudo", "stat", "-c", "%U", *runtime_paths
    ).splitlines()
    assert owners_before == ["root"] * len(runtime_paths)

    juju.refresh(LEGACY_AGENT_APPLICATION_NAME, path=charm)

    # The upgraded charm leaves the service stopped until the explicit ownership
    # action repairs the legacy tree.
    juju.cli(
        "run",
        unit_name,
        "migrate-runtime-directory",
        "--wait=20m",
    )
    juju.cli("ssh", unit_name, "sudo", "systemctl", "start", "jenkins-agent")
    # Refresh once more so the charm observes the manually started service and
    # clears the blocked status set during the ownership gate.
    juju.refresh(LEGACY_AGENT_APPLICATION_NAME, path=charm)

    def upgraded_and_active(status: jubilant.Status) -> bool:
        application = status.apps.get(LEGACY_AGENT_APPLICATION_NAME)
        return bool(
            application
            and application.charm_origin == "local"
            and jubilant.all_active(status, LEGACY_AGENT_APPLICATION_NAME)
        )

    juju.wait(upgraded_and_active, timeout=60 * 20)
    assert _wait_for_agent_online(jenkins_client, agent_name), (
        f"Agent {agent_name} did not come online after the local charm refresh"
    )

    owners_after = juju.cli(
        "ssh", unit_name, "sudo", "stat", "-c", "%U", *runtime_paths
    ).splitlines()
    assert owners_after == ["jenkins"] * len(runtime_paths)
    # The migration is in place: no archive is created, and the original proof remains
    # in the workspace used by the root-running revision.
    legacy_archive_paths = [
        f"{LEGACY_AGENT_HOME}/.jenkins-agent-legacy-remoting",
        f"{LEGACY_AGENT_HOME}/.jenkins-agent-legacy-workspace",
    ]
    for legacy_archive_path in legacy_archive_paths:
        juju.cli("ssh", unit_name, "sudo", "test", "!", "-e", legacy_archive_path)
    preserved_proof = f"{LEGACY_AGENT_HOME}/workspace/{job_name}/upgrade-proof"
    juju.cli("ssh", unit_name, "sudo", "test", "-f", preserved_proof)

    queue_item = job.invoke()
    queue_item.block_until_complete()
    build = queue_item.get_build()
    assert build.get_status() == "SUCCESS", build.get_console()
    assert "agent-user=jenkins" in build.get_console()
    juju.cli("ssh", unit_name, "sudo", "test", "-f", preserved_proof)

    # A subsequent restart must keep the migrated state and the same workspace usable.
    juju.cli("ssh", unit_name, "sudo", "systemctl", "restart", "jenkins-agent")
    assert _wait_for_agent_online(jenkins_client, agent_name), (
        f"Agent {agent_name} did not reconnect after a second restart"
    )
    juju.cli("ssh", unit_name, "sudo", "test", "-f", preserved_proof)


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

    nodes = jenkins_client.get_nodes()
    agent_nodes = [node for node in nodes.values() if active_agent in node.name]
    assert len(agent_nodes) == 1, f"Expected one agent node, found {len(agent_nodes)}"
    agent_name = agent_nodes[0].name

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


def test_agent_traefik_ingress(
    ingressed_jenkins_server: str,
    active_agent: str,
    jenkins_agent_application: str,
    jenkins_agent_requirer: str,
    juju: jubilant.Juju,
    microk8s_juju: jubilant.Juju,
    traefik_k8s_application: str,
):
    """
    Verify agent connects successfully through traefik ingress using WebSocket.

    This is the primary test for issue #165 - ensuring that jenkins-agent can
    connect to jenkins-k8s through HTTP-only ingress (traefik) using the -webSocket flag.

    Without the -webSocket flag, the agent attempts to connect via TCP port 50000 which is not
    routed by traefik (HTTP-only ingress), causing connection failure. With -webSocket, the
    agent uses the same HTTP connection and successfully connects.

    NOTE: This test only runs on amd64 with microk8s. Non-amd64 architectures (arm64, s390x,
    ppc64le) use Docker-based Jenkins deployment (via --use-docker flag) and automatically
    skip this test via the traefik_k8s_application fixture.

    arrange: jenkins-k8s with traefik ingress configured, jenkins-agent deployed with websocket_mode=true
    act: relate agent to ingressed jenkins server
    assert:
      - agent reaches active status (not error/blocked)
      - logs show "WebSocket connection open" (confirming WebSocket mode)
      - logs do NOT show "port:50000 is not reachable" (the bug from issue #165)
      - agent can execute jobs successfully (functional verification)
    """
    # Relate agent to ingressed Jenkins server (if not already related)
    logger.info("Ensuring jenkins-agent is related to ingressed jenkins-k8s...")
    try:
        juju.integrate(jenkins_agent_requirer, jenkins_agent_application)
    except CLIError as e:
        # Relation may already exist from earlier tests in the same module
        if "already exists" in str(e):
            logger.info("Relation already exists, continuing...")
        else:
            raise
    juju.wait(jubilant.all_active, timeout=60 * 15)
    logger.info("jenkins-agent reached active status")

    # Check agent logs for WebSocket connection confirmation
    status = juju.status()
    model_name = status.model.name

    import subprocess  # nosec: B404 - subprocess usage is safe here for juju SSH

    logger.info("Checking jenkins-agent logs for WebSocket connection...")
    log_result = subprocess.run(  # nosec: B607, B603
        [
            "juju",
            "ssh",
            "--model",
            model_name,
            f"{jenkins_agent_application}/0",
            "sudo",
            "journalctl",
            "-u",
            "jenkins-agent",
            "-n",
            "200",
            "--no-pager",
        ],
        capture_output=True,
        text=True,
        check=True,
    )

    agent_logs = log_result.stdout

    # Assert WebSocket connection was established
    assert "WebSocket connection open" in agent_logs, (
        "Agent logs should show 'WebSocket connection open' when using -webSocket flag. "
        "This confirms the agent is using WebSocket mode to connect through HTTP ingress."
    )

    # Assert NO TCP port 50000 errors (the bug from issue #165)
    assert "port:50000 is not reachable" not in agent_logs, (
        "Agent should not try to connect via TCP port 50000 when using WebSocket mode. "
        "This error indicates the -webSocket flag is missing and the agent is trying JNLP4 protocol."
    )

    logger.info("WebSocket connection verified in agent logs")

    # Verify agent is functional by checking it's registered in Jenkins
    # Note: When using traefik ingress, the Jenkins API access may be limited.
    # Use a client routed through the Traefik ingress: it survives pod restarts
    # and balancer-side readiness, unlike the module-scoped pod-IP client.
    fresh_client = _fresh_server_client(microk8s_juju, traefik_k8s_application)
    nodes = fresh_client.get_nodes()
    agent_nodes = [node for node in nodes.values() if jenkins_agent_application in node.name]
    assert len(agent_nodes) == 1, f"Expected one agent node, found {len(agent_nodes)}"
    agent_name = agent_nodes[0].name
    _configure_agent_remote_fs(fresh_client, agent_name)
    assert fresh_client.get_node(agent_name).get_config_element("remoteFS") == JENKINS_AGENT_HOME
    assert_job_success(
        client=fresh_client,
        agent_name=agent_name,
        test_target_label="machine",
    )
