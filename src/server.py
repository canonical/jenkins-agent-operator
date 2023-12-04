# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.

"""Functions to interact with jenkins server."""

import logging
import typing
from pathlib import Path

import ops
from pydantic import BaseModel

logger = logging.getLogger(__name__)

JENKINS_WORKDIR = Path("/var/lib/jenkins")
AGENT_JAR_PATH = Path(JENKINS_WORKDIR / "agent.jar")
AGENT_READY_PATH = Path(JENKINS_WORKDIR / "agents/.ready")
ENTRYSCRIPT_PATH = Path(JENKINS_WORKDIR / "entrypoint.sh")

USER = "_daemon_"


class Credentials(BaseModel):
    """The credentials used to register to the Jenkins server.

    Attrs:
        address: The Jenkins server address to register to.
        secret: The secret used to register agent.
    """

    address: str
    secret: str


class ServerBaseError(Exception):
    """Represents errors with interacting with Jenkins server."""


def validate_credentials(
    agent_name: str,
    credentials: Credentials,
    container: ops.Container,
) -> bool:
    """Check if the credentials can be used to register to the server.

    Args:
        agent_name: The Jenkins agent name.
        credentials: Server credentials required to register to Jenkins server.
        container: The Jenkins agent workload container.
        add_random_delay: Whether random delay should be added to prevent parallel registration on
            server.

    Returns:
        True if credentials and agent_name pairs are valid, False otherwise.
    """
    proc: ops.pebble.ExecProcess = container.exec(
        [
            "java",
            "-jar",
            str(AGENT_JAR_PATH),
            "-jnlpUrl",
            f"{credentials.address}/computer/{agent_name}/slave-agent.jnlp",
            "-workDir",
            str(JENKINS_WORKDIR),
            "-noReconnect",
            "-secret",
            credentials.secret,
        ],
        timeout=5,
        user=USER,
        working_dir=str(JENKINS_WORKDIR),
        combine_stderr=True,
    )
    # The process will exit due to connection failure(invalid credentials) or timeout.
    # Check for successful connection log from the stdout.
    connected = False
    terminated = False
    lines = ""
    # The proc.stdout is iterable according to process.exec documentation
    for line in proc.stdout:  # type: ignore
        lines += line
        if "INFO: Connected" in line:
            connected = True
        if "INFO: Terminated" in line:
            terminated = True
    logger.debug(lines)
    return connected and not terminated


def find_valid_credentials(
    agent_name_token_pairs: typing.Iterable[typing.Tuple[str, str]],
    server_url: str,
    container: ops.Container,
) -> typing.Optional[typing.Tuple[str, str]]:
    """Find credentials that can be applied if available.

    Args:
        agent_name_token_pairs: Matching agent name and token pair to check.
        server_url: The jenkins server url address.
        container: The Jenkins agent workload container.

    Returns:
        Agent name and token pair that can be used. None if no pair is available.
    """
    for agent_name, agent_token in agent_name_token_pairs:
        logger.debug("Validating %s", agent_name)
        if not validate_credentials(
            agent_name=agent_name,
            credentials=Credentials(address=server_url, secret=agent_token),
            container=container,
        ):
            logger.debug("agent %s validation failed.", agent_name)
            continue
        return (agent_name, agent_token)
    return None
