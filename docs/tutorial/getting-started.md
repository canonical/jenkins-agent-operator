# Getting Started

## What you'll do

- Deploy the [jenkins-agent charm](https://charmhub.io/jenkins-agent)
- Deploy the [jenkins-k8s charm](https://charmhub.io/jenkins-k8s) and integrate with it via a cross-model integration

The `jenkins-agent` charm helps deploy a Jenkins agent with ease and also helps operate the charm. This
tutorial will walk you through each step of deployment to get a basic Jenkins agent deployment and integrate it with Jenkins.

### Prerequisites

To deploy the `jenkins-agent` charm, you'll need to have a bootstrapped machine model. Learn about
bootstrapping different clouds [here](https://juju.is/docs/olm/get-started-with-juju#heading--prepare-your-cloud).

Use `juju bootstrap localhost localhost` to bootstrap a `lxd` machine controller with the name
`localhost` for tutorial purposes.

### Setting up the tutorial model

To easily clean up the resources and to separate your workload from the contents of this tutorial,
it is recommended to set up a new model with the following command.

```
juju add-model tutorial
```

### Deploy the jenkins-agent charm

Start off by deploying the jenkins-agent charm. By default it will deploy the latest stable release
of the jenkins-agent charm.

```
# Deploy an edge version of the charm until stable version is released.
juju deploy jenkins-agent --channel=latest/edge
```

### Deploy and integrate with the jenkins-k8s charm

To deploy jenkins-k8s charm, you will need a juju bootstrapped with any kubernetes controller.
To see how to bootstrap your juju installation with microk8s, please refer to the documentation
on microk8s [installation](https://juju.is/docs/olm/microk8s).

Use `juju bootstrap microk8s localhost-microk8s` to bootstrap a `microk8s` machine controller with the name
`localhost-microk8s` for tutorial purposes.

Then, switch to your kubernetes controller add a model for the jenkins-k8s charm with the following command:
```
juju switch -c localhost-microk8s
juju add-model jenkins-tutorial
```

Continue by deploying the jenkins-k8s charm. by default it will deploy the latest stable release of the jenkins-k8s charm:
```
juju deploy jenkins-k8s --channel=latest/edge
```

The Jenkins application can only have a single server unit. Adding more units through --num-units parameter will cause the application to misbehave.

#### Create an offer for Cross Model Integration

To integrate charms
[across different models](https://juju.is/docs/juju/manage-cross-model-integrations), a juju
[`offer`](https://juju.is/docs/juju/manage-cross-model-integrations#heading--create-an-offer) is
required.

Create an offer of the `jenkins-k8s` charm's `agent` integration.

```
juju offer jenkins-k8s:agent
```

The output should look similar to the contents below:

```
Application "jenkins-k8s" endpoints [agent] available at "admin/jenkins-tutorial.jenkins-k8s"
```

#### Integrate the Jenkins agent charm through the offer

Switch back to the k8s model where the `jenkins-agent` charm is deployed. An example of the switch
command looks like the following: `juju switch localhost:tutorial`.

Integrate the `jenkins-agent` charm to the `jenkins-k8s` server charm through the offer.
The syntax of the offer is as follows: `<controller>:<user>/<model>.<charm>`.

```
juju integrate jenkins-agent:agent localhost-microk8s:admin/jenkins-tutorial.jenkins-agent
```


### Cleaning up the environment

Congratulations! You have successfully finished the tutorial. You can now remove the
models that you’ve created using the following command.

```
juju destroy model localhost-microk8s:admin/jenkins-tutorial -y --release-storage
juju destroy model localhost:admin/tutorial -y --release-storage
```