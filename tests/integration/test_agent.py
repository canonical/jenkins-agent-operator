# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Integration tests for jenkins-agent-k8s-operator charm."""

import json
import logging
import textwrap
import time
from typing import Any, cast

import jenkinsapi.build
import jenkinsapi.custom_exceptions
import jenkinsapi.jenkins
import jenkinsapi.job
import jenkinsapi.node
import jenkinsapi.queue
import jubilant
import pytest
import requests
from jubilant._juju import CLIError
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    stop_after_delay,
    wait_fixed,
)

logger = logging.getLogger()

JENKINS_APPLICATION_NAME = "jenkins-k8s"
JENKINS_AGENT_HOME = "/srv/jenkins-agent"
JENKINS_AGENT_USER = "jenkins-agent-test"
LEGACY_AGENT_APPLICATION_NAME = "upgrade-agent"
LEGACY_AGENT_HOME = "/var/lib/jenkins"
LEGACY_AGENT_LABEL = "ownership-upgrade"
LEGACY_AGENT_REVISION = 265
BUILD_POLL_TIMEOUT = 600
BUILD_POLL_INTERVAL = 5


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
    client: jenkinsapi.jenkins.Jenkins,
    agent_name: str,
    remote_fs: str = JENKINS_AGENT_HOME,
) -> jenkinsapi.node.Node:
    """Set the Jenkins controller workspace root for an agent."""
    node = client.get_node(agent_name)
    node.set_config_element("remoteFS", remote_fs)
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
    unit_name = _unit_name(juju, jenkins_agent_application)
    juju.cli(
        "ssh",
        unit_name,
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


def test_wait_for_build_retries_transient_queue_error():
    """
    Arrange: the first queue poll raises a transient Jenkins API error.
    Act: invoke the decorated build-polling function with no artificial wait.
    Assert: the queue is retried and the assigned build is returned.
    """

    class QueueItem:
        def __init__(self):
            self.poll_count = 0

        def poll(self):
            self.poll_count += 1
            if self.poll_count == 1:
                raise jenkinsapi.custom_exceptions.JenkinsAPIException()

        def get_build_number(self):
            return 7

    class Build:
        def poll(self):
            return {"building": False, "result": "SUCCESS"}

    class Job:
        def get_build(self, build_number):
            assert build_number == 7
            return Build()

    queue_item = QueueItem()
    build_number, build = cast(Any, _wait_for_build).retry_with(
        stop=stop_after_attempt(2), wait=wait_fixed(0)
    )(job=Job(), queue_item=queue_item)

    assert queue_item.poll_count == 2
    assert build_number == 7
    assert build.poll()["result"] == "SUCCESS"


_BUILD_RETRYABLE_ERRORS = (
    jenkinsapi.custom_exceptions.JenkinsAPIException,
    jenkinsapi.custom_exceptions.NotBuiltYet,
    requests.exceptions.RequestException,
)


@retry(
    retry=retry_if_exception_type(_BUILD_RETRYABLE_ERRORS),
    stop=stop_after_delay(BUILD_POLL_TIMEOUT),
    wait=wait_fixed(BUILD_POLL_INTERVAL),
    reraise=True,
)
def _wait_for_build(
    *, job: jenkinsapi.job.Job, queue_item: jenkinsapi.queue.QueueItem
) -> tuple[int, jenkinsapi.build.Build]:
    """Resolve a queue item to a fresh completed build using bounded polling."""
    queue_item.poll()
    build_number = queue_item.get_build_number()
    if build_number is None:
        raise jenkinsapi.custom_exceptions.NotBuiltYet()

    # Re-fetch by the captured number. Do not use get_last_build(), which can race
    # with another queued build.
    build = job.get_build(build_number)
    build_data = build.poll()
    if build_data.get("building", True) or not build_data.get("result"):
        raise jenkinsapi.custom_exceptions.NotBuiltYet()
    build.poll()
    return build_number, build


def test_service_process_user_retries_when_main_pid_exits():
    """
    Arrange: the first process lookup sees a stale systemd main PID.
    Act: invoke the decorated process-user helper with no artificial wait.
    Assert: it refreshes the PID and returns the eventual process user.
    """

    class Juju:
        def __init__(self):
            self.process_lookup_count = 0
            self.process_commands = []

        def cli(self, *args):
            if "systemctl" in args:
                return "100\n"
            self.process_commands.append(args)
            self.process_lookup_count += 1
            if self.process_lookup_count == 1:
                raise CLIError(1, list(args), "", "process exited")
            return "jenkins\n"

    juju = Juju()
    user = cast(Any, _service_process_user).retry_with(
        stop=stop_after_attempt(2), wait=wait_fixed(0)
    )(juju=juju, unit_name="jenkins-agent/0")

    assert user == "jenkins"
    assert juju.process_lookup_count == 2
    assert all(command[2] == "--" for command in juju.process_commands)


def _run_job(*, job: jenkinsapi.job.Job, expected_status: str = "SUCCESS") -> tuple[int, str]:
    """Run one job and return its build number and console output."""
    queue_item = job.invoke()
    build_number, build = _wait_for_build(job=job, queue_item=queue_item)
    status = build.get_status()
    console = build.get_console()
    if status != expected_status:
        logger.error("Jenkins build %s failed; console:\n%s", build, console)
    assert status == expected_status, console
    return build_number, console


def _unit_name(juju: jubilant.Juju, application: str) -> str:
    """Return the single unit currently belonging to an application."""
    units = juju.status().get_units(application)
    assert len(units) == 1, f"Expected one unit for {application}, found {list(units)}"
    return next(iter(units))


_SERVICE_USER_RETRY_ERRORS = (CLIError, ValueError)


@retry(
    retry=retry_if_exception_type(_SERVICE_USER_RETRY_ERRORS),
    stop=stop_after_attempt(12),
    wait=wait_fixed(5),
    reraise=True,
)
def _service_process_user(juju: jubilant.Juju, unit_name: str) -> str:
    """Return the OS user of the systemd agent's main process."""
    pid = juju.cli(
        "ssh", unit_name, "sudo", "systemctl", "show", "-p", "MainPID", "--value", "jenkins-agent"
    ).strip()
    if not pid or pid == "0":
        raise ValueError("jenkins-agent has no main process")
    user = juju.cli("ssh", unit_name, "--", "sudo", "ps", "-o", "user=", "-p", pid).strip()
    if not user:
        raise ValueError(f"jenkins-agent process {pid} has no OS user")
    return user


def _service_is_active(juju: jubilant.Juju, unit_name: str) -> bool:
    """Return whether the remote systemd agent service is active."""
    try:
        return (
            juju.cli("ssh", unit_name, "sudo", "systemctl", "is-active", "jenkins-agent").strip()
            == "active"
        )
    except CLIError:
        return False


def _stat_entries(
    juju: jubilant.Juju, unit_name: str, paths: list[str]
) -> dict[str, tuple[int, int, int, int, str]]:
    """Return numeric uid/gid/mode/inode/type for remote paths."""
    output = juju.cli("ssh", unit_name, "sudo", "stat", "-c", "%u:%g:%a:%i:%F", *paths)
    entries = {}
    for path, line in zip(paths, output.splitlines(), strict=True):
        uid, gid, mode, inode, kind = line.split(":", 4)
        entries[path] = (int(uid), int(gid), int(mode), int(inode), kind)
    return entries


def _remote_exists(juju: jubilant.Juju, unit_name: str, path: str) -> bool:
    """Return whether a path exists on the agent unit."""
    try:
        juju.cli("ssh", unit_name, "sudo", "test", "-e", path)
    except CLIError:
        return False
    return True


def assert_job_success(
    *, client: jenkinsapi.jenkins.Jenkins, agent_name: str, test_target_label: str
):
    """Assert that a job can be created and run successfully."""
    job = client.create_job(agent_name, _gen_test_job_xml(test_target_label))
    _run_job(job=job)


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
    _, console = _run_job(job=job)
    assert f"agent-user={JENKINS_AGENT_USER}" in console
    # Jenkins runs freestyle jobs in a workspace below the node remote FS.
    # The controller may export its own JENKINS_HOME to build processes; the
    # workspace path is the authoritative agent-home check.
    assert f"workdir={JENKINS_AGENT_HOME}/workspace/" in console


def _wait_for_agent_online(
    jenkins_client: jenkinsapi.jenkins.Jenkins,
    agent_name: str,
    *,
    online: bool = True,
    timeout: int = 600,
) -> bool:
    """Wait for a Jenkins agent to reach the requested online state."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            node = jenkins_client.get_node(agent_name)
            if node.is_online() == online:
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
    Arrange: deploy one root-running revision 265 agent with a real Jenkins workspace.
    Act: refresh the same unit, exercise a nested ownership failure, then run the action.
    Assert: atomic files, in-place ownership migration, and the same job all behave correctly.
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
    unit_name = _unit_name(juju, LEGACY_AGENT_APPLICATION_NAME)
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
    legacy_node = _configure_agent_remote_fs(
        jenkins_client, agent_name, remote_fs=LEGACY_AGENT_HOME
    )
    assert legacy_node.get_config_element("remoteFS") == LEGACY_AGENT_HOME
    juju.cli("ssh", unit_name, "sudo", "systemctl", "restart", "jenkins-agent")
    assert _wait_for_agent_online(jenkins_client, agent_name), (
        f"Legacy agent {agent_name} did not reconnect after setting remoteFS"
    )
    assert _service_process_user(juju, unit_name) == "root"

    job_name = f"{agent_name}-rev265-upgrade"
    job = jenkins_client.create_job(
        job_name,
        _gen_test_job_xml(
            LEGACY_AGENT_LABEL,
            "set -eu; if [ ! -e upgrade-proof ]; then "
            'printf "workspace-write-ok\\n" > upgrade-proof; else '
            'printf "workspace-updated\\n" > upgrade-proof; fi; '
            'printf "agent-user=%s\\n" "$(id -un)"',
        ),
    )
    first_number, first_console = _run_job(job=job)
    assert "agent-user=root" in first_console

    proof_path = f"{LEGACY_AGENT_HOME}/workspace/{job_name}/upgrade-proof"
    runtime_paths = [
        f"{LEGACY_AGENT_HOME}/agent.jar",
        f"{LEGACY_AGENT_HOME}/.ready",
        f"{LEGACY_AGENT_HOME}/remoting",
        f"{LEGACY_AGENT_HOME}/workspace",
        proof_path,
    ]
    root_uid = int(juju.cli("ssh", unit_name, "sudo", "id", "-u", "root"))
    root_gid = int(juju.cli("ssh", unit_name, "sudo", "id", "-g", "root"))
    before = _stat_entries(juju, unit_name, runtime_paths)
    assert all(before[path][0:2] == (root_uid, root_gid) for path in runtime_paths)
    before_inodes = {path: before[path][3] for path in runtime_paths}
    assert before[proof_path][4] == "regular file"

    # The candidate detects the old top-level ownership and deliberately does not start.
    juju.refresh(LEGACY_AGENT_APPLICATION_NAME, path=charm)

    def candidate_blocked(status: jubilant.Status) -> bool:
        application = status.apps.get(LEGACY_AGENT_APPLICATION_NAME)
        unit = status.get_units(LEGACY_AGENT_APPLICATION_NAME).get(unit_name)
        return bool(
            application
            and application.charm_origin == "local"
            and unit
            and unit.workload_status.current == "blocked"
            and "migrate-runtime-directory" in (unit.workload_status.message or "")
        )

    blocked_status = juju.wait(candidate_blocked, timeout=60 * 20)
    assert (
        blocked_status.get_units(LEGACY_AGENT_APPLICATION_NAME)[unit_name].workload_status.current
        == "blocked"
    )
    assert not _service_is_active(juju, unit_name)
    assert not _remote_exists(juju, unit_name, f"{LEGACY_AGENT_HOME}/.ready")
    assert _wait_for_agent_online(jenkins_client, agent_name, online=False), (
        f"Legacy agent {agent_name} should be offline before ownership repair"
    )

    # Prepare only the top-level paths so the candidate can connect while the existing
    # workspace proof remains root-owned. This isolates the nested-file failure case.
    juju.cli(
        "ssh",
        unit_name,
        "sudo",
        "chown",
        "-R",
        "jenkins:jenkins",
        f"{LEGACY_AGENT_HOME}/remoting",
    )
    juju.cli(
        "ssh",
        unit_name,
        "sudo",
        "chown",
        "jenkins:jenkins",
        f"{LEGACY_AGENT_HOME}/workspace",
    )
    # A config event provides a deterministic second reconcile without replacing the unit.
    juju.config(
        LEGACY_AGENT_APPLICATION_NAME,
        {"jenkins_agent_labels": f"{LEGACY_AGENT_LABEL},migration-test"},
    )
    juju.wait(jubilant.all_active, timeout=60 * 15)
    assert _wait_for_agent_online(jenkins_client, agent_name), (
        f"Candidate agent {agent_name} did not reconnect after top-level preparation"
    )
    assert _service_process_user(juju, unit_name) == "jenkins"
    node_config = jenkins_client.get_node(agent_name).get_config_element("remoteFS")
    assert node_config == LEGACY_AGENT_HOME

    candidate_paths = _stat_entries(juju, unit_name, runtime_paths)
    jenkins_uid = int(juju.cli("ssh", unit_name, "sudo", "id", "-u", "jenkins"))
    jenkins_gid = int(juju.cli("ssh", unit_name, "sudo", "id", "-g", "jenkins"))
    assert candidate_paths[f"{LEGACY_AGENT_HOME}/agent.jar"][0:2] == (jenkins_uid, jenkins_gid)
    assert candidate_paths[f"{LEGACY_AGENT_HOME}/.ready"][0:2] == (jenkins_uid, jenkins_gid)
    assert candidate_paths[proof_path][0:2] == (root_uid, root_gid)
    assert candidate_paths[proof_path][3] == before_inodes[proof_path]
    assert (
        candidate_paths[f"{LEGACY_AGENT_HOME}/workspace"][3]
        == before_inodes[f"{LEGACY_AGENT_HOME}/workspace"]
    )
    assert (
        candidate_paths[f"{LEGACY_AGENT_HOME}/agent.jar"][3]
        != before_inodes[f"{LEGACY_AGENT_HOME}/agent.jar"]
    )
    assert (
        candidate_paths[f"{LEGACY_AGENT_HOME}/.ready"][3]
        != before_inodes[f"{LEGACY_AGENT_HOME}/.ready"]
    )

    # The same existing job now tries to overwrite the root-owned nested proof.
    second_number, second_console = _run_job(job=job, expected_status="FAILURE")
    assert second_number != first_number
    assert "Permission denied" in second_console

    # Run the explicit ownership action against the configured Jenkins home. It
    # stops the active service before migration and restores it afterward.
    action_output = juju.cli(
        "run",
        "--format=json",
        "--wait=20m",
        unit_name,
        "migrate-runtime-directory",
    )
    action = json.loads(action_output)[unit_name]
    assert action["status"] == "completed", action_output
    assert action["results"]["directory"] == LEGACY_AGENT_HOME
    assert action["results"]["user"] == "jenkins"
    assert action["results"]["return-code"] == 0
    assert action["results"]["service-restarted"] in (True, "true")
    assert _service_is_active(juju, unit_name)

    migrated = _stat_entries(juju, unit_name, runtime_paths)
    assert all(migrated[path][0:2] == (jenkins_uid, jenkins_gid) for path in runtime_paths)
    assert migrated[proof_path][3] == before_inodes[proof_path]
    assert juju.cli("ssh", unit_name, "sudo", "cat", proof_path).strip() == "workspace-write-ok"
    for archive_name in (".jenkins-agent-legacy-remoting", ".jenkins-agent-legacy-workspace"):
        with pytest.raises(CLIError):
            juju.cli("ssh", unit_name, "sudo", "test", "-e", f"{LEGACY_AGENT_HOME}/{archive_name}")

    assert _wait_for_agent_online(jenkins_client, agent_name), (
        f"Agent {agent_name} did not reconnect after ownership migration"
    )
    third_number, third_console = _run_job(job=job, expected_status="SUCCESS")
    assert third_number != second_number
    assert f"agent-user={jenkins_uid}" in third_console
    assert juju.cli("ssh", unit_name, "sudo", "cat", proof_path).strip() == "workspace-updated"

    # A later restart must retain the same migrated workspace and its contents.
    juju.cli("ssh", unit_name, "sudo", "systemctl", "restart", "jenkins-agent")
    assert _wait_for_agent_online(jenkins_client, agent_name), (
        f"Agent {agent_name} did not reconnect after a second restart"
    )
    final_paths = _stat_entries(juju, unit_name, [proof_path])
    assert final_paths[proof_path][3] == before_inodes[proof_path]


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
