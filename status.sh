#!/usr/bin/env bash
source "$(dirname "$0")/docker/common.sh"
require_docker
require_config
dc ps --all
dc exec -T postgres sh -c 'pg_isready -U postgres -d "$POSTGRES_DB"'
dc exec -T collector python -m collector_offline.container healthcheck
