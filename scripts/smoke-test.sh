#!/usr/bin/env bash
set -euo pipefail

curl --fail --silent --show-error http://127.0.0.1:9187/health/live
echo
curl --fail --silent --show-error http://127.0.0.1:9187/health/ready
echo
systemctl --no-pager --full status collector-offline.service
