#!/bin/sh
set -eu

cd "$(dirname "$0")/.."
PYTHONDONTWRITEBYTECODE=1 python3 -m bounty_hunter.algora_discovery "$@"
