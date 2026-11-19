#!/usr/bin/env bash
source "$(dirname "$0")/docker/common.sh"
require_docker
require_config
reject_legacy
check_images
dc run --rm --no-deps --pull never collector validate
dc up -d --no-build --pull never --wait --wait-timeout 180
bash "$TASK_ROOT/status.sh"
