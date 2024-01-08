# Integrations

### agent

_Interface_: jenkins_agent_v0
_Supported charms_: [jenkins-k8s](https://charmhub.io/jenkins-agent-k8s),

Jenkins agents provide a way to perform tasks scheduled by the Jenkins server. Jenkins agents are
used to distribute workload across multiple containers, allowing parallel execution of jobs.

To create a [cross model relation](https://juju.is/docs/olm/manage-cross-model-integrations) with
a jenkins-agent (VM) charm, create an offer from the machine model.

`juju offer jenkins-agent:agent`

Then, integrate the offer from the k8s model where jenkins-k8s charm resides.

`juju integrate jenkins-k8s:agent <controller-name>:<juju-user>/<agent-model>.jenkins-agent`

An example of such command would look like the following, using a jenkins-k8s charm deployed on microk8s.
`juju integrate jenkins-k8s:agent localhost:admin/jenkins-agent-model.jenkins-agent`
