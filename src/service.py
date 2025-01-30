# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""The agent pebble service module."""

import logging
import os
import pwd
import time
from pathlib import Path

import jinja2
from charms.operator_libs_linux.v0 import apt
from charms.operator_libs_linux.v1 import systemd

from charm_state import State

logger = logging.getLogger(__name__)
AGENT_SERVICE_NAME = "jenkins-agent"
REQUIRED_PACKAGES = ["openjdk-21-jre"]
SYSTEMD_SERVICE_CONF_DIR = "/etc/systemd/system/jenkins-agent.service.d/"
STARTUP_CHECK_TIMEOUT = 30
STARTUP_CHECK_INTERVAL = 2
JENKINS_HOME = Path("/var/lib/jenkins")
JENKINS_AGENT_SYSTEMD_PATH = Path("/etc/systemd/system/jenkins-agent.service")
JENKINS_AGENT_START_SCRIPT_PATH = Path("/usr/bin/jenkins-agent")
AGENT_READY_PATH = Path(JENKINS_HOME / ".ready")


class PackageInstallError(Exception):
    """Exception raised when package installation fails."""


class ServiceRestartError(Exception):
    """Exception raised when failing to start the agent service."""


class ServiceStopError(Exception):
    """Exception raised when failing to stop the agent service."""


class FileRenderError(Exception):
    """Exception raised when failing to interact with a file in the filesystem."""


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
        self._template_loader = jinja2.Environment(
            loader=jinja2.FileSystemLoader(searchpath="templates"), autoescape=True
        )

    def _render_file(self, path: Path, content: str, mode: int) -> None:
        """Write a content rendered from a template to a file.

        Args:
            path: Path object to the file.
            content: the data to be written to the file.
            mode: access permission mask applied to the
              file using chmod (e.g. 0o640).

        Raises:
            FileRenderError: if interaction with the filesystem fails
        """
        try:
            path.write_text(content)
            os.chmod(path, mode)
            # Get the uid/gid for the root user (running the service).
            # TODO: the user running the jenkins agent is currently root
            # we should replace this by defining a dedicated user in the apt package
            u = pwd.getpwnam("root")
            # Set the correct ownership for the file.
            os.chown(path, uid=u.pw_uid, gid=u.pw_gid)
        except (OSError, KeyError, TypeError) as exc:
            raise FileRenderError(f"Error rendering file:\n{exc}") from exc

    @property
    def is_active(self) -> bool:
        """Indicate if the jenkins agent service is active."""
        try:
            return os.path.exists(str(AGENT_READY_PATH)) and systemd.service_running(
                AGENT_SERVICE_NAME
            )
        except SystemError as exc:
            logger.error("Failed to call systemctl:\n%s", exc)
            return False

    def install(self) -> None:
        """Install and set up the jenkins agent apt package.

        Raises:
            PackageInstallError: if the package installation failed.
        """
        agent_service = Path("templates/jenkins_agent.service")
        JENKINS_AGENT_SYSTEMD_PATH.write_text(
            agent_service.read_text(encoding="utf-8"), encoding="utf-8"
        )
        agent_script = Path("templates/jenkins_agent.sh")
        self._render_file(
            JENKINS_AGENT_START_SCRIPT_PATH, agent_script.read_text(encoding="utf-8"), 755
        )

        try:
            apt.add_package(REQUIRED_PACKAGES, update_cache=True)
        except (apt.PackageError, apt.PackageNotFoundError) as exc:
            raise PackageInstallError("Error installing the Java package") from exc

    def restart(self) -> None:
        """Start the agent service.

        Raises:
            ServiceRestartError: when restarting the service fails
        """
        # Render template and write to appropriate file if only credentials are set
        credentials = self.state.agent_relation_credentials
        if not credentials:
            raise ServiceRestartError("Error starting the agent service: missing configuration")

        # fetch credentials and set them as environments
        environments = {
            "JENKINS_TOKEN": credentials.secret,
            "JENKINS_URL": credentials.address,
            "JENKINS_AGENT": self.state.agent_meta.name,
        }
        # render template file
        agent_env_conf_template = self._template_loader.get_template("jenkins_agent_env.conf.j2")
        rendered = agent_env_conf_template.render(environments=environments)
        # Ensure that service conf directory exist
        config_dir = Path(SYSTEMD_SERVICE_CONF_DIR)
        config_dir.mkdir(parents=True, exist_ok=True)
        # Write the conf file
        logger.info("Rendering agent configuration")
        logger.debug("%s", environments)
        config_file = Path(f"{SYSTEMD_SERVICE_CONF_DIR}/override.conf")
        try:
            self._render_file(config_file, rendered, 0o644)
            systemd.daemon_reload()
            systemd.service_restart(AGENT_SERVICE_NAME)
        except systemd.SystemdError as exc:
            raise ServiceRestartError(f"Error starting the agent service:\n{exc}") from exc
        except FileRenderError as exc:
            raise ServiceRestartError(
                "Error interacting with the filesystem when rendering configuration file"
            ) from exc

        # Check if the service is running after startup
        if not self._startup_check():
            raise ServiceRestartError("Error waiting for the agent service to start")

    def reset_failed_state(self) -> None:
        """Reset NRestart count of service back to 0.

        The service keeps track of the 'restart-count' and blocks further restarts
        if the maximum allowed is reached. This count is not reset when the service restarts
        so we need to do it manually.
        """
        try:
            # Disable protected-access here because reset-failed is not implemented in the lib
            systemd._systemctl("reset-failed", AGENT_SERVICE_NAME)  # pylint: disable=W0212
        except systemd.SystemdError:
            # We only log the exception here as this is not critical
            logger.error("Failed to reset failed state")

    def reset(self) -> None:
        """Stop the agent service and clear its configuration file.

        Raises:
            ServiceStopError: if systemctl stop returns a non-zero exit code.
        """
        try:
            systemd.service_stop(AGENT_SERVICE_NAME)
        except systemd.SystemdError as exc:
            logger.error("service %s failed to stop", AGENT_SERVICE_NAME)
            raise ServiceStopError(f"service {AGENT_SERVICE_NAME} failed to stop") from exc
        config_file = Path(f"{SYSTEMD_SERVICE_CONF_DIR}/override.conf")
        config_file.unlink(missing_ok=True)

    def _startup_check(self) -> bool:
        """Check whether the service was correctly started.

        Returns:
            bool: indicate whether the service was started.
        """
        timeout = time.time() + STARTUP_CHECK_TIMEOUT
        while time.time() < timeout:
            time.sleep(STARTUP_CHECK_INTERVAL)
            if self.is_active:
                break
        return self.is_active
