# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.

"""The agent pebble service module."""

import logging
import time

from charms.operator_libs_linux.v2 import snap

from charm_state import State

logger = logging.getLogger(__name__)
SNAP_NAME = "jenkins-agent"
READINESS_CHECK_DELAY = 5


class SnapInstallError(Exception):
    """Exception raised when package installation fails."""


class SnapServiceStartError(Exception):
    """Exception raised when failing to start the agent service."""


class JenkinsAgentService:
    """Jenkins agent service class.

    Attrs:
       is_active: Indicate if the agent service is active and running.
    """

    def __init__(self, state: State):
        """Initialize the jenkins agent service.

        Args:
            state: The Jenkins agent state.
        """
        self.state = state

    @property
    def is_active(self) -> bool:
        """Indicate if the jenkins agent snap is active."""
        cache = snap.SnapCache()
        agent = cache[SNAP_NAME]
        service: dict = agent.services.get(SNAP_NAME)
        return bool(service.get("active"))

    def install(self):
        """Install and set up the snap.

        Raises:
            SnapInstallError: if the installation of the snap failed.
        """
        try:
            cache = snap.SnapCache()
            agent = cache[SNAP_NAME]

            if not agent.present:
                agent.ensure(snap.SnapState.Latest, classic=False, channel="latest/edge")
        except snap.SnapError as exc:
            raise SnapInstallError("Error installing the agent charm") from exc

    def start(self):
        """Start the agent service."""
        cache = snap.SnapCache()
        agent = cache[SNAP_NAME]
        agent.set(
            {
                "jenkins.token": self.state.agent_relation_credentials.secret,
                "jenkins.url": self.state.agent_relation_credentials.address,
                "jenkins.agent": self.state.agent_meta.name,
            }
        )
        agent.start()

    def stop(self) -> None:
        """Stop the agent service."""
        cache = snap.SnapCache()
        agent = cache[SNAP_NAME]
        agent.stop()

    def restart(self) -> None:
        """Restart the agent service.

        Raises:
            SnapServiceStartError: if the readiness check failed.
        """
        self.stop()
        self.start()
        if not self._readiness_check():
            raise SnapServiceStartError("Failed readiness check")

    def _readiness_check(self) -> bool:
        """Check whether the service was correctly started.

        Returns:
            bool: indicate whether the service was started.
        """
        time.sleep(READINESS_CHECK_DELAY)
        return self.is_active
