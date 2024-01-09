# Managing workload inside the charm
The core jenkins agent workload requires 3 main parameters (JENKINS_URL, JENKINS_AGENT and JENKINS_SECRET) and is defined as a 2-step process:

1. Download the agent binary at JENKINS_URL/jnlpJars/agent.jar and store it in the agent’s home directory
2. Run the agent binary with the following parameters to register the node with Jenkins
```
/usr/bin/java -jar agent.jar \\
-jnlpUrl "<jnlp-path-on-jenkins-server>" \\
-workDir "${JENKINS_WORKDIR}" \\
-noReconnect \\
-secret "${JENKINS_SECRET}"
```
In the charm, this workload is managed using an [apt package](https://launchpad.net/~canonical-is-devops/+archive/ubuntu/jenkins-agent-charm) which installs a systemd service that can be configured via a configuration file.
```
# File: /etc/systemd/system/jenkins-agent.service.d/override.conf
[Service]
Environment="JENKINS_SECRET=secret"
Environment="JENKINS_URL=url"
Environment="JENKINS_AGENT=node-name"
```

The service won’t start automatically through the use of the `--no-start` option during packaging in order to allow flexibility between running the workload as a service and as a standalone executable, located at `/usr/bin/jenkins-agent`.