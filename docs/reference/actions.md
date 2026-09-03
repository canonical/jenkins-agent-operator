# Actions

## `migrate-runtime-directory`

Migrate an existing Jenkins directory tree in place to the configured
`agent_user` and group. The action process normally runs as `root`, but that
account is not used as the target owner.

If `directory` is omitted, the action uses the configured `jenkins_home`
(default: `/var/lib/jenkins`). A specified path must be an existing directory
under that home.

The action preserves paths and contents. It creates no archive, does not follow
symlinks, and does not cross filesystem boundaries. Same-filesystem bind mounts
are not detected; detach them or exclude them from the selected path first.

Run it when the charm blocks on legacy runtime ownership:

```bash
juju run --wait=5m jenkins-agent/0 migrate-runtime-directory
```

When relation credentials are ready, a successful migration starts the service,
whether it was running or stopped when the action began. The action stops an
active service before migration. Without credentials, it completes the migration
and starts the service on the next reconciliation. If stopping or restarting
fails, it reports the error and leaves the service stopped where possible. Do not
run it while a Jenkins job modifies the selected tree.

To migrate one subdirectory:

```bash
juju run --wait=5m jenkins-agent/0 migrate-runtime-directory \
  directory=/var/lib/jenkins/workspace
```

The startup gate checks both `remoting` and `workspace`; repair both if needed.
`--wait` is the Juju client timeout, so increase it for large trees.

> **Deprecation notice:** This action is a temporary compatibility path for
> root-running revisions such as revision 265.

See [Actions](https://charmhub.io/jenkins-agent/actions).

> Read more about actions in the Juju docs: [Action](https://documentation.ubuntu.com/juju/3.6/reference/action/)
