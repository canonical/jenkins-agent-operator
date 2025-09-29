#!/bin/bash

# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

# Required to install cryptography lib on S390X & PPC
echo "Installing required packages"
sudo apt-get install -y cargo pkg-config
