# CI Failure Diagnosis: PR #168 (fix/websocket branch)

## Issue Summary

The integration tests on amd64 are failing during the **setup phase**, not during the actual test execution.

### Failure Location
- **Job**: `integration-tests-amd64 / Integration tests / Integration tests` (ID: 85784663609)
- **Status**: Failed
- **Phase**: Pre-run script (`tests/integration/setup_microk8s.sh`)
- **Command**: `sg snap_microk8s -c 'microk8s enable storage'`

### Error Message
```
Elevated permissions are needed for this command. Please use sudo.
##[error]Process completed with exit code 1.
```

### Root Cause

The setup script `tests/integration/setup_microk8s.sh` uses `sg snap_microk8s -c "microk8s ..."` commands without `sudo` prefix. The microk8s CLI is now requiring elevated permissions that `sg` alone doesn't provide.

Failing line in `tests/integration/setup_microk8s.sh`:
```bash
sg snap_microk8s -c "microk8s enable storage"
```

All subsequent `sg snap_microk8s -c "microk8s ..."` commands will also fail with the same error.

### Recent Changes

In commit `c2f03d2` (fix(ci): add MetalLB disable/enable cycle for reliability), the setup script was modified to:
1. Export microk8s config
2. Add MetalLB disable/enable cycle

However, the core issue is that ALL the `sg snap_microk8s -c "microk8s ..."` commands need sudo.

### Why Non-amd64 Tests Pass

The non-amd64 tests (arm64, s390x, ppc64le) all **passed** because they use `--use-docker` flag and skip the microk8s setup entirely. They deploy Jenkins in Docker containers instead of using microk8s.

### Solution

All `sg snap_microk8s -c "microk8s ..."` commands in `tests/integration/setup_microk8s.sh` need to be prefixed with `sudo`:

```bash
# Current (BROKEN):
sg snap_microk8s -c "microk8s enable storage"

# Fixed:
sudo sg snap_microk8s -c "microk8s enable storage"
```

This needs to be applied to all lines using `sg snap_microk8s -c "microk8s ..."` in the script:
- Line 15: `microk8s enable storage`
- Line 18: `microk8s enable metallb:10.64.140.43-10.64.140.49`
- Line 20: `microk8s disable metallb`
- Line 21: `microk8s enable metallb:10.64.140.43-10.64.140.49`
- Line 22: `microk8s status --wait-ready`
- Line 25: `juju bootstrap localhost localhost`
- Line 28: `juju bootstrap microk8s microk8s`
- Line 31: `juju switch localhost`

## Technical Detail

In commit `eb73772`, the workflow changed from **inline multi-line script** to **inline multi-line script with separate commands**:
```yaml
# Before eb73772:
pre-run-script: |\n  -c \"sudo microk8s config > ${GITHUB_WORKSPACE}/kube-config\n  ./tests/integration/setup_microk8s.sh\"

# After eb73772:
pre-run-script: |\n  sudo microk8s config > ${GITHUB_WORKSPACE}/kube-config\n  chmod +x tests/integration/setup_microk8s.sh\n  ./tests/integration/setup_microk8s.sh
```

Then in commit `c2f03d2`, the format changed to **file path only**:
```yaml
pre-run-script: tests/integration/setup_microk8s.sh
```

And the `sudo microk8s config` line was moved INTO `setup_microk8s.sh`.

When operator-workflows receives a file path (not inline content), it executes: `bash -xe <filepath>`, which runs as the ubuntu user without sudo context. The `sg snap_microk8s` commands then fail because microk8s now requires elevated permissions.

## Recommendation

Update `tests/integration/setup_microk8s.sh` to prefix all `sg snap_microk8s` commands with `sudo`.
