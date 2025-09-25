#!/bin/bash

# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

echo "Setting up any-charm"
git clone https://github.com/canonical/any-charm
cd any-charm && charmcraft pack && mv *.charm ../ && cd ..
