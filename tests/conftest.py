# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.

"""Fixtures for jenkins-agent charm tests."""

import pytest


def pytest_addoption(parser: pytest.Parser):
    """Parse additional pytest options.

    Args:
        parser: pytest command line parser.
    """
    # The Jenkins agent image name:tag.
    parser.addoption("--jenkins-agent-image", action="store", default="")
    # The prebuilt charm file.
    parser.addoption("--charm-file", action="store", default="")
