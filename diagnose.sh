#!/usr/bin/env bash
source "$(dirname "$0")/docker/common.sh"
require_docker
require_config
dc ps --all
# No tag reads, writes, synthetic data or backfill POSTs.
dc run --rm --no-deps --pull never collector diagnose
