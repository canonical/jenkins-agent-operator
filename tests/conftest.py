# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.

"""Fixtures for Pollen charm tests."""

from pytest import Parser


def pytest_addoption(parser: Parser):
    """Parse additional pytest options.

    Args:
        parser: Pytest parser.
    """
    parser.addoption("--charm-file", action="store")
