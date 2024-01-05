# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

"""The module for managing charm state."""


import logging
import os
import typing
from dataclasses import dataclass

import ops
from dotenv import dotenv_values
from pydantic import BaseModel, Field, ValidationError, tools
from typing_extensions import Literal

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


class AgentMeta(BaseModel):
    """The Jenkins agent metadata.

    Attrs:
        executors: The number of executors available on the unit.
        labels: The comma separated labels to assign to the agent.
        name: The name of the agent.
    """

    labels: str
    name: str
    executors: int = Field(..., ge=1)

    def as_dict(self) -> typing.Dict[str, str]:
        """Return dictionary representation of agent metadata.

        Returns:
            A dictionary adhering to jenkins_agent_v0 interface.
        """
        return {
            "executors": str(self.executors),
            "labels": self.labels,
            "name": self.name,
        }


class UnitData(BaseModel):
    """The charm's unit data.

    Attrs:
        series: The base of the machine on which the charm is running.
    """

    series: Literal["focal", "jammy"]


class InvalidStateError(Exception):
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
    jenkins_unit = [unit for unit in all_units if unit.app.name != current_app_name]
    return jenkins_unit[0] if jenkins_unit else None


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
    """The Jenkins agent state.

    Attrs:
        agent_meta: The Jenkins agent metadata to register on Jenkins server.
        agent_relation_credentials: The full set of credentials from the agent relation. None if
            partial data is set or the credentials do not belong to current agent.
        unit_data: Data about the current unit.
        jenkins_agent_service_name: The Jenkins agent workload container name.
    """

    agent_meta: AgentMeta
    agent_relation_credentials: typing.Optional[Credentials]
    unit_data: UnitData
    jenkins_agent_service_name: str = "jenkins-agent"

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
            agent_meta = AgentMeta(
                executors=tools.parse_obj_as(int, os.cpu_count()),
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

        # Load series information
        os_release: dict = dotenv_values("/etc/os-release")
        unit_series = os_release.get("UBUNTU_CODENAME")
        try:
            unit_data = UnitData(series=unit_series)
        except ValidationError as exc:
            logging.error("Unsupported series, %s: %s", unit_series, exc)
            raise InvalidStateError("Unsupported series.") from exc

        return cls(
            agent_meta=agent_meta,
            agent_relation_credentials=agent_relation_credentials,
            unit_data=unit_data,
        )
