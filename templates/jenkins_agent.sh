#!/bin/bash

# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

err() {
  echo "[$(date +'%Y-%m-%dT%H:%M:%S%z')]: $*" >&2
}

info() {
   echo "[$(date +'%Y-%m-%dT%H:%M:%S%z')]: $*"
}

set -eu -o pipefail

export LC_ALL=C
export TERM=xterm

readonly JENKINS_HOME="/var/lib/jenkins"

if ! mkdir -p $JENKINS_HOME; then
    err "Error initializing the agent's home directory"
    exit 1
fi
cd $JENKINS_HOME

CONFIG_MISSING=false
if [ -z ${JENKINS_URL+x} ]; then
    CONFIG_MISSING=true
    err "JENKINS_URL needs to be configured"
fi
if [ -z ${JENKINS_AGENT+x} ]; then
    CONFIG_MISSING=true
    err "JENKINS_AGENT needs to be configured"
fi
if [ -z ${JENKINS_TOKEN+x} ]; then
    CONFIG_MISSING=true
    err "JENKINS_TOKEN needs to be configured"
fi

if [ "$CONFIG_MISSING" == true ]; then
    err "Invalid configuration, missing value(s)"
    exit 1
fi

info "Fetching the agent binary"
if ! curl --connect-timeout 1200 "${JENKINS_URL}/jnlpJars/agent.jar" -o ${JENKINS_HOME}/agent.jar; then
    err Unable to download agent binary
    exit 1
fi

# Specify the agent as ready
touch "${JENKINS_HOME}/.ready"
info "Connecting to jenkins"
if ! java -jar "${JENKINS_HOME}/agent.jar" -jnlpUrl "${JENKINS_URL}/computer/${JENKINS_AGENT}/slave-agent.jnlp" -workDir "${JENKINS_HOME}" -noReconnect -secret "${JENKINS_TOKEN}"; then
    err "Error connecting to jenkins"
    # Remove ready mark if unsuccessful
    rm $JENKINS_HOME/.ready
    exit 1
fi
