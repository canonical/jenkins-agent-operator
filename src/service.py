# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.

"""The agent pebble service module."""

import logging
import time
import os, pwd

from charms.operator_libs_linux.v0 import apt
from charms.operator_libs_linux.v1 import systemd

from charm_state import State
from jinja2 import Template
from pathlib import Path

logger = logging.getLogger(__name__)
AGENT_SERVICE_NAME = "jenkins-agent"
AGENT_PACKAGE_NAME = "jenkins-agent"
SYSTEMD_SERVICE_CONF_DIR = "etc/systemd/system/jenkins-agent.service.d/"
READINESS_CHECK_DELAY = 30


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
        with open(path, "w+") as file:
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
        return systemd.service_running(AGENT_SERVICE_NAME)

    def install(self) -> None:
        """Install and set up the jenkins agent apt package.

        Raises:
            ServiceRestartError: if the installation of the snap failed.
            PackageInstallError: if the package installation failed.
        """
        try:
            # Add ppa that hosts the jenkins-agent package
            repositories = apt.RepositoryMapping()
            if "deb-ppa.launchpadcontent.net/tphan025/ppa-jammy" not in repositories:
                repositories.add(apt.DebianRepository(
                    enabled=True,
                    repotype="deb",
                    uri="https://ppa.launchpadcontent.net/tphan025/ppa/ubuntu/",
                    # TODO: depends on the series of the charm unit, set the release accordingly
                    release="jammy",
                ))
            # Install the apt package
            apt.update()
            apt.add_package(AGENT_PACKAGE_NAME)
            systemd.service_restart(AGENT_SERVICE_NAME)
        except systemd.SystemdError as exc:
            raise ServiceRestartError("Error starting the agent service") from exc
        except (apt.PackageError, apt.PackageNotFoundError) as exc:
            raise PackageInstallError("Error installing the agent package") from exc

    def restart(self) -> None:
        """Start the agent service."""
        # Render template and write to appropriate file if only credentials are set
        if credentials := self.state.agent_relation_credentials:
            with open("templates/jenkins_agent_env.conf.j2", "r") as file:
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
            config_dir = Path(SYSTEMD_SERVICE_CONF_DIR).mkdir(parents=True, exist_ok=True)
            # Write the conf file
            self._render_file(f"{config_dir}/environment.conf", rendered, 0o644)
        try:
            systemd.service_restart(AGENT_SERVICE_NAME)
        except systemd.SystemdError as exc:
            raise ServiceRestartError("Error starting the agent service") from exc
        # Check if the service is active
        # TODO: handle cases where downloading the binary takes a long time but workload still active
        if not self._readiness_check():
            raise ServiceRestartError(f"Failed readiness check (timed out after {READINESS_CHECK_DELAY} seconds)")

    def stop(self) -> None:
        """Stop the agent service."""
        try:
            systemd.service_stop(AGENT_SERVICE_NAME)
        except systemd.SystemdError as _:
            # TODO: do we raise exception here?
            logger.debug("service %s failed to stop", AGENT_SERVICE_NAME)

    def _readiness_check(self) -> bool:
        """Check whether the service was correctly started.

        Returns:
            bool: indicate whether the service was started.
        """
        time.sleep(READINESS_CHECK_DELAY)
        return self.is_active
