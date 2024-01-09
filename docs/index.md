# Jenkins-k8s Operator

A [Juju](https://juju.is/) [charm](https://juju.is/docs/olm/charmed-operators) deploying and managing [Jenkins](https://www.jenkins.io/) Agents on machines. It is configurable to integrate with a Jenkins charm deployed in another Juju model.

This charm simplifies initial deployment and "day N" operations of Jenkins Agent on VMs and bare metal.

As such, the charm makes it easy for those looking to take control of their own agents whilst keeping operations simple, and gives them the freedom to deploy on the platform of their choice.

For DevOps or SRE teams this charm will make operating Jenkins Agent simple and straightforward through Juju's clean interface. It will allow easy deployment into multiple environments for testing changes, and supports scaling out for enterprise deployments.

## Project and community

The Jenkins-agent Operator is a member of the Ubuntu family. It's an open source project that warmly welcomes community projects, contributions, suggestions, fixes and constructive feedback.

- [Code of conduct](https://ubuntu.com/community/code-of-conduct)
- [Get support](https://discourse.charmhub.io/)
- [Join our online chat](https://app.element.io/#/room/#charmhub-charmdev:ubuntu.com)
- [Contribute](Contribute)

Thinking about using the Jenkins-k8s Operator for your next project? [Get in touch](https://app.element.io/#/room/#charmhub-charmdev:ubuntu.com)!

# Contents

1. [Tutorial](tutorial)
   1. [Getting Started](tutorial/getting-started.md)
1. [How to](how-to)
   1. [Configure agent node label](how-to/configure-agent-node-label.md)
1. [Reference](reference)
   1. [Actions](reference/actions.md)
   1. [Configurations](reference/configurations.md)
   1. [Integrations](reference/integrations.md)
1. [Explanation](explanation)
