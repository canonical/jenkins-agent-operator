# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

"""The agent pebble service module."""

import logging
import os
import pwd
import time
from pathlib import Path

from charms.operator_libs_linux.v0 import apt
from charms.operator_libs_linux.v1 import systemd
from jinja2 import Template

from charm_state import State

logger = logging.getLogger(__name__)
AGENT_SERVICE_NAME = "jenkins-agent"
APT_PACKAGE_NAME = "jenkins-agent"
APT_PACKAGE_VERSION = "1.0.4"
SYSTEMD_SERVICE_CONF_DIR = "/etc/systemd/system/jenkins-agent.service.d/"
PPA_URI = "https://ppa.launchpadcontent.net/canonical-is-devops/jenkins-agent-charm/ubuntu/"
PPA_DEB_SRC = "deb-https://ppa.launchpadcontent.net/canonical-is-devops/jenkins-agent-charm/ubuntu/-"  # noqa: E501 pylint: disable=line-too-long
PPA_GPG_KEY_ID = "ad4196d35c25cdac"
STARTUP_CHECK_TIMEOUT = 30
STARTUP_CHECK_INTERVAL = 2
JENKINS_HOME = Path("/var/lib/jenkins")
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
            return systemd.service_running(AGENT_SERVICE_NAME)
        except SystemError as exc:
            logger.error("Failed to call systemctl:\n%s", exc)
            return False

    def install(self) -> None:
        """Install and set up the jenkins agent apt package.

        Raises:
            PackageInstallError: if the package installation failed.
        """
        try:
            # Add ppa that hosts the jenkins-agent package
            series = self.state.unit_data.series
            repositories = apt.RepositoryMapping()
            if f"{PPA_DEB_SRC}-{series}" not in repositories:
                repositories.add(
                    apt.DebianRepository(
                        enabled=True,
                        repotype="deb",
                        uri=PPA_URI,
                        release=series,
                        groups=["main"],
                    )
                )
                apt.import_key(PPA_GPG_KEY_ID)
            # Install the necessary packages
            apt.update()
            apt.add_package("openjdk-17-jre")
            apt.add_package(package_names=APT_PACKAGE_NAME, version=APT_PACKAGE_VERSION)
        except (apt.PackageError, apt.PackageNotFoundError, apt.GPGKeyError) as exc:
            raise PackageInstallError("Error installing the agent package") from exc

    def restart(self) -> None:
        """Start the agent service.

        Raises:
            ServiceRestartError: when restarting the service fails
        """
        # Render template and write to appropriate file if only credentials are set
        credentials = self.state.agent_relation_credentials
        if not credentials:
            raise ServiceRestartError("Error starting the agent service: missing configuration")

        with open("templates/jenkins_agent_env.conf.j2", "r", encoding="utf-8") as file:
            template = Template(file.read())
        # fetch credentials and set them as environments
        environments = {
            "JENKINS_TOKEN": credentials.secret,
            "JENKINS_URL": credentials.address,
            "JENKINS_AGENT": self.state.agent_meta.name,
        }
        # render template file
        rendered = template.render(environments=environments)
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
            service_up = os.path.exists(str(AGENT_READY_PATH)) and self.is_active
            if service_up:
                break
        return os.path.exists(str(AGENT_READY_PATH)) and self.is_active
