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
AGENT_PACKAGE_NAME = "jenkins-agent"
SYSTEMD_SERVICE_CONF_DIR = "/etc/systemd/system/jenkins-agent.service.d/"
PPA_URI = "https://ppa.launchpadcontent.net/canonical-is-devops/jenkins-agent-charm/ubuntu/"
PPA_GPG_KEY_ID = "67393A94A577DC24"
STARTUP_CHECK_TIMEOUT = 60
STARTUP_CHECK_INTERVAL = 1


class PackageInstallError(Exception):
    """Exception raised when package installation fails."""


class ServiceRestartError(Exception):
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

    def _render_file(self, path: str, content: str, mode: int) -> None:
        """Write a content rendered from a template to a file.

        Args:
            path: the path to the file.
            content: the data to be written to the file.
            mode: access permission mask applied to the
              file using chmod (e.g. 0o640).
        """
        with open(path, "w+", encoding="utf-8") as file:
            file.write(content)
        # Ensure correct permissions are set on the file.
        os.chmod(path, mode)
        try:
            # Get the uid/gid for the root user (running the service).
            # TODO: the user running the jenkins agent is currently root
            # we should replace this by defining a dedicated user in the apt package
            u = pwd.getpwnam("root")
            # Set the correct ownership for the file.
            os.chown(path, uid=u.pw_uid, gid=u.pw_gid)
        except KeyError:
            # Ignore non existing user error when it wasn't created yet.
            pass

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
            repositories = apt.RepositoryMapping()
            if "deb-ppa.launchpadcontent.net/canonical-is-devops/ppa-jammy" not in repositories:
                repositories.add(
                    apt.DebianRepository(
                        enabled=True,
                        repotype="deb",
                        uri=PPA_URI,
                        # TODO: depending on the series of the charm unit
                        # set the release accordingly
                        release="jammy",
                        groups=["main"],
                    )
                )
                apt.import_key(PPA_GPG_KEY_ID)
            # Install the necessary packages
            apt.update()
            apt.add_package("openjdk-17-jre")
            apt.add_package(AGENT_PACKAGE_NAME)

            logger.info("Added ppa, list of repositories: %s", repositories.keys())
        except (apt.PackageError, apt.PackageNotFoundError) as exc:
            raise PackageInstallError("Error installing the agent package") from exc

    def restart(self) -> None:
        """Start the agent service.

        Raises:
            ServiceRestartError: when restarting the service fails
        """
        # Render template and write to appropriate file if only credentials are set
        if credentials := self.state.agent_relation_credentials:
            with open("templates/jenkins_agent_env.conf.j2", "r", encoding="utf-8") as file:
                template = Template(file.read())
            # fetch credentials and set them as environments
            environments = {
                "JENKINS_TOKEN": credentials.secret,
                "JENKINS_URL": credentials.address,
                "JENKINS_AGENT": self.state.agent_meta.name,
            }
            # render template
            rendered = template.render(environments=environments)
            # Ensure that service conf directory exist
            config_dir = Path(SYSTEMD_SERVICE_CONF_DIR)
            config_dir.mkdir(parents=True, exist_ok=True)
            # Write the conf file
            logger.info("Rendering agent configuration")
            logger.debug("%s", environments)
            # file name (override.conf) is important for the service to import envvars
            self._render_file(f"{config_dir.resolve().as_posix()}/override.conf", rendered, 0o644)
        try:
            systemd.service_restart(AGENT_SERVICE_NAME)
            # Check after startup
            timeout = time.time() + STARTUP_CHECK_TIMEOUT
            while time.time() < timeout:
                if self.is_active:
                    return self.is_active
                time.sleep(STARTUP_CHECK_INTERVAL)
            return self.is_active
        except systemd.SystemdError as exc:
            raise ServiceRestartError(f"Error starting the agent service:\n{exc}") from exc

    def stop(self) -> None:
        """Stop the agent service."""
        try:
            systemd.service_stop(AGENT_SERVICE_NAME)
        except systemd.SystemdError:
            # TODO: do we raise exception here?
            logger.debug("service %s failed to stop", AGENT_SERVICE_NAME)
