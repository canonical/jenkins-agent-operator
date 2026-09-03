# How to upgrade

Upgrade to the latest revision of the Jenkins agent charm using the `juju refresh` command:

```bash
juju refresh jenkins-agent
```

The upgrade may take several seconds to complete. You can monitor the status of the upgrade using:

```bash
juju status
```

Once the charm is ready, the status will show the new revision number.

## Upgrade from a root-running revision

Revision 265 ran the agent as `root` and may have left `remoting` and
`workspace` under `/var/lib/jenkins` owned by `root`. Current revisions use the
configured service user.

During an upgrade, the charm safely migrates existing top-level `remoting` and
`workspace` trees when possible. It preserves their paths and contents and does
not archive or change unrelated Jenkins state. After a successful migration, the
service starts automatically, including when it was stopped before the upgrade.

If migration cannot proceed, the charm blocks and reports the reason. Fix any
reported symlink, non-directory, or mount issue, then retry with:

```bash
juju run --wait=5m jenkins-agent/0 migrate-runtime-directory
```

The action uses the configured `jenkins_home` by default. To select one subtree:

```bash
juju run --wait=5m jenkins-agent/0 migrate-runtime-directory \
  directory=/var/lib/jenkins/workspace
```

After a successful action, the service also starts automatically whether it was
running or stopped when the action began. If both runtime trees are unsafe,
repair both or select the complete Jenkins home. Set `--wait` high enough for
large trees.

> **Deprecation notice:** In-place migration is a temporary compatibility path
> for upgrades from root-running revisions such as revision 265.
