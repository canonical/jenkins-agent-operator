# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Fixtures for jenkins-agent charm tests."""

from pytest import Parser


def pytest_addoption(parser: Parser):
    """Parse additional pytest options.

    Args:
        parser: Pytest parser.
    """
    parser.addoption(
        "--charm-file",
        action="store",
        help="Path to Jenkins agent operator charm file.",
    )
    parser.addoption(
        "--any-charm-file",
        action="store",
        help="Path to Any-charm charm file. Used to test non-amd64 architectures.",
    )
    parser.addoption(
        "--use-docker",
        action="store_true",
        default=False,
        help="Enable testing with Jenkins server hosted on Docker. "
        "Used for testing non-amd64 architectures",
    )
