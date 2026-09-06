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

Revision 265 ran the agent as `root`. It can leave `agent.jar`, `remoting`,
and `workspace` under `/var/lib/jenkins` inaccessible to the dedicated service
user used by current revisions.

During the first service start after an upgrade, the launcher atomically replaces
`agent.jar`. It does not recursively change file ownership. If `remoting` or
`workspace` is inaccessible, the launcher moves that generated runtime directory
to `.jenkins-agent-legacy-remoting` or `.jenkins-agent-legacy-workspace` in the
same Jenkins home, then creates a writable replacement. Moving the top-level
directory keeps startup time constant and preserves the previous cache or
workspace data for manual recovery. After you confirm that the agent and its jobs
work, you can archive or remove the preserved directories according to your data
retention policy.
