# Integrations

### `agent`

_Interface_: jenkins_agent_v0    
_Supported charms_: [jenkins-k8s](https://charmhub.io/jenkins-agent-k8s)

Jenkins agents provide a way to perform tasks scheduled by the Jenkins server. Jenkins agents are
used to distribute workload across multiple containers, allowing parallel execution of jobs.

The agent publishes its configured `jenkins_home` as the relation's `remote_fs` metadata. The
Jenkins controller consumes this value as the node's remote root directory, ensuring that workspaces
are created under a path owned by the configured agent user. Existing consumers that do not publish
`remote_fs` continue to use `/var/lib/jenkins`.

To create a [cross model integration](https://documentation.ubuntu.com/juju/3.6/howto/manage-relations/#add-a-cross-model-relation) with
a jenkins-agent (VM) charm, create an offer from the machine model.

```
juju offer jenkins-agent:agent
```

Then, integrate the offer from the Kubernetes model where jenkins-k8s charm resides.

```
juju integrate jenkins-k8s:agent <controller-name>:<juju-user>/<agent-model>.jenkins-agent
```

An example of such command would look like the following for the jenkins-k8s charm deployed on MicroK8s.

```
juju integrate jenkins-k8s:agent localhost:admin/jenkins-agent-model.jenkins-agent
```