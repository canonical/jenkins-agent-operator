# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""The agent pebble service module."""

import logging
import os
import pwd
import re
import stat

# Bandit flags the subprocess import; useradd/visudo are trusted fixed-path system binaries.
import subprocess  # nosec: B404
import time
import typing
from pathlib import Path

import jinja2
from charms.operator_libs_linux.v0 import apt
from charms.operator_libs_linux.v1 import systemd

from charm_state import Credentials, State

logger = logging.getLogger(__name__)
AGENT_SERVICE_NAME = "jenkins-agent"
REQUIRED_PACKAGES = ["openjdk-21-jre", "sudo"]
SYSTEMD_SERVICE_CONF_DIR = "/etc/systemd/system/jenkins-agent.service.d/"
STARTUP_CHECK_TIMEOUT = 30
STARTUP_CHECK_INTERVAL = 2
JENKINS_AGENT_SYSTEMD_PATH = Path("/etc/systemd/system/jenkins-agent.service")
JENKINS_AGENT_START_SCRIPT_PATH = Path("/usr/bin/jenkins-agent")
SUDOERS_DROP_IN_DIR = Path("/etc/sudoers.d")
# This state is deliberately outside the user-writable Jenkins home. It records
# the last completed ownership migration so update-status hooks remain cheap.
OWNERSHIP_MIGRATION_STATE_DIR = Path("/var/lib/jenkins-agent")
OWNERSHIP_MIGRATION_STATE_PATH = OWNERSHIP_MIGRATION_STATE_DIR / "home-ownership"
_OWNERSHIP_MIGRATION_VERSION = "1"

# Pattern for systemd Environment="KEY=VALUE" lines.
_SYSTEMD_ENV_PATTERN = re.compile(r'^Environment="([^=]+)=(.*)"$')


def _parse_systemd_env(content: str) -> typing.Dict[str, str]:
    """Parse Environment directives from a systemd override.conf file.

    Args:
        content: The file content to parse.

    Returns:
        A dictionary of environment variable key-value pairs.
    """
    env: typing.Dict[str, str] = {}
    for line in content.splitlines():
        match = _SYSTEMD_ENV_PATTERN.match(line.strip())
        if match:
            value = match.group(2)
            env[match.group(1)] = (
                value.replace('\\"', '"').replace("\\\\", "\\").replace("%%", "%")
            )
    return env


class PackageInstallError(Exception):
    """Exception raised when package installation fails."""


class ServiceRestartError(Exception):
    """Exception raised when failing to start the agent service."""


class ServiceStopError(Exception):
    """Exception raised when failing to stop the agent service."""


class FileRenderError(Exception):
    """Exception raised when failing to interact with a file in the filesystem."""


def _systemd_quote(value: str) -> str:
    """Escape a value for a double-quoted systemd Environment assignment."""
    if any(character in value for character in "\x00\r\n"):
        raise ValueError("systemd environment values cannot contain control characters")
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")


