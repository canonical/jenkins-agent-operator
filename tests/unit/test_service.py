# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.
#
# Learn more about testing at: https://juju.is/docs/sdk/testing

"""Test for service interaction."""

from __future__ import annotations

import os
import pwd

# Bandit flags subprocess in tests; it is only used to build command lists for unit-test mocks.
import subprocess  # nosec: B404
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import MagicMock, PropertyMock, call

import ops.testing
import pytest
from charms.operator_libs_linux.v0 import apt
from charms.operator_libs_linux.v1 import systemd

import charm_state
import service
from charm_state import AGENT_RELATION, Credentials, State

if TYPE_CHECKING:
    from charm import JenkinsAgentCharm


def _begin_with_lazy_service(harness: ops.testing.Harness, *, run_install: bool = False) -> None:
    """Run a minimal hook so lazy charm state/service initialization occurs."""
    harness.begin()
    if run_install:
        harness.charm.on.install.emit()


def _service(harness: ops.testing.Harness) -> service.JenkinsAgentService:
    """Build the service from the charm's current desired state for direct tests."""
    return service.JenkinsAgentService(charm_state.State.from_charm(harness.charm))


def _mock_install_host(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    package_installed: bool = False,
    mock_fs_ownership: bool = True,
) -> SimpleNamespace:
    """Mock host syscalls so JenkinsAgentService.install runs off-host.

    Args:
        monkeypatch: pytest monkeypatch fixture.
        tmp_path: temporary directory the service files are written to.
        package_installed: whether the required apt package is already present.
        mock_fs_ownership: whether to mock os.chmod/os.chown. Disabling it leaves
            os.chmod untouched but still stubs os.chown, so tests can assert real
            filesystem writes without requiring root privileges to change owners.

    Returns:
        Namespace of the patched systemd entry points (daemon_reload, service_enable)
        and the tmp paths for the unit and script files.
    """
    unit_path = tmp_path / "jenkins-agent.service"
    script_path = tmp_path / "jenkins-agent"
    monkeypatch.setattr(service, "JENKINS_AGENT_SYSTEMD_PATH", unit_path)
    monkeypatch.setattr(service, "JENKINS_AGENT_START_SCRIPT_PATH", script_path)
    monkeypatch.setattr(service, "SUDOERS_DROP_IN_DIR", tmp_path / "sudoers.d")
    ownership_state_dir = tmp_path / "ownership-state"
    monkeypatch.setattr(service, "OWNERSHIP_MIGRATION_STATE_DIR", ownership_state_dir)
    monkeypatch.setattr(
        service, "OWNERSHIP_MIGRATION_STATE_PATH", ownership_state_dir / "home-ownership"
    )
    monkeypatch.setattr(
        charm_state,
        "_JENKINS_HOME_PREFIXES",
        (tmp_path, *charm_state._JENKINS_HOME_PREFIXES),
    )

    real_mkdir = Path.mkdir
    real_fwalk = os.fwalk
    test_home_paths = {
        Path("/var/lib/jenkins"): tmp_path / "var-lib-jenkins",
        Path("/srv/jenkins"): tmp_path / "srv-jenkins",
    }

    def fake_mkdir(path, *args, **kwargs):
        if path in test_home_paths:
            return real_mkdir(test_home_paths[path], *args, **kwargs)
        return real_mkdir(path, *args, **kwargs)

    def fake_fwalk(top=".", *args, **kwargs):
        yield from real_fwalk(test_home_paths.get(Path(top), top), *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", fake_mkdir)
    monkeypatch.setattr(os, "fwalk", fake_fwalk)
    if mock_fs_ownership:
        monkeypatch.setattr(os, "chmod", MagicMock())
        monkeypatch.setattr(os, "chown", MagicMock())
    else:
        monkeypatch.setattr(os, "chown", MagicMock())
    fchown = MagicMock()
    monkeypatch.setattr(os, "fchown", fchown)
    daemon_reload = MagicMock()
    service_enable = MagicMock()
    monkeypatch.setattr(systemd, "service_running", MagicMock(return_value=False))
    monkeypatch.setattr(systemd, "daemon_reload", daemon_reload)
    monkeypatch.setattr(systemd, "service_enable", service_enable)
    from_installed = (
        MagicMock() if package_installed else MagicMock(side_effect=apt.PackageNotFoundError)
    )
    monkeypatch.setattr(apt.DebianPackage, "from_installed_package", from_installed)
    monkeypatch.setattr(apt, "add_package", MagicMock())
    _make_fake_useradd(monkeypatch)
    return SimpleNamespace(
        daemon_reload=daemon_reload,
        service_enable=service_enable,
        fchown=fchown,
        unit_path=unit_path,
        script_path=script_path,
    )


@pytest.mark.parametrize(
    "f,error_thrown",
    [
        ("add_package", apt.PackageError),
        ("add_package", apt.PackageNotFoundError),
    ],
)
def test_install_apt_package_gpg_key_error(
    harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, f, error_thrown
):
    """
    arrange: Harness with mocked apt module.
    act: run _on_install hook with methods raising different errors.
    assert: The charm should be in an error state.
    """
    _mock_install_host(monkeypatch, tmp_path)
    _begin_with_lazy_service(harness, run_install=True)
    charm: JenkinsAgentCharm = harness.charm
    monkeypatch.setattr(apt, "add_package", MagicMock())
    monkeypatch.setattr(apt, f, MagicMock(side_effect=[error_thrown]))

    with pytest.raises(RuntimeError, match=r"Error installing the agent service"):
        charm.on.install.emit()


def test_on_install(harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """
    arrange: Harness with mocked apt module.
    act: run initial hook.
    assert: The installation should pass without error and charm in blocked state.
    """
    apt_add_package_mock = MagicMock()
    host = _mock_install_host(monkeypatch, tmp_path)
    monkeypatch.setattr(apt, "add_package", apt_add_package_mock)

    harness.begin_with_initial_hooks()

    # The package is absent (mock always raises), so every reconcile installs it
    # with the expected package list.
    assert apt_add_package_mock.call_count >= 1
    assert apt_add_package_mock.call_args_list[0][0][0] == ["openjdk-21-jre", "sudo"]
    # The unit file is newly written, so the service is reloaded and enabled for
    # automatic start on reboot.
    assert host.service_enable.call_count >= 1

    assert harness.charm.unit.status.name == ops.BlockedStatus.name


def test_install_skips_apt_when_package_present(
    harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """
    arrange: Harness where the required apt package is already installed.
    act: run the install hook to trigger reconcile.
    assert: apt.add_package is not called (the expensive step is gated on presence).
    """
    apt_add_package_mock = MagicMock()
    _mock_install_host(monkeypatch, tmp_path, package_installed=True)
    monkeypatch.setattr(apt, "add_package", apt_add_package_mock)

    _begin_with_lazy_service(harness, run_install=True)
    harness.charm.on.install.emit()

    assert apt_add_package_mock.call_count == 0


def test_install_skips_reload_when_unit_unchanged(
    harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """
    arrange: Harness where the unit file on disk already matches the template.
    act: run the install hook twice to trigger reconcile.
    assert: the second run does not reload or re-enable the unchanged unit file.
    """
    host = _mock_install_host(monkeypatch, tmp_path, package_installed=True)
    _begin_with_lazy_service(harness, run_install=True)

    harness.charm.on.install.emit()
    first_enable_count = host.service_enable.call_count
    harness.charm.on.install.emit()

    assert first_enable_count == 1
    # Second reconcile sees identical files on disk, so no reload/enable happens.
    assert host.service_enable.call_count == 1


def test_install_enable_error(
    harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """
    arrange: Harness where enabling the systemd unit fails.
    act: run the install hook to trigger reconcile.
    assert: reconcile raises RuntimeError surfacing the enable failure.
    """
    host = _mock_install_host(monkeypatch, tmp_path)
    host.service_enable.side_effect = systemd.SystemdError("boom")
    _begin_with_lazy_service(harness)

    with pytest.raises(RuntimeError, match=r"Error installing the agent service"):
        harness.charm.on.install.emit()


def test_install_renders_user_and_workdir(
    harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """
    arrange: Harness with patched install paths and templates.
    act: run the install hook.
    assert: the systemd unit contains User, Group and WorkingDirectory derived from
        charm config defaults.
    """
    host = _mock_install_host(monkeypatch, tmp_path)
    apt_add_package_mock = MagicMock()
    monkeypatch.setattr(apt, "add_package", apt_add_package_mock)

    harness.begin_with_initial_hooks()
    unit_text = host.unit_path.read_text()

    assert "User=jenkins" in unit_text
    assert "Group=" not in unit_text
    assert "WorkingDirectory=/var/lib/jenkins" in unit_text
    assert 'Environment="JENKINS_HOME=/var/lib/jenkins"' in unit_text
    assert 'Environment="HOME=/var/lib/jenkins"' in unit_text
    assert 'Environment="USER=jenkins"' in unit_text


def test_install_renders_custom_user_and_workdir(
    harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """
    arrange: Harness configured with agent_user=jenkins and jenkins_home=/srv/jenkins.
    act: run the install hook.
    assert: the systemd unit uses the configured user, group and working directory.
    """
    host = _mock_install_host(monkeypatch, tmp_path)
    apt_add_package_mock = MagicMock()
    monkeypatch.setattr(apt, "add_package", apt_add_package_mock)

    harness.update_config({"agent_user": "jenkins", "jenkins_home": "/srv/jenkins"})
    harness.begin_with_initial_hooks()
    unit_text = host.unit_path.read_text()

    assert "User=jenkins" in unit_text
    assert "Group=" not in unit_text
    assert "WorkingDirectory=/srv/jenkins" in unit_text
    assert 'Environment="JENKINS_HOME=/srv/jenkins"' in unit_text
    assert "ExecStopPost=rm -rf /srv/jenkins/.ready" in unit_text


def test_install_renders_script_with_jenkins_home(
    harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """
    arrange: Harness with patched install paths and templates.
    act: run the install hook.
    assert: the launcher script references the configured JENKINS_HOME.
    """
    host = _mock_install_host(monkeypatch, tmp_path)
    apt_add_package_mock = MagicMock()
    monkeypatch.setattr(apt, "add_package", apt_add_package_mock)

    harness.update_config({"jenkins_home": "/srv/jenkins"})
    harness.begin_with_initial_hooks()
    script_text = Path(host.script_path).read_text()

    assert 'JENKINS_HOME="${JENKINS_HOME:-/srv/jenkins}"' in script_text


def test_install_renders_script_with_default_jenkins_home(
    harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """
    arrange: Harness without any jenkins_home override.
    act: run the install hook.
    assert: the launcher script references the default JENKINS_HOME.
    """
    host = _mock_install_host(monkeypatch, tmp_path)
    apt_add_package_mock = MagicMock()
    monkeypatch.setattr(apt, "add_package", apt_add_package_mock)

    harness.begin_with_initial_hooks()
    script_text = Path(host.script_path).read_text()

    assert 'JENKINS_HOME="${JENKINS_HOME:-/var/lib/jenkins}"' in script_text


def test_install_creates_user_and_home(
    harness: ops.testing.Harness,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    """
    arrange: Harness with agent_user=jenkins and jenkins_home pointing inside tmp_path.
    act: run the install hook.
    assert: the home directory is created and the charm calls os.chown using the
        created jenkins uid/gid.
    """
    home = tmp_path / "jenkins-home"
    host = _mock_install_host(monkeypatch, tmp_path)
    monkeypatch.setattr(apt, "add_package", MagicMock())
    _make_fake_useradd(monkeypatch)

    harness.update_config({"agent_user": "jenkins", "jenkins_home": str(home)})
    harness.begin_with_initial_hooks()

    assert home.exists()
    jenkins_uid = pwd.getpwnam("jenkins").pw_uid
    jenkins_gid = pwd.getpwnam("jenkins").pw_gid
    assert any(
        entry.args[1:] == (jenkins_uid, jenkins_gid) for entry in host.fchown.call_args_list
    )


def test_install_fails_on_useradd_failure(
    harness: ops.testing.Harness,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    """
    arrange: Harness with agent_user=nonexistent-testuser and useradd mocked to fail.
    act: run the install hook.
    assert: the charm reports an installation error.
    """
    username = "nonexistent-testuser"
    home = tmp_path / f"{username}-home"
    _mock_install_host(monkeypatch, tmp_path)
    monkeypatch.setattr(apt, "add_package", MagicMock())
    monkeypatch.setattr(
        subprocess, "run", MagicMock(side_effect=subprocess.CalledProcessError(1, ["useradd"]))
    )

    harness.update_config({"agent_user": username, "jenkins_home": str(home)})
    with pytest.raises(RuntimeError, match=r"Error installing the agent service"):
        harness.begin_with_initial_hooks()


def _make_fake_useradd(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provide an in-memory useradd that creates entries seen by pwd.getpwnam."""
    user_db: dict[str, pwd.struct_passwd] = {}

    def fake_run(args, **_kwargs):
        if args and args[0].endswith("visudo"):
            return subprocess.CompletedProcess(args, 0, "", "")
        # Validate expected useradd shape:
        # /usr/sbin/useradd --home-dir HOME --create-home --shell /bin/bash USER
        if len(args) < 7 or not args[0].endswith("useradd"):
            raise subprocess.CalledProcessError(1, args)
        if "--system" in args:
            raise subprocess.CalledProcessError(1, args)
        if "--create-home" not in args or "--shell" not in args or "/bin/bash" not in args:
            raise subprocess.CalledProcessError(1, args)
        username = args[-1]
        try:
            home = args[args.index("--home-dir") + 1]
        except (ValueError, IndexError) as exc:
            raise subprocess.CalledProcessError(1, args) from exc
        if username in user_db:
            raise subprocess.CalledProcessError(9, args)
        # Pick deterministic fake uid/gid based on username hash to avoid collisions.
        uid = 50000 + hash(username) % 10000
        gid = uid
        user_db[username] = pwd.struct_passwd((username, "x", uid, gid, "", home, "/bin/bash"))
        return subprocess.CompletedProcess(args, 0, "", "")

    real_getpwnam = pwd.getpwnam

    def fake_getpwnam(username: str):
        if username in user_db:
            return user_db[username]
        return real_getpwnam(username)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(pwd, "getpwnam", fake_getpwnam)


def test_render_file_uses_configured_owner(
    harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """
    arrange: Harness with agent_user=jenkins and patched install paths.
    act: run the install hook.
    assert: the systemd unit and launcher script are chowned to root; the home dir
        is chowned to jenkins.
    """
    home = tmp_path / "jenkins-home"
    host = _mock_install_host(monkeypatch, tmp_path)
    monkeypatch.setattr(apt, "add_package", MagicMock())
    _make_fake_useradd(monkeypatch)
    chown_mock = MagicMock()
    monkeypatch.setattr(os, "chown", chown_mock)

    harness.update_config({"agent_user": "jenkins", "jenkins_home": str(home)})
    harness.begin_with_initial_hooks()

    jenkins_uid = pwd.getpwnam("jenkins").pw_uid
    jenkins_gid = pwd.getpwnam("jenkins").pw_gid
    assert call(host.unit_path, uid=0, gid=0) in chown_mock.call_args_list
    assert call(host.script_path, uid=0, gid=0) in chown_mock.call_args_list
    assert any(
        entry.args[1:] == (jenkins_uid, jenkins_gid) for entry in host.fchown.call_args_list
    )


def test_ensure_user_chowns_existing_home_contents(
    harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """
    arrange: Harness with agent_user=jenkins, jenkins_home pre-existing with a file.
    act: run the install hook.
    assert: the home directory and its existing contents are chowned to jenkins.
    """
    home = tmp_path / "jenkins-home"
    home.mkdir(parents=True)
    existing = home / "existing.txt"
    existing.write_text("old")
    host = _mock_install_host(monkeypatch, tmp_path)
    monkeypatch.setattr(apt, "add_package", MagicMock())
    _make_fake_useradd(monkeypatch)
    chown_mock = MagicMock()
    monkeypatch.setattr(os, "chown", chown_mock)

    harness.update_config({"agent_user": "jenkins", "jenkins_home": str(home)})
    harness.begin_with_initial_hooks()

    jenkins_uid = pwd.getpwnam("jenkins").pw_uid
    jenkins_gid = pwd.getpwnam("jenkins").pw_gid
    assert any(
        entry.args[0] == "existing.txt"
        and entry.args[1:] == (jenkins_uid, jenkins_gid)
        and entry.kwargs["follow_symlinks"] is False
        for entry in chown_mock.call_args_list
    )
    assert any(
        entry.args[1:] == (jenkins_uid, jenkins_gid) for entry in host.fchown.call_args_list
    )


def test_install_migrates_home_ownership_only_once(
    harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """
    arrange: a non-root agent home contains a legacy file from a root-running revision.
    act: run installation and a later reconcile.
    assert: ownership migration is persisted and is not repeated on the later reconcile.
    """
    home = tmp_path / "jenkins-home"
    home.mkdir(parents=True)
    existing = home / "agent.jar"
    existing.write_text("legacy")
    migration_state = tmp_path / "migration-state"
    _mock_install_host(monkeypatch, tmp_path)
    monkeypatch.setattr(service, "OWNERSHIP_MIGRATION_STATE_DIR", migration_state, raising=False)
    monkeypatch.setattr(
        service,
        "OWNERSHIP_MIGRATION_STATE_PATH",
        migration_state / "home-ownership",
        raising=False,
    )
    monkeypatch.setattr(apt, "add_package", MagicMock())
    chown_mock = MagicMock()
    monkeypatch.setattr(os, "chown", chown_mock)

    harness.update_config({"agent_user": "jenkins", "jenkins_home": str(home)})
    _begin_with_lazy_service(harness, run_install=True)
    harness.charm.on.update_status.emit()

    assert (migration_state / "home-ownership").exists()
    existing_calls = [entry for entry in chown_mock.call_args_list if entry.args[0] == "agent.jar"]
    assert len(existing_calls) == 1


def test_chown_home_tree_does_not_follow_symlinks_or_mounts(
    harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """Ownership migration stays on the home filesystem and never follows links."""
    _mock_install_host(monkeypatch, tmp_path)
    home = tmp_path / "home"
    regular = home / "regular"
    regular.mkdir(parents=True)
    (regular / "payload").write_text("data")
    mounted = home / "mounted"
    mounted.mkdir()
    (mounted / "payload").write_text("data")
    outside = tmp_path / "outside"
    outside.write_text("outside")
    (home / "outside-link").symlink_to(outside)

    real_stat = os.stat

    def fake_stat(path, *args, **kwargs):
        result = real_stat(path, *args, **kwargs)
        if path == "mounted" and kwargs.get("dir_fd") is not None:
            values = list(result)
            values[2] += 1
            return os.stat_result(values)
        return result

    chown_mock = MagicMock()
    monkeypatch.setattr(os, "stat", fake_stat)
    monkeypatch.setattr(os, "chown", chown_mock)

    service.JenkinsAgentService._chown_home_tree(home, uid=4242, gid=4242)

    assert any(entry.args[0] == "regular" for entry in chown_mock.call_args_list)
    assert sum(entry.args[0] == "payload" for entry in chown_mock.call_args_list) == 1
    assert not any(
        entry.args[0] in {"mounted", "outside-link"} for entry in chown_mock.call_args_list
    )
    assert all(entry.kwargs.get("follow_symlinks") is False for entry in chown_mock.call_args_list)


def test_chown_home_tree_skips_entries_with_matching_owner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """Ownership migration does not issue chown calls for already-correct entries."""
    home = tmp_path / "home"
    home.mkdir()
    (home / "payload").write_text("data")
    chown_mock = MagicMock()
    fchown_mock = MagicMock()
    monkeypatch.setattr(os, "chown", chown_mock)
    monkeypatch.setattr(os, "fchown", fchown_mock)

    service.JenkinsAgentService._chown_home_tree(home, uid=os.getuid(), gid=os.getgid())

    assert not chown_mock.called
    assert not fchown_mock.called


def test_migrate_home_ownership_stops_running_service(
    harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """Ownership migration stops a running agent before changing its home."""
    _mock_install_host(monkeypatch, tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    _begin_with_lazy_service(harness)
    instance = _service(harness)
    running = MagicMock(return_value=True)
    stop = MagicMock()
    monkeypatch.setattr(systemd, "service_running", running)
    monkeypatch.setattr(systemd, "service_stop", stop)

    instance._migrate_home_ownership(home, uid=os.getuid(), gid=os.getgid())

    stop.assert_called_once_with(service.AGENT_SERVICE_NAME)


def test_migrate_home_ownership_retries_after_failure(
    harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """A failed migration is not marked complete and is retried later."""
    _mock_install_host(monkeypatch, tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    _begin_with_lazy_service(harness)
    instance = _service(harness)
    real_migrate = instance._chown_home_tree
    failing_migrate = MagicMock(side_effect=OSError("transient failure"))
    monkeypatch.setattr(instance, "_chown_home_tree", failing_migrate)

    with pytest.raises(service.PackageInstallError, match="migrate Jenkins home ownership"):
        instance._migrate_home_ownership(home, uid=os.getuid(), gid=os.getgid())
    assert not service.OWNERSHIP_MIGRATION_STATE_PATH.exists()

    monkeypatch.setattr(instance, "_chown_home_tree", real_migrate)
    instance._migrate_home_ownership(home, uid=os.getuid(), gid=os.getgid())
    assert service.OWNERSHIP_MIGRATION_STATE_PATH.exists()


def test_migrate_home_ownership_skips_matching_marker(
    harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """A matching fingerprint avoids stopping or walking the home again."""
    _mock_install_host(monkeypatch, tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    _begin_with_lazy_service(harness)
    instance = _service(harness)
    fingerprint = instance._ownership_fingerprint(home, os.getuid(), os.getgid())
    service.OWNERSHIP_MIGRATION_STATE_PATH.parent.mkdir()
    service.OWNERSHIP_MIGRATION_STATE_PATH.write_text(fingerprint)
    monkeypatch.setattr(systemd, "service_running", MagicMock(side_effect=AssertionError))
    walk = MagicMock(side_effect=AssertionError)
    monkeypatch.setattr(instance, "_chown_home_tree", walk)

    instance._migrate_home_ownership(home, uid=os.getuid(), gid=os.getgid())

    assert not walk.called


def test_chown_home_tree_ignores_entries_removed_during_migration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """A concurrent removal does not make ownership migration fail."""
    home = tmp_path / "home"
    home.mkdir()
    removed_directory = home / "removed-directory"
    removed_directory.mkdir()
    removed_file = home / "removed-file"
    removed_file.write_text("data")
    fchown_mock = MagicMock()

    def fake_chown(path, *args, **kwargs):
        if path in {"removed-directory", "removed-file"}:
            raise FileNotFoundError(path)
        return None

    monkeypatch.setattr(os, "fchown", fchown_mock)
    monkeypatch.setattr(os, "chown", fake_chown)

    service.JenkinsAgentService._chown_home_tree(home, uid=4242, gid=4242)

    assert fchown_mock.called


def test_chown_home_tree_ignores_entries_missing_during_stat(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """Entries that disappear during the scan are skipped safely."""
    home = tmp_path / "home"
    home.mkdir()
    missing_directory = home / "missing-directory"
    missing_directory.mkdir()
    missing_file = home / "missing-file"
    missing_file.write_text("data")
    real_stat = os.stat
    fchown_mock = MagicMock()

    def fake_stat(path, *args, **kwargs):
        if path in {"missing-directory", "missing-file"} and kwargs.get("dir_fd") is not None:
            raise FileNotFoundError(path)
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", fake_stat)
    monkeypatch.setattr(os, "fchown", fchown_mock)
    monkeypatch.setattr(os, "chown", MagicMock())

    service.JenkinsAgentService._chown_home_tree(home, uid=4242, gid=4242)

    assert fchown_mock.called


def test_migrate_home_ownership_reports_stop_failure(
    harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """A running service that cannot be stopped blocks ownership migration."""
    _mock_install_host(monkeypatch, tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    _begin_with_lazy_service(harness)
    instance = _service(harness)
    monkeypatch.setattr(systemd, "service_running", MagicMock(return_value=True))
    monkeypatch.setattr(
        systemd, "service_stop", MagicMock(side_effect=systemd.SystemdError("stop failed"))
    )

    with pytest.raises(service.PackageInstallError, match="stop the agent"):
        instance._migrate_home_ownership(home, uid=os.getuid(), gid=os.getgid())


def test_migrate_home_ownership_rejects_state_inside_home(
    harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """The migration marker must not be placed inside the migrated home."""
    _mock_install_host(monkeypatch, tmp_path)
    _begin_with_lazy_service(harness)
    instance = _service(harness)
    home = service.OWNERSHIP_MIGRATION_STATE_DIR / "home"

    with pytest.raises(service.PackageInstallError, match="outside Jenkins home"):
        instance._migrate_home_ownership(home, uid=os.getuid(), gid=os.getgid())


def test_migrate_home_ownership_rejects_symlinked_state(
    harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """A symlink cannot redirect the migration marker."""
    _mock_install_host(monkeypatch, tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    target = tmp_path / "target"
    target.write_text("target")
    service.OWNERSHIP_MIGRATION_STATE_DIR.mkdir()
    service.OWNERSHIP_MIGRATION_STATE_PATH.symlink_to(target)
    _begin_with_lazy_service(harness)
    instance = _service(harness)

    with pytest.raises(service.PackageInstallError, match="inspect ownership migration state"):
        instance._migrate_home_ownership(home, uid=os.getuid(), gid=os.getgid())


def test_migrate_home_ownership_rejects_symlinked_state_directory(
    harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """A symlinked state directory cannot receive a migration marker."""
    _mock_install_host(monkeypatch, tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    target = tmp_path / "target"
    target.mkdir()
    service.OWNERSHIP_MIGRATION_STATE_DIR.symlink_to(target, target_is_directory=True)
    _begin_with_lazy_service(harness)
    instance = _service(harness)

    with pytest.raises(service.PackageInstallError, match="migrate Jenkins home ownership"):
        instance._migrate_home_ownership(home, uid=os.getuid(), gid=os.getgid())


def test_ensure_user_rejects_symlinked_home(
    harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """The configured home itself must never be a symlink."""
    target = tmp_path / "target"
    target.mkdir()
    home = tmp_path / "home"
    home.symlink_to(target, target_is_directory=True)
    _mock_install_host(monkeypatch, tmp_path)
    monkeypatch.setattr(apt, "add_package", MagicMock())
    _make_fake_useradd(monkeypatch)
    harness.update_config({"agent_user": "jenkins", "jenkins_home": str(home)})

    with pytest.raises(RuntimeError, match="Error installing the agent service"):
        harness.begin_with_initial_hooks()


def test_ensure_user_rejects_symlinked_home_parent(
    harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """A symlink in a configured home path cannot redirect ownership migration."""
    target = tmp_path / "target"
    target.mkdir()
    parent = tmp_path / "parent"
    parent.symlink_to(target, target_is_directory=True)
    home = parent / "home"
    _mock_install_host(monkeypatch, tmp_path)
    monkeypatch.setattr(apt, "add_package", MagicMock())
    _make_fake_useradd(monkeypatch)
    harness.update_config({"agent_user": "jenkins", "jenkins_home": str(home)})

    with pytest.raises(RuntimeError, match="Error installing the agent service"):
        harness.begin_with_initial_hooks()


def test_install_grants_passwordless_sudo(
    harness: ops.testing.Harness,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    """Write a validated passwordless sudo rule for the configured agent user."""
    home = tmp_path / "jenkins-home"
    sudoers_d = tmp_path / "sudoers.d"
    _mock_install_host(monkeypatch, tmp_path)
    monkeypatch.setattr(os, "chmod", MagicMock())
    monkeypatch.setattr(service, "SUDOERS_DROP_IN_DIR", sudoers_d)

    harness.update_config({"agent_user": "jenkins", "jenkins_home": str(home)})
    harness.begin_with_initial_hooks()

    drop_in_path = sudoers_d / "99-jenkins-agent"
    assert drop_in_path.read_text() == "jenkins ALL=(ALL:ALL) NOPASSWD: ALL\n"


def test_restart_service(
    harness: ops.testing.Harness,
    monkeypatch: pytest.MonkeyPatch,
    agent_relation_data: dict,
    service_configuration_template: str,
):
    """
    arrange: Harness with mocked systemd and fs-related methods.
    act: add relation with jenkins-k8s with mock relation data and restart the agent service.
    assert: Configuration file content should match the relation data and
    restart should not raise any error and the charm should be in active state.
    """
    pathlib_write_text_mock = MagicMock()
    monkeypatch.setattr(Path, "write_text", pathlib_write_text_mock)
    monkeypatch.setattr(Path, "mkdir", MagicMock)
    monkeypatch.setattr(os, "chmod", MagicMock)
    monkeypatch.setattr(os, "chown", MagicMock)
    monkeypatch.setattr(systemd, "daemon_reload", MagicMock)
    monkeypatch.setattr(systemd, "service_restart", MagicMock)
    monkeypatch.setattr(systemd, "service_running", MagicMock(return_value=True))
    monkeypatch.setattr(
        service.JenkinsAgentService, "configuration_changed", MagicMock(return_value=False)
    )
    monkeypatch.setattr(
        service.JenkinsAgentService, "_startup_check", MagicMock(return_value=True)
    )
    # The reconcile handler also runs install(); it is exercised separately, so
    # stub it here to keep this test focused on the restart/config-render path.
    monkeypatch.setattr(service.JenkinsAgentService, "install", MagicMock())

    harness.add_relation(AGENT_RELATION, "jenkins-k8s", unit_data=agent_relation_data)
    _begin_with_lazy_service(harness, run_install=True)
    charm: JenkinsAgentCharm = harness.charm
    charm.on.config_changed.emit()

    assert pathlib_write_text_mock.call_args[0][0] == service_configuration_template
    assert charm.unit.status.name == ops.ActiveStatus.name


def test_restart_template_preserves_systemd_values(harness: ops.testing.Harness):
    """Systemd templates must not HTML-escape credentials or URLs."""
    _begin_with_lazy_service(harness)
    template = _service(harness)._template_loader.get_template("jenkins_agent_env.conf.j2")

    rendered = template.render(
        environments={"JENKINS_TOKEN": 'a&b<c>"d', "JENKINS_URL": "https://jenkins.test"}
    )

    assert 'Environment="JENKINS_TOKEN=a&b<c>\\"d"' in rendered
    with pytest.raises(ValueError, match=r"control characters"):
        template.render(environments={"JENKINS_TOKEN": "bad\nvalue"})


def test_restart_service_write_config_type_error(
    harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch, agent_relation_data: dict
):
    """
    arrange: Harness with mocked fs-related methods raising an error.
    act: restart the agent service.
    assert: The charm should raise ServiceRestartError.
    """
    monkeypatch.setattr(Path, "write_text", MagicMock(side_effect=TypeError))
    monkeypatch.setattr(Path, "mkdir", MagicMock)
    harness.add_relation(AGENT_RELATION, "jenkins-k8s", unit_data=agent_relation_data)
    _begin_with_lazy_service(harness)

    with pytest.raises(
        service.ServiceRestartError,
        match=r"Error interacting with the filesystem when rendering configuration file",
    ):
        _service(harness).restart()


def test_restart_service_systemd_error(
    harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch, agent_relation_data: dict
):
    """
    arrange: Harness with mocked systemd methods raising an error.
    act: restart the agent service.
    assert: The charm should raise ServiceRestartError.
    """
    systemd_error_message = "Mock systemd error"
    monkeypatch.setattr(service.JenkinsAgentService, "_render_file", MagicMock)
    monkeypatch.setattr(Path, "mkdir", MagicMock)
    monkeypatch.setattr(
        systemd,
        "daemon_reload",
        MagicMock(side_effect=systemd.SystemdError(systemd_error_message)),
    )
    harness.add_relation(AGENT_RELATION, "jenkins-k8s", unit_data=agent_relation_data)
    _begin_with_lazy_service(harness)

    with pytest.raises(
        service.ServiceRestartError,
        match=rf"Error starting the agent service:\n{systemd_error_message}",
    ):
        _service(harness).restart()


def test_service_is_ready_systemd_error(
    harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch
):
    """
    arrange: Harness with mocked fs-related methods raising an error.
    act: Check if the service is running.
    assert: The call should return false and not raising any exceptions.
    """
    _begin_with_lazy_service(harness)
    # Mock Path.exists to return True for the configured default ready marker so
    # we exercise the systemd.service_running path.
    ready_path = Path("/var/lib/jenkins/.ready")
    real_exists = Path.exists

    def _mock_exists(self):
        if self == ready_path:
            return True
        return real_exists(self)

    monkeypatch.setattr(Path, "exists", _mock_exists)
    monkeypatch.setattr(systemd, "service_running", MagicMock(side_effect=SystemError))

    with pytest.raises(RuntimeError, match="Failed to query the agent service"):
        assert _service(harness).is_ready


def test_parse_systemd_env():
    """
    arrange: A systemd override.conf with Environment directives.
    act: Parse the content.
    assert: The correct key-value pairs are extracted.
    """
    content = (
        "[Service]\n"
        'Environment="JENKINS_TOKEN=abc123"\n'  # nosec: B105
        'Environment="JENKINS_URL=http://10.1.69.130:8080"\n'
        'Environment="JENKINS_AGENT=jenkins-agent-k8s-0"'
    )
    result = service._parse_systemd_env(content)
    assert result == {
        "JENKINS_TOKEN": "abc123",
        "JENKINS_URL": "http://10.1.69.130:8080",
        "JENKINS_AGENT": "jenkins-agent-k8s-0",
    }


def test_parse_systemd_env_unquotes_values():
    """Parse the escaping emitted by the systemd environment template."""
    content = '[Service]\nEnvironment="JENKINS_TOKEN=a%%\\\\b\\"c"'

    assert service._parse_systemd_env(content)["JENKINS_TOKEN"] == 'a%\\b"c'


@pytest.mark.parametrize(
    "override_content,cred_url,cred_secret,expected",
    [
        pytest.param(None, "http://new", "s", True, id="no_override_file"),
        pytest.param(
            "[Service]\n"
            'Environment="JENKINS_TOKEN=secret123"\n'
            'Environment="JENKINS_URL=http://10.1.69.130:8080"\n'
            'Environment="JENKINS_AGENT=test-model-jenkins-agent-0"',
            "http://10.1.69.130:8080",
            "secret123",
            False,
            id="same_credentials",
        ),
        pytest.param(
            "[Service]\n"
            'Environment="JENKINS_TOKEN=secret123"\n'
            'Environment="JENKINS_URL=http://10.1.69.130:8080"\n'
            'Environment="JENKINS_AGENT=test-model-jenkins-agent-0"',
            "http://10.1.69.153:8080",
            "secret123",
            True,
            id="url_differs",
        ),
        pytest.param(
            '[Service]\nEnvironment="JENKINS_TOKEN=a%%\\\\b\\"c"\n'
            'Environment="JENKINS_URL=http://10.1.69.130:8080"',
            "http://10.1.69.130:8080",
            'a%\\b"c',
            False,
            id="escaped_credentials",
        ),
    ],
)
def test_credentials_changed(
    harness: ops.testing.Harness,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    override_content: str | None,
    cred_url: str,
    cred_secret: str,
    expected: bool,
):
    """
    arrange: A service with or without an override.conf file.
    act: Check if credentials changed against given values.
    assert: Returns expected bool depending on file state and content.
    """
    config_dir = tmp_path / "override"
    if override_content is not None:
        config_dir.mkdir()
        (config_dir / "override.conf").write_text(override_content)
    monkeypatch.setattr(service, "SYSTEMD_SERVICE_CONF_DIR", str(config_dir))
    harness.add_relation(
        AGENT_RELATION,
        "jenkins-k8s",
        unit_data={"url": cred_url, "test-model-jenkins-agent-0_secret": cred_secret},
    )
    _begin_with_lazy_service(harness)
    result = _service(harness).credentials_changed(
        Credentials(address=cred_url, secret=cred_secret)
    )
    assert result is expected


def test_restart_missing_credentials(harness: ops.testing.Harness):
    """
    arrange: a service whose state has no agent relation credentials.
    act: call restart directly.
    assert: ServiceRestartError is raised for the missing configuration.
    """
    # The default harness has no agent relation, so credentials resolve to None.
    _begin_with_lazy_service(harness)

    with pytest.raises(service.ServiceRestartError, match=r"missing configuration"):
        _service(harness).restart()


def test_restart_startup_check_timeout(
    harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch, agent_relation_data: dict
):
    """
    arrange: a related service whose agent never becomes active before timeout.
    act: call restart directly.
    assert: ServiceRestartError is raised because the startup check times out.
    """
    monkeypatch.setattr(service.JenkinsAgentService, "_render_file", MagicMock())
    monkeypatch.setattr(Path, "mkdir", MagicMock())
    monkeypatch.setattr(systemd, "daemon_reload", MagicMock())
    monkeypatch.setattr(systemd, "service_restart", MagicMock())
    monkeypatch.setattr(service.JenkinsAgentService, "is_ready", PropertyMock(return_value=False))
    # Collapse the polling loop so the test does not actually sleep.
    monkeypatch.setattr(service, "STARTUP_CHECK_TIMEOUT", 0)
    monkeypatch.setattr(service.time, "sleep", MagicMock())
    harness.add_relation(AGENT_RELATION, "jenkins-k8s", unit_data=agent_relation_data)
    _begin_with_lazy_service(harness)

    with pytest.raises(
        service.ServiceRestartError, match=r"waiting for the agent service to start"
    ):
        _service(harness).restart()


def test_startup_check_becomes_active(
    harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch
):
    """
    arrange: a service that reports active on the first poll.
    act: call the startup check.
    assert: it returns True after breaking out of the poll loop.
    """
    monkeypatch.setattr(service.JenkinsAgentService, "is_ready", PropertyMock(return_value=True))
    monkeypatch.setattr(service.time, "sleep", MagicMock())
    _begin_with_lazy_service(harness)

    assert _service(harness)._startup_check() is True


def test_reset_failed_state_logs_on_error(
    harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch
):
    """
    arrange: a service whose systemctl reset-failed raises a SystemdError.
    act: call reset_failed_state.
    assert: the error is swallowed (logged, not raised) as it is non-critical.
    """
    monkeypatch.setattr(systemd, "_systemctl", MagicMock(side_effect=systemd.SystemdError))
    _begin_with_lazy_service(harness)

    # Should not raise.
    _service(harness).reset_failed_state()


def test_reset_stops_and_clears_config(
    harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """
    arrange: a related service with an existing override.conf file.
    act: call reset.
    assert: the service is stopped and the override.conf file is removed.
    """
    config_dir = tmp_path / "override"
    config_dir.mkdir()
    override = config_dir / "override.conf"
    override.write_text("[Service]")
    monkeypatch.setattr(service, "SYSTEMD_SERVICE_CONF_DIR", str(config_dir))
    stop_mock = MagicMock()
    monkeypatch.setattr(systemd, "service_stop", stop_mock)
    _begin_with_lazy_service(harness)

    _service(harness).reset()

    assert stop_mock.call_count == 1
    assert not override.exists()


def test_reset_stop_error(harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch):
    """
    arrange: a service whose systemctl stop raises a SystemdError.
    act: call reset.
    assert: ServiceStopError is raised.
    """
    monkeypatch.setattr(systemd, "service_stop", MagicMock(side_effect=systemd.SystemdError))
    _begin_with_lazy_service(harness)

    with pytest.raises(service.ServiceStopError, match=r"failed to stop"):
        _service(harness).reset()


def test_sync_service_files_read_error(
    harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """
    arrange: a service whose existing on-disk unit file cannot be read.
    act: run install to trigger the file sync.
    assert: FileRenderError is raised when the destination file read fails.
    """
    host = _mock_install_host(monkeypatch, tmp_path)
    unit_path = host.unit_path
    unit_path.write_text("stale")

    real_read_text = Path.read_text

    def _read_text(self, *args, **kwargs):
        # Fail only when reading the existing on-disk unit file; template reads
        # (relative paths under templates/) must still succeed.
        if self == unit_path:
            raise OSError("disk gone")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _read_text)
    _begin_with_lazy_service(harness)

    with pytest.raises(service.FileRenderError, match=r"Error reading"):
        _service(harness).install()


def test_install_reports_service_file_changes(
    harness: ops.testing.Harness,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    """Return whether reconciliation changed files that require a restart."""
    _mock_install_host(monkeypatch, tmp_path, package_installed=True)
    _begin_with_lazy_service(harness)
    sync_mock = MagicMock(side_effect=[True, False])
    monkeypatch.setattr(_service(harness), "_sync_service_files", sync_mock)

    assert _service(harness).install() is True
    assert _service(harness).install() is False


def test_is_running_queries_systemd(harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch):
    """is_running reports the systemd service state independently of readiness."""
    _begin_with_lazy_service(harness)
    monkeypatch.setattr(systemd, "service_running", MagicMock(return_value=True))
    assert _service(harness).is_running


def test_is_running_systemd_error_raises(
    harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch
):
    """A systemd query failure is retryable rather than silently inactive."""
    _begin_with_lazy_service(harness)
    monkeypatch.setattr(systemd, "service_running", MagicMock(side_effect=SystemError))
    with pytest.raises(RuntimeError, match="Failed to query the agent service"):
        _ = _service(harness).is_running


def test_configuration_changed_compares_desired_override(
    harness: ops.testing.Harness,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    agent_relation_data: dict,
):
    """Compare desired credentials/agent/home with the actual override file."""
    harness.add_relation(AGENT_RELATION, "jenkins-k8s", unit_data=agent_relation_data)
    _begin_with_lazy_service(harness)
    service_instance = _service(harness)
    override_dir = tmp_path / "override"
    override_dir.mkdir()
    monkeypatch.setattr(service, "SYSTEMD_SERVICE_CONF_DIR", str(override_dir))
    monkeypatch.setattr(service_instance, "service_files_changed", MagicMock(return_value=False))
    (override_dir / "override.conf").write_text(
        "[Service]\n"
        f'Environment="JENKINS_TOKEN={agent_relation_data["test-model-jenkins-agent-0_secret"]}"\n'
        f'Environment="JENKINS_URL={agent_relation_data["url"]}"\n'
        'Environment="JENKINS_AGENT=test-model-jenkins-agent-0"\n'
        'Environment="JENKINS_HOME=/var/lib/jenkins"'
    )
    credentials = Credentials(
        address=agent_relation_data["url"],
        secret=agent_relation_data["test-model-jenkins-agent-0_secret"],
    )

    assert not service_instance.configuration_changed(credentials)
    assert not service_instance.configuration_changed()
    (override_dir / "override.conf").unlink()
    assert service_instance.configuration_changed(credentials)
    (override_dir / "override.conf").write_text(
        "[Service]\n"
        f'Environment="JENKINS_TOKEN={agent_relation_data["test-model-jenkins-agent-0_secret"]}"\n'
        f'Environment="JENKINS_URL={agent_relation_data["url"]}"\n'
        'Environment="JENKINS_AGENT=test-model-jenkins-agent-0"\n'
        'Environment="JENKINS_HOME=/srv/jenkins"'
    )
    assert service_instance.configuration_changed(credentials)


def test_service_files_changed_compares_rendered_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """Detect drift in rendered service files before writing them."""
    state = cast(
        State,
        SimpleNamespace(
            agent_user="root", jenkins_home=Path("/var/lib/jenkins"), websocket_mode=True
        ),
    )
    service_instance = service.JenkinsAgentService(state)
    unit_path = tmp_path / "jenkins-agent.service"
    script_path = tmp_path / "jenkins-agent"
    monkeypatch.setattr(service, "JENKINS_AGENT_SYSTEMD_PATH", unit_path)
    monkeypatch.setattr(service, "JENKINS_AGENT_START_SCRIPT_PATH", script_path)
    monkeypatch.setattr(
        service_instance, "_render_service_files", MagicMock(return_value=("unit", "script"))
    )
    unit_path.write_text("unit")
    script_path.write_text("script")

    assert not service_instance.service_files_changed()
    script_path.write_text("changed")
    assert service_instance.service_files_changed()


def test_service_files_changed_read_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Surface an error reading an existing service file."""
    state = cast(
        State,
        SimpleNamespace(
            agent_user="root", jenkins_home=Path("/var/lib/jenkins"), websocket_mode=True
        ),
    )
    service_instance = service.JenkinsAgentService(state)
    unit_path = tmp_path / "jenkins-agent.service"
    script_path = tmp_path / "jenkins-agent"
    monkeypatch.setattr(service, "JENKINS_AGENT_SYSTEMD_PATH", unit_path)
    monkeypatch.setattr(service, "JENKINS_AGENT_START_SCRIPT_PATH", script_path)
    unit_path.write_text("unit")
    monkeypatch.setattr(
        service_instance, "_render_service_files", MagicMock(return_value=("unit", "script"))
    )
    monkeypatch.setattr(Path, "read_text", MagicMock(side_effect=OSError("read failed")))

    with pytest.raises(service.FileRenderError, match="read failed"):
        service_instance.service_files_changed()
