# Integrations

### agent

_Interface_: jenkins_agent_v0
_Supported charms_: [jenkins-agent-k8s](https://charmhub.io/jenkins-agent-k8s),
[jenkins-agent](https://charmhub.io/jenkins-agent)

Jenkins agents provide a way to perform tasks scheduled by the Jenkins server. Jenkins agents are
used to distribute workload across multiple containers, allowing parallel execution of jobs.

Example agent relate command: `juju relate jenkins-k8s:agent jenkins-agent-k8s:agent`

To create a [cross model relation](https://juju.is/docs/olm/manage-cross-model-integrations) with
a jenkins-agent (VM) charm, create an offer from the machine model.

`juju offer jenkins-agent:agent`

Then, relate the offer from the k8s model where jenkins-k8s charm resides.

`juju relate jenkins-k8s:agent <controller-name>:<juju-user>/<agent-model>.jenkins-agent`

An example of such command would look like the following, using a jenkins-agent deployed on a
localhost
[lxd controller](https://juju.is/docs/olm/get-started-with-juju#heading--prepare-your-cloud).

`juju relate jenkins-k8s:agent localhost:admin/jenkins-vm-model.jenkins-agent`
