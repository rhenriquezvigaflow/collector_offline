#!/usr/bin/env bash
source "$(dirname "$0")/docker/common.sh"
require_docker
require_config
# Stops in dependency order; never removes containers or volumes.
dc stop
