# How to configure installable plugins

### Configure `jenkins_agent_labels`

Use the `jenkins_agent_labels` configuration to allow assigning different labels to the agent's node on the jenkins server.
Comma-separated list of node labels. If empty, the agent's node will have the underlying machine's arch as a label by default, most of the time this will be `x86_64`. If this value is configured before any integrations with the jenkins charm is established, the label will be applied during the node's creation once an integration has been established.

```
juju config jenkins-agent jenkins_agent_labels=label1,label2,label3
```
