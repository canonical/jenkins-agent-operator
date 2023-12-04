# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.

"""The agent pebble service module."""

import logging
import typing
from charm_state import State

from charms.operator_libs_linux.v2 import snap

logger = logging.getLogger(__name__)
SNAP_NAME = "jenkins-agent"


class JenkinsAgentService:
    """Jenkins agent service class."""

    def __init__(self, state: State):
        """Initialize the jenkins agent service.

        Args:
            state: The Jenkins agent state.
        """
        self.state = state

    def start(self, server_url: str, agent_token_pair: typing.Tuple[str, str]):
        cache = snap.SnapCache()
        agent = cache[SNAP_NAME]
        agent_name, agent_token = agent_token_pair
        agent.set(
            {
                "jenkins.token": agent_token,
                "jenkins.url": server_url,
                "jenkins.agent": agent_name,
            }
        )
        agent.start()

    def stop(self):
        cache = snap.SnapCache()
        agent = cache[SNAP_NAME]
        agent.stop()
