# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

Each revision is versioned by the date of the revision.

## 2026-08-31

- Re-own existing Jenkins home contents when the agent runs as a non-root user,
  allowing upgrades from root-based revisions to overwrite the existing agent binary.

## 2026-08-17

- Initialize agent state and service lazily during reconciliation so invalid
  configuration can be corrected without restarting the charm process.
- Publish an explicitly configured agent home as optional `remote_fs` relation metadata;
  the default local `/var/lib/jenkins` home does not claim controller ownership.

## August 12, 2026

- Harden configurable agent user and home directory handling with validated paths,
  least-privilege ownership, and fail-fast account setup.
- Keep password-free privileged access provisioning deterministic and fail closed
  when validation or installation fails.
- Render systemd environment values with systemd escaping instead of HTML
  escaping, reject line-breaking control characters, and avoid logging secrets.
- Fail installation when the agent account, home directory, package setup, or
  privileged-rule validation cannot be completed.

## 2026-07-06

- Add `-websocket` flag to agent connection to support HTTP-only reverse proxies (traefik-k8s ingress).

## 2026-06-23

- Fix deprecated `-jnlpUrl` argument by using `-url` and `-name` when connecting to Jenkins.

## 2026-06-15

- Fix issue with agent applications with same name under different models.