class JenkinsAgentService:
    """Jenkins agent service class.

    Attrs:
       is_ready: Indicate if the agent service is active and running.
    """

    def __init__(self, state: State):
        """Initialize the jenkins agent service.

        Args:
            state: The Jenkins agent state.
        """
        self.state = state
        self._template_loader = jinja2.Environment(
            loader=jinja2.FileSystemLoader("templates"),
            autoescape=jinja2.select_autoescape(
                enabled_extensions=("html", "htm", "xml"),
                default=False,
                default_for_string=False,
            ),
        )
        self._template_loader.filters["systemd_quote"] = _systemd_quote

    def _render_file(self, path: Path, content: str, mode: int, owner: str = "root") -> None:
        """Write a file to disk, setting its mode and ownership.

        Args:
            path: target file path.
            content: file content to write.
            mode: permission bits to apply (e.g. 0o640).
            owner: POSIX username the file should be owned by. Defaults to root,
                which is appropriate for systemd unit files and the launcher script
                because systemd itself runs as root and only drops privileges when
                executing the service (see the `User=` directive in the unit).

        Raises:
            FileRenderError: if interaction with the filesystem fails
        """
        try:
            path.write_text(content)
            os.chmod(path, mode)
            user_info = pwd.getpwnam(owner)
            os.chown(path, uid=user_info.pw_uid, gid=user_info.pw_gid)
        except (OSError, KeyError, TypeError) as exc:
            raise FileRenderError(f"Error rendering file:\n{exc}") from exc

    def _write_if_changed(self, path: Path, content: str, mode: int, owner: str = "root") -> bool:
        """Render content to a file only when it differs from what is on disk.

        Args:
            path: Destination file path.
            content: Desired file content.
            mode: Access permission mask applied when the file is (re)written.
            owner: POSIX username the file should be owned by.

        Returns:
            True if the file was created or its content changed, False otherwise.

        Raises:
            FileRenderError: if reading the existing file from disk fails.
        """
        if self._file_matches(path, content):
            return False
        self._render_file(path, content, mode, owner=owner)
        return True

    @property
    def is_running(self) -> bool:
        """Indicate if the systemd service is running, independent of readiness."""
        try:
            return systemd.service_running(AGENT_SERVICE_NAME)
        except SystemError as exc:
            logger.error("Failed to call systemctl:")
            raise RuntimeError("Failed to query the agent service") from exc

    @property
    def is_ready(self) -> bool:
        """Indicate if the agent is running and has created its ready marker."""
        return self.is_running and self.state.jenkins_home.joinpath(".ready").exists()

    def credentials_changed(self, credentials: Credentials) -> bool:
        """Return whether the running override differs from desired configuration."""
        config_file = Path(f"{SYSTEMD_SERVICE_CONF_DIR}/override.conf")
        if not config_file.exists():
            return True
        current_env = _parse_systemd_env(config_file.read_text())
        return (
            current_env.get("JENKINS_URL") != credentials.address
            or current_env.get("JENKINS_TOKEN") != credentials.secret
        )

    def configuration_changed(self, credentials: typing.Optional[Credentials] = None) -> bool:
        """Return whether actual service configuration differs from desired state."""
        if self.service_files_changed():
            return True
        if credentials is None:
            return False
        config_file = Path(f"{SYSTEMD_SERVICE_CONF_DIR}/override.conf")
        if not config_file.exists():
            return True
        current_env = _parse_systemd_env(config_file.read_text())
        return any(
            (
                current_env.get("JENKINS_URL") != credentials.address,
                current_env.get("JENKINS_TOKEN") != credentials.secret,
                current_env.get("JENKINS_AGENT") != self.state.agent_meta.name,
                current_env.get("JENKINS_HOME") != str(self.state.jenkins_home),
            )
        )

    def _render_service_files(self) -> typing.Tuple[str, str]:
        """Render desired systemd and launcher contents without writing them."""
        unit_template = self._template_loader.get_template("jenkins_agent.service")
        service_content = unit_template.render(
            agent_user=self.state.agent_user,
            jenkins_home=str(self.state.jenkins_home),
        )
        script_template = self._template_loader.get_template("jenkins_agent.sh.j2")
        script_content = script_template.render(
            websocket_mode=self.state.websocket_mode,
            jenkins_home=str(self.state.jenkins_home),
        )
        return service_content, script_content

    def _file_matches(self, path: Path, content: str) -> bool:
        """Return whether a file contains the desired content."""
        try:
            return path.exists() and path.read_text(encoding="utf-8") == content
        except OSError as exc:
            raise FileRenderError(f"Error reading {path}:\n{exc}") from exc

    def service_files_changed(self) -> bool:
        """Return whether desired service files differ from files on disk."""
        service_content, script_content = self._render_service_files()
        return not (
            self._file_matches(JENKINS_AGENT_SYSTEMD_PATH, service_content)
            and self._file_matches(JENKINS_AGENT_START_SCRIPT_PATH, script_content)
        )

    def _sync_service_files(self) -> bool:
        """Write the systemd unit and its launcher script if they've changed.

        The systemd unit and the shell script run by ExecStart are shipped as
        non-templated files in templates/. We read them here and write them only if
        their contents differ, so an upgrade that changes a template is always
        picked up while an unchanged reconcile is a no-op.

        Returns:
            True if the systemd unit file changed (a daemon reload is required),
            False otherwise.
        """
        service_content, script_content = self._render_service_files()
        unit_changed = self._write_if_changed(JENKINS_AGENT_SYSTEMD_PATH, service_content, 0o644)

        script_changed = self._write_if_changed(
            JENKINS_AGENT_START_SCRIPT_PATH, script_content, 0o755
        )
        return unit_changed or script_changed

    def _required_packages_installed(self) -> bool:
        """Check whether every required apt package is already installed.

        Returns:
            True if all required packages are present, False otherwise.
        """
        for package in REQUIRED_PACKAGES:
            try:
                apt.DebianPackage.from_installed_package(package)
            except apt.PackageNotFoundError:
                return False
        return True

    def install(self) -> bool:
        """Converge the agent service files and required packages to desired state.

        Idempotent and safe to run on every reconcile: the systemd unit and launch
        script are re-rendered whenever their contents drift from the shipped
        templates (reloading and enabling the unit when the unit file changes), and
        the required apt packages are installed only when missing.

        Returns:
            Whether the rendered service files changed and require a restart.

        Raises:
            PackageInstallError: if enabling the service or installing a package
                failed.
        """
        if not self._required_packages_installed():
            try:
                apt.add_package(REQUIRED_PACKAGES, update_cache=True)
            except (apt.PackageError, apt.PackageNotFoundError) as exc:
                raise PackageInstallError("Error installing the required packages") from exc

        self._ensure_user_and_home()
        unit_file_changed = self._sync_service_files()
        if unit_file_changed:
            try:
                systemd.daemon_reload()
                # Enable the unit so its [Install] WantedBy target is wired up and
                # the agent starts automatically after a machine reboot.
                systemd.service_enable(AGENT_SERVICE_NAME)
            except systemd.SystemdError as exc:
                raise PackageInstallError("Error enabling the agent service") from exc
        return unit_file_changed

    def _ensure_user_and_home(self) -> None:
        """Ensure the configured agent user exists, owns the home, and can sudo.

        Does nothing for the root user. For non-root users, the user is created
        (regular user, not a system account) with the configured home directory if
        missing, the home directory and legacy contents are owned by the user, and
        a passwordless sudo entry is written to /etc/sudoers.d. Failures raise
        PackageInstallError so the charm does not enable a broken service.
        """
        username = self.state.agent_user
        home = self.state.jenkins_home
        if username == "root":
            return

        try:
            pwd.getpwnam(username)
        except KeyError:
            logger.info("Creating user %s", username)
            try:
                subprocess.run(  # nosec: B603 - fixed-path system binary (useradd)
                    [
                        "/usr/sbin/useradd",
                        "--home-dir",
                        str(home),
                        "--create-home",
                        "--shell",
                        "/bin/bash",
                        username,
                    ],
                    check=True,
                    capture_output=True,
                )
            except (subprocess.CalledProcessError, FileNotFoundError) as exc:
                raise PackageInstallError(f"Failed to create user {username}") from exc
        try:
            if home.is_symlink() or any(parent.is_symlink() for parent in home.parents):
                raise OSError(f"Jenkins home must not contain symbolic links: {home}")
            home.mkdir(parents=True, exist_ok=True)
            user_info = pwd.getpwnam(username)
        except (OSError, KeyError) as exc:
            raise PackageInstallError(f"Failed to prepare Jenkins home {home}") from exc

        self._migrate_home_ownership(home, user_info.pw_uid, user_info.pw_gid)
        self._grant_passwordless_sudo(username)

    @staticmethod
    def _ownership_fingerprint(home: Path, uid: int, gid: int) -> str:
        """Return the durable key for a completed home ownership migration."""
        return f"{_OWNERSHIP_MIGRATION_VERSION}\n{home}\n{uid}\n{gid}\n"

    @staticmethod
    def _chown_home_directories(
        dirfd: int, dirnames: typing.List[str], home_device: int, uid: int, gid: int
    ) -> None:
        """Re-own safe child directories and prune unsafe traversal targets."""
        for dirname in dirnames[:]:
            try:
                entry = os.stat(dirname, dir_fd=dirfd, follow_symlinks=False)
            except FileNotFoundError:
                dirnames.remove(dirname)
                continue
            if stat.S_ISLNK(entry.st_mode) or entry.st_dev != home_device:
                dirnames.remove(dirname)
                continue
            if entry.st_uid != uid or entry.st_gid != gid:
                try:
                    os.chown(
                        dirname,
                        uid,
                        gid,
                        dir_fd=dirfd,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    dirnames.remove(dirname)

    @staticmethod
    def _chown_home_files(
        dirfd: int, filenames: typing.Iterable[str], home_device: int, uid: int, gid: int
    ) -> None:
        """Re-own regular entries on the home filesystem without following links."""
        for filename in filenames:
            try:
                entry = os.stat(filename, dir_fd=dirfd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(entry.st_mode) or entry.st_dev != home_device:
                continue
            if entry.st_uid != uid or entry.st_gid != gid:
                try:
                    os.chown(
                        filename,
                        uid,
                        gid,
                        dir_fd=dirfd,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    continue

    @staticmethod
    def _chown_home_tree(home: Path, uid: int, gid: int) -> None:
        """Re-own entries on the home filesystem without following symlinks.

        ``os.fwalk`` keeps directory file descriptors anchored while traversing,
        and relative ``stat``/``chown`` calls with ``follow_symlinks=False`` avoid
        changing an object outside the home if an entry is replaced concurrently.
        Mount points below the home are not traversed or re-owned.
        """
        home_device: typing.Optional[int] = None
        for _, dirnames, filenames, dirfd in os.fwalk(home, topdown=True, follow_symlinks=False):
            current_dir = os.fstat(dirfd)
            if home_device is None:
                home_device = current_dir.st_dev
            if current_dir.st_dev != home_device:
                continue
            if current_dir.st_uid != uid or current_dir.st_gid != gid:
                os.fchown(dirfd, uid, gid)
            JenkinsAgentService._chown_home_directories(dirfd, dirnames, home_device, uid, gid)
            JenkinsAgentService._chown_home_files(dirfd, filenames, home_device, uid, gid)

    def _migrate_home_ownership(self, home: Path, uid: int, gid: int) -> None:
        """Migrate legacy home contents once for this path and account identity."""
        fingerprint = self._ownership_fingerprint(home, uid, gid)
        state_dir = OWNERSHIP_MIGRATION_STATE_DIR
        state_path = OWNERSHIP_MIGRATION_STATE_PATH
        try:
            if state_path.is_symlink():
                raise OSError(
                    f"Ownership migration state must not be a symbolic link: {state_path}"
                )
            if state_path.is_file() and state_path.read_text(encoding="utf-8") == fingerprint:
                return
        except OSError as exc:
            raise PackageInstallError("Failed to inspect ownership migration state") from exc

        if home == state_dir or home.is_relative_to(state_dir) or state_dir.is_relative_to(home):
            raise PackageInstallError("Ownership migration state must be outside Jenkins home")

        try:
            if self.is_running:
                systemd.service_stop(AGENT_SERVICE_NAME)
        except (RuntimeError, systemd.SystemdError) as exc:
            raise PackageInstallError(
                "Failed to stop the agent before ownership migration"
            ) from exc

        try:
            self._chown_home_tree(home, uid, gid)
            state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            if state_dir.is_symlink() or state_path.is_symlink():
                raise OSError("Ownership migration state must not be a symbolic link")
            os.chmod(state_dir, 0o700)
            os.chown(state_dir, uid=0, gid=0)
            state_path.write_text(fingerprint, encoding="utf-8")
            os.chmod(state_path, 0o600)
            os.chown(state_path, uid=0, gid=0)
        except OSError as exc:
            raise PackageInstallError("Failed to migrate Jenkins home ownership") from exc

    def _grant_passwordless_sudo(self, username: str) -> None:
        """Validate and write a passwordless sudo rule for the agent user."""
        sudoers_content = f"{username} ALL=(ALL:ALL) NOPASSWD: ALL\n"
        drop_in_path = SUDOERS_DROP_IN_DIR / "99-jenkins-agent"
        try:
            subprocess.run(  # nosec: B603 - fixed-path system binary (visudo)
                ["/usr/sbin/visudo", "-cf", "-"],
                input=sudoers_content,
                check=True,
                capture_output=True,
                text=True,
            )
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            raise PackageInstallError("Generated sudoers content failed validation") from exc
        try:
            SUDOERS_DROP_IN_DIR.mkdir(parents=True, exist_ok=True)
            drop_in_path.write_text(sudoers_content)
            os.chmod(drop_in_path, 0o440)
            os.chown(drop_in_path, uid=0, gid=0)
        except (OSError, KeyError) as exc:
            raise PackageInstallError(f"Failed to write sudoers drop-in {drop_in_path}") from exc

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
            "JENKINS_HOME": str(self.state.jenkins_home),
        }
        # render template file
        agent_env_conf_template = self._template_loader.get_template("jenkins_agent_env.conf.j2")
        rendered = agent_env_conf_template.render(environments=environments)
        # Ensure that service conf directory exist
        config_dir = Path(SYSTEMD_SERVICE_CONF_DIR)
        config_dir.mkdir(parents=True, exist_ok=True)
        # Write the conf file
        logger.info("Rendering agent configuration")
        logger.debug("Rendering agent environment keys: %s", sorted(environments))
        config_file = Path(f"{SYSTEMD_SERVICE_CONF_DIR}/override.conf")
        try:
            self._render_file(config_file, rendered, 0o600)
            systemd.daemon_reload()
            systemd.service_restart(AGENT_SERVICE_NAME)
        except systemd.SystemdError as exc:
            raise ServiceRestartError(f"Error starting the agent service:\n{exc}") from exc
        except FileRenderError as exc:
            raise ServiceRestartError(
                "Error interacting with the filesystem when rendering configuration file"
            ) from exc

        # Check if the service running after startup
        if not self._startup_check():
            raise ServiceRestartError("Error waiting for the agent service to start")

    def reset_failed_state(self) -> None:
        """Reset NRestart count of service back to 0.

        The service keeps track of the 'restart-count' and blocks further restarts
        if the maximum allowed is reached. This count is not reset when the service restarts
        so we need to do it manually.
        """
        try:
            # Disable protected_access here because reset-failed is not implemented in the lib
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
            if self.is_ready:
                break
        return self.is_ready
