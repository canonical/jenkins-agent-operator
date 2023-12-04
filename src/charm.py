#!/usr/bin/env python3

# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.

"""Charm jenkins agent."""

import logging
import typing

import ops
from ops.main import main

import agent_observer
import service
import server
from state import AGENT_RELATION, InvalidStateError, State

logger = logging.getLogger()


class JenkinsAgentCharm(ops.CharmBase):
    """Charm Jenkins agent."""

    def __init__(self, *args: typing.Any):
        """Initialize the charm and register event handlers.

        Args:
            args: Arguments to initialize the charm base.
        """
        super().__init__(*args)
        try:
            self.state = State.from_charm(self)
        except InvalidStateError as exc:
            self.unit.status = ops.BlockedStatus(exc.msg)
            return

        self.agent_observer = agent_observer.Observer(self, self.state, self.pebble_service)

        self.framework.observe(self.on.config_changed, self._on_config_changed)
        self.framework.observe(self.on.upgrade_charm, self._on_upgrade_charm)

    def _on_config_changed(self, event: ops.ConfigChangedEvent) -> None:
        """Handle config changed event.

        Args:
            event: The event fired on configuration change.
        """
        # TODO: implement lifecycle management with snap
        self._reconcile(event)

    def _on_upgrade_charm(self, event: ops.UpgradeCharmEvent) -> None:
        """Handle upgrade charm event.

        Args:
            event: The event fired on upgrade charm.
        """
        # TODO: implement lifecycle management with snap
        self._reconcile(event)


if __name__ == "__main__":  # pragma: no cover
    main(JenkinsAgentCharm)
