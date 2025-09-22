#!/bin/bash

# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

# Pre-run script for integration test operator-workflows action.
# https://github.com/canonical/operator-workflows/blob/main/.github/workflows/integration_test.yaml

# Cargo and pkg-config is required to build cryptography package on s390x and ppc64le
sudo apt-get install -y cargo pkg-config

# Jenkins machine agent charm is deployed on lxd and Jenkins-k8s server charm is deployed on
# microk8s.
sg snap_microk8s -c "microk8s enable storage"
sg snap_microk8s -c "microk8s status --wait-ready"
# lxd should be installed and inited by a previous step in integration test action.
echo "bootstrapping lxd juju controller"
sg snap_microk8s -c "juju bootstrap localhost localhost"

echo "bootstrapping secondary microk8s controller"
sg snap_microk8s -c "juju bootstrap microk8s microk8s"

echo "Switching to testing model"
sg snap_microk8s -c "juju switch localhost"

echo "Starting Jenkins docker service"
docker run -p 8080:8080 -p 50000:50000 --name jenkins --restart=on-failure docker.io/jenkins/jenkins:lts-jdk17
docker exec <jenkins_container_id_or_name> cat /var/jenkins_home/secrets/initialAdminPassword
