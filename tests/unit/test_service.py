# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.
"""Test for service interaction."""
# pylint: disable=protected-access
from unittest.mock import MagicMock

import ops.testing
import pytest
from charms.operator_libs_linux.v0 import apt

from charm import JenkinsAgentCharm


@pytest.mark.parametrize(
    "f,error_thrown",
    [
        ("import_key", apt.GPGKeyError),
        ("add_package", apt.PackageError),
        ("add_package", apt.PackageNotFoundError),
    ],
)
def test_install_apt_package_gpg_key_error(
    harness: ops.testing.Harness, monkeypatch: pytest.MonkeyPatch, f, error_thrown
):
    """
    arrange: Harness with mocked apt module.
    act: run _on_install hook with methods raising different errors.
    assert: The charm should be in an error state.
    """
    harness.begin()
    charm: JenkinsAgentCharm = harness.charm
    monkeypatch.setattr(apt, "RepositoryMapping", MagicMock())
    monkeypatch.setattr(apt, "import_key", MagicMock())
    monkeypatch.setattr(apt, "update", MagicMock())
    monkeypatch.setattr(apt, "add_package", MagicMock())

    monkeypatch.setattr(apt, f, MagicMock(side_effect=[error_thrown]))

    with pytest.raises(RuntimeError, match="Error installing the agent service"):
        charm._on_install(MagicMock(spec=ops.InstallEvent))
