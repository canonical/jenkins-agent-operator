# Actions

## `migrate-runtime-directory`

Migrate ownership and owner permissions for an existing directory in place. The
action uses the configured `agent_user` as the target user; it does not use the
action process user, which normally runs as `root`.

If `directory` is omitted or empty, the action migrates the configured
`jenkins_home` (default: `/var/lib/jenkins`). A specified directory must be an
existing directory under that Jenkins home. The action recursively changes
ownership for that selected tree, preserves its paths and contents, and does not
create an archive. It does not follow symbolic links or cross-filesystem
boundaries.

Run it after an upgrade from a root-running revision when the charm blocks on
legacy runtime ownership. The action refuses to run while `jenkins-agent` is active,
so stop the service first and restart it after the action succeeds:

```bash
juju ssh jenkins-agent/0 -- sudo systemctl stop jenkins-agent
juju run --wait=5m jenkins-agent/0 migrate-runtime-directory
juju ssh jenkins-agent/0 -- sudo systemctl start jenkins-agent
```

To migrate only a subdirectory, pass `directory` explicitly:

```bash
juju run --wait=5m jenkins-agent/0 migrate-runtime-directory \
  directory=/var/lib/jenkins/workspace
```

This maintenance-window requirement prevents the action from racing an active
Jenkins job.

> **Deprecation notice:** This action is a temporary compatibility path for
> root-running revisions such as revision 265. It may be removed in a future
> release after those revisions are no longer supported.

See [Actions](https://charmhub.io/jenkins-agent/actions).

> Read more about actions in the Juju docs: [Action](https://documentation.ubuntu.com/juju/3.6/reference/action/)
