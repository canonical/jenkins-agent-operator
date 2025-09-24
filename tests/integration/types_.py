# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Types for Jenkins-agent-k8s-operator charm integration tests."""

from dataclasses import dataclass
from enum import StrEnum

import jenkinsapi


class DeployPlatform(StrEnum):
    DOCKER = "DOCKER"
    JUJU = "juju"


class JenkinsClient(jenkinsapi.jenkins.Jenkins):
    """Jenkins client wrapper."""

    def __init__(
        self,
        baseurl: str,
        username: str = "",
        password: str = "",
        requester=None,
        lazy: bool = False,
        ssl_verify: bool = True,
        cert=None,
        timeout: int = 10,
        use_crumb: bool = True,
        max_retries=None,
        deploy_platform: DeployPlatform = DeployPlatform.JUJU,
    ) -> None:
        super().__init__(
            baseurl,
            username,
            password,
            requester,
            lazy,
            ssl_verify,
            cert,
            timeout,
            use_crumb,
            max_retries,
        )
        self.deploy_platform = deploy_platform
