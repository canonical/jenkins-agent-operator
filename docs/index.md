A [Juju](https://juju.is/) [charm](https://documentation.ubuntu.com/juju/3.6/reference/charm/) deploying and managing [Jenkins](https://www.jenkins.io/) agents on machines. It is configurable to integrate with a Jenkins charm deployed in another Juju model.

This charm simplifies initial deployment and operations of Jenkins Agent on VMs and bare metal. For DevOps or SRE teams this charm will make operating Jenkins agents simple and straightforward through Juju's clean interface. It will allow easy deployment into multiple environments for testing changes, and supports scaling out for enterprise deployments.

## In this documentation

| | |
|--|--|
|  [Tutorials](https://charmhub.io/jenkins-agent/docs/tutorial-getting-started)</br>  Get started - a hands-on introduction to using the charm for new users </br> |  [How-to guides](https://charmhub.io/jenkins-agent/docs/how-to-configure-agent-node-label) </br> Step-by-step guides covering key operations and common tasks |
| [Reference](https://charmhub.io/jenkins-agent/docs/reference-actions) </br> Technical information - specifications, APIs, architecture | [Explanation](https://charmhub.io/jenkins-agent/docs/explanation-workload) </br> Concepts - discussion and clarification of key topics  |

## Contributing to this documentation

Documentation is an important part of this project, and we take the same open-source approach to the documentation as 
the code. As such, we welcome community contributions, suggestions and constructive feedback on our documentation. 
Our documentation is hosted on the [Charmhub forum](https://discourse.charmhub.io/) 
to enable easy collaboration. Please use the "Help us improve this documentation" links on each documentation page to 
either directly change something you see that's wrong, ask a question or make a suggestion about a potential change via 
the comments section.

If there's a particular area of documentation that you'd like to see that's missing, please 
[file a bug](https://github.com/canonical/jenkins-agent-operator/issues).

## Project and community

The jenkins-agent Operator is a member of the Ubuntu family. It's an open source project that warmly welcomes community projects, contributions, suggestions, fixes and constructive feedback.

- [Code of conduct](https://ubuntu.com/community/code-of-conduct)
- [Get support](https://discourse.charmhub.io/)
- [Join our online chat](https://app.element.io/#/room/#charmhub-charmdev:ubuntu.com)
- [Contribute](https://github.com/canonical/jenkins-agent-operator/blob/3e451213530aba783b892d231ce0f783a22ec303/CONTRIBUTING.md)

Thinking about using the Jenkins-agent Operator for your next project? [Get in touch](https://app.element.io/#/room/#charmhub-charmdev:ubuntu.com)!

# Contents

1. [Tutorial](tutorial)
  1. [Getting Started](tutorial/getting-started.md)
1. [How to](how-to)
  1. [Configure agent node label](how-to/configure-agent-node-label.md)
  1. [Upgrade](how-to/upgrade.md)
1. [Reference](reference)
  1. [Actions](reference/actions.md)
  1. [Charm architecture](reference/charm-architecture.md)
  1. [Configurations](reference/configurations.md)
  1. [Integrations](reference/integrations.md)
1. [Explanation](explanation)
  1. [Managing workload inside the charm](explanation/workload.md)
