# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.

"""The module for managing charm state."""


import logging
import os
import typing
from dataclasses import dataclass

import ops
from pydantic import BaseModel, ValidationError, HttpUrl

import metadata

# agent relation name
AGENT_RELATION = "agent"

logger = logging.getLogger()


class Credentials(BaseModel):
    """The credentials used to register to the Jenkins server.

    Attrs:
        address: The Jenkins server address to register to.
        secret: The secret used to register agent.
    """

    address: str
    secret: str


class ProxyConfig(BaseModel):
    """Configuration for external access through proxy.

    Attributes:
        http_proxy: The http proxy URL.
        https_proxy: The https proxy URL.
        no_proxy: Comma separated list of hostnames to bypass proxy.
    """

    http_proxy: typing.Optional[HttpUrl]
    https_proxy: typing.Optional[HttpUrl]

    @classmethod
    def from_env(cls) -> typing.Optional["ProxyConfig"]:
        """Instantiate ProxyConfig from juju charm environment.

        Returns:
            ProxyConfig if proxy configuration is provided, None otherwise.
        """
        http_proxy = os.environ.get("JUJU_CHARM_HTTP_PROXY")
        https_proxy = os.environ.get("JUJU_CHARM_HTTPS_PROXY")

        if not http_proxy and not https_proxy:
            return None

        return cls(
            http_proxy=http_proxy if http_proxy else None,
            https_proxy=https_proxy if https_proxy else None,
        )


class CharmStateBaseError(Exception):
    """Represents error with charm state."""


class InvalidStateError(CharmStateBaseError):
    """Exception raised when state configuration is invalid."""

    def __init__(self, msg: str = ""):
        """Initialize a new instance of the InvalidStateError exception.

        Args:
            msg: Explanation of the error.
        """
        self.msg = msg


def _get_jenkins_unit(
    all_units: typing.Set[ops.Unit], current_app_name: str
) -> typing.Optional[ops.Unit]:
    """Get the Jenkins charm unit in a relation.

    Args:
        all_units: All units in a relation.
        current_app_name: The Jenkins-agent application name.

    Returns:
        The Jenkins server application unit in the relation if found. None otherwise.
    """
    for unit in all_units:
        # if the unit's application name is the same, this is peer unit. Otherwise, it is the
        # Jenkins server unit.
        if unit.app.name == current_app_name:
            continue
        return unit
    return None


def _get_credentials_from_agent_relation(
    server_unit_databag: ops.RelationDataContent, unit_name: str
) -> typing.Optional[Credentials]:
    """Import server metadata from databag in agent relation.

    Args:
        server_unit_databag: The relation databag content from agent relation.
        unit_name: The agent unit name.

    Returns:
        Metadata if complete values(url, secret) are set. None otherwise.
    """
    address = server_unit_databag.get("url")
    secret = server_unit_databag.get(f"{unit_name}_secret")
    if not address or not secret:
        return None
    return Credentials(address=address, secret=secret)


@dataclass
class State:
    """The jenkins agent state.

    Attributes:
        agent_meta: metadata of the agent visible to the jenkins server
        agent_relation_credentials: information required to connect to the jenkins server
        proxy_config: proxy configuration to access the snap store
    """

    agent_meta: metadata.Agent
    agent_relation_credentials: typing.Optional[Credentials]
    proxy_config: typing.Optional[ProxyConfig]

    @classmethod
    def from_charm(cls, charm: ops.CharmBase) -> "State":
        """Initialize the state from charm.

        Args:
            charm: The root Jenkins agent charm.

        Raises:
            InvalidStateError: if invalid state values were encountered.

        Returns:
            Current state of Jenkins agent.
        """
        try:
            agent_meta = metadata.Agent(
                num_executors=os.cpu_count() or 0,
                labels=charm.model.config.get("jenkins_agent_labels", "") or os.uname().machine,
                name=charm.unit.name.replace("/", "-"),
            )
        except ValidationError as exc:
            logging.error("Invalid executor state, %s", exc)
            raise InvalidStateError("Invalid executor state.") from exc

        agent_relation = charm.model.get_relation(AGENT_RELATION)
        agent_relation_credentials: typing.Optional[Credentials] = None
        if agent_relation and (
            agent_relation_jenkins_unit := _get_jenkins_unit(agent_relation.units, charm.app.name)
        ):
            agent_relation_credentials = _get_credentials_from_agent_relation(
                agent_relation.data[agent_relation_jenkins_unit], agent_meta.name
            )

        try:
            proxy_config = ProxyConfig.from_env()
        except ValidationError as exc:
            logger.error("Invalid juju model proxy configuration, %s", exc)
            raise InvalidStateError("Invalid model proxy configuration.") from exc

        return cls(
            agent_meta=agent_meta,
            agent_relation_credentials=agent_relation_credentials,
            proxy_config=proxy_config,
        )
