#!/bin/bash

# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

# Pre-run script for integration test operator-workflows action.
# https://github.com/canonical/operator-workflows/blob/main/.github/workflows/integration_test.yaml

# Jenkins machine agent charm is deployed on lxd and Jenkins-k8s server charm is deployed on
# microk8s.

# Export microk8s config for integration tests
sudo microk8s config | sudo tee "${GITHUB_WORKSPACE}/kube-config" > /dev/null

sudo sg snap_microk8s -c "microk8s enable storage"
# Enable MetalLB for LoadBalancer service support (required by traefik-k8s)
# Use a local IP range that won't conflict with the host network
sudo sg snap_microk8s -c "microk8s enable metallb:10.64.140.43-10.64.140.49"
# Restart metallb - metallb can be flaky and stop setup midway
sudo sg snap_microk8s -c "microk8s disable metallb"
sudo sg snap_microk8s -c "microk8s enable metallb:10.64.140.43-10.64.140.49"
sudo sg snap_microk8s -c "microk8s status --wait-ready"
# lxd should be installed and inited by a previous step in integration test action.
echo "bootstrapping lxd juju controller"
sudo sg snap_microk8s -c "juju bootstrap localhost localhost"

echo "bootstrapping secondary microk8s controller"
sudo sg snap_microk8s -c "juju bootstrap microk8s microk8s"

echo "Switching to testing model"
sudo sg snap_microk8s -c "juju switch localhost"
