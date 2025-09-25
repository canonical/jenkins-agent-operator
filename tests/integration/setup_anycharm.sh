#!/bin/bash

# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

echo "Setting up any-charm"
git clone https://github.com/canonical/any-charm

cat <<EOF > any-charm/charmcraft.yaml
# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

type: charm
bases:
  - build-on:
      - name: "ubuntu"
        channel: "22.04"
        architectures: [amd64]
    run-on:
      - name: "ubuntu"
        channel: "22.04"
        architectures: [amd64]
  - build-on:
      - name: "ubuntu"
        channel: "22.04"
        architectures: [arm64]
    run-on:
      - name: "ubuntu"
        channel: "22.04"
        architectures: [arm64]
  - build-on:
      - name: "ubuntu"
        channel: "22.04"
        architectures: [s390x]
    run-on:
      - name: "ubuntu"
        channel: "22.04"
        architectures: [s390x]
  - build-on:
      - name: "ubuntu"
        channel: "22.04"
        architectures: [ppc64el]
    run-on:
      - name: "ubuntu"
        channel: "22.04"
        architectures: [ppc64el]

parts:
  charm: {}
  wheelhouse:
    plugin: nil
    source: .
    build-packages:
      - python3-pip
    override-build: |
      mkdir -p \$CRAFT_PART_INSTALL/wheelhouse
      cp \$CRAFT_PART_SRC/wheelhouse.txt \$CRAFT_PART_INSTALL
      for package in \$(cat \$CRAFT_PART_SRC/wheelhouse.txt)
      do
        pip wheel --wheel-dir=\$CRAFT_PART_INSTALL/wheelhouse --prefer-binary \$package
      done
EOF

cd any-charm && charmcraft pack && mv *.charm ../ && cd ..
