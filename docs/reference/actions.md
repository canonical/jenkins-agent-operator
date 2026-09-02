# Actions

## `migrate-runtime-directory`

Migrate ownership and owner permissions for an existing directory in place. The
action uses the configured `agent_user` as the target user; it does not use the
action process user, which normally runs as `root`.

If `directory` is omitted or empty, the action migrates the configured
`jenkins_home` (default: `/var/lib/jenkins`). A specified directory must be an
existing directory under that Jenkins home. The action recursively changes
ownership for that selected tree, preserves its paths and contents, and does not
create an archive. It does not follow symbolic links or cross-device filesystem
boundaries. The filesystem check does not distinguish bind mounts on the same
filesystem; detach it, or select a directory that does not contain it, if the
mount must not be changed before running the action.

Run it after an upgrade from a root-running revision when the charm blocks on
legacy runtime ownership:

```bash
juju run --wait=5m jenkins-agent/0 migrate-runtime-directory
```

If `jenkins-agent` is active, the action stops it before migration and restarts it
after a successful migration. If the service is already stopped, the action leaves
it stopped; the charm starts it on the next reconciliation when its prerequisites
are ready. In either case, run the action when no Jenkins job is modifying the
selected tree.

To migrate only a subdirectory, pass `directory` explicitly:

```bash
juju run --wait=5m jenkins-agent/0 migrate-runtime-directory \
  directory=/var/lib/jenkins/workspace
```

The charm's startup gate checks both top-level `remoting` and `workspace`.
Migrating only one subdirectory will not unblock the unit if the other tree still
has unsafe ownership or access. Migrate both trees (or the complete Jenkins home)
when both are affected.

The walk is O(n) in the number of entries and the action has no server-side
timeout. `--wait` is only the Juju client wait; choose a value large enough for
large workspaces. The action fails if stopping or restarting the service fails. A
migration failure leaves a previously running service stopped so the tree can be
repaired and retried safely.

> **Deprecation notice:** This action is a temporary compatibility path for
> root-running revisions such as revision 265. It may be removed in a future
> release after those revisions are no longer supported.

See [Actions](https://charmhub.io/jenkins-agent/actions).

> Read more about actions in the Juju docs: [Action](https://documentation.ubuntu.com/juju/3.6/reference/action/)
