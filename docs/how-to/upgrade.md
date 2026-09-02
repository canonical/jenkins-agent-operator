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

Revision 265 ran the agent as `root`. It can leave `agent.jar`, `remoting`, and
`workspace` under `/var/lib/jenkins` owned by `root`, while current revisions run
as a dedicated service user.

During the first service start after an upgrade, the launcher replaces `agent.jar`
and the readiness marker atomically. On the upgrade hook, the charm automatically
migrates existing `remoting` and `workspace` trees left with legacy ownership when
it can do so safely. This limited migration preserves paths, contents, and inodes;
it does not archive the Jenkins home or recursively change unrelated state.

If automatic migration fails, the charm leaves the service stopped and enters a
blocked status with the reported reason. Fix any reported symlink, non-directory,
or mount problem manually, then run the ownership migration action to retry:

```bash
juju run --wait=5m jenkins-agent/0 migrate-runtime-directory
```

If the service is already stopped because reconciliation blocked it, the action
leaves it stopped. The charm starts it on the next reconciliation when its
prerequisites are ready (typically within the update-status interval). If it was
active when the action started, the action stops it before migration and restarts
it after a successful migration. The action remains available when automatic
upgrade migration cannot safely complete.

The action defaults to the configured `jenkins_home` and can target a smaller
subdirectory, for example:

```bash
juju run --wait=5m jenkins-agent/0 migrate-runtime-directory \
  directory=/var/lib/jenkins/workspace
```

It updates ownership to the configured service user and group and restores owner
read/write access for files and owner read/write/search access for directories.
Existing paths and contents are retained, so Jenkins can continue using its
previous workspaces. The migration does not follow symbolic links or cross-device
filesystem boundaries. Same-filesystem bind mounts are not distinguished by the
filesystem check; detach the mount, or select a directory that does not contain it,
if it must not be changed. A top-level
symlink, non-directory entry, or nested mount must be handled manually before
retrying the action.

The charm's startup gate checks both top-level `remoting` and `workspace`, so
migrating only `workspace` will not unblock the unit if `remoting` is also unsafe.
The migration walk is O(n) in the number of entries and has no server-side timeout;
set the Juju client's `--wait` value large enough for large workspaces.

> **Deprecation notice:** In-place migration is a temporary compatibility path for
> upgrades from root-running revisions such as revision 265. It may be removed in
> a future release after those revisions are no longer supported.
