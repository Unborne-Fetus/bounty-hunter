#!/bin/sh
set -eu

cd "$(dirname "$0")/.."

set -- python3 -m bounty_hunter.cli
while IFS= read -r org; do
  case "$org" in
    ""|\#*) continue ;;
  esac
  set -- "$@" --algora-org "$org"
done < config/algora_orgs.txt

PYTHONDONTWRITEBYTECODE=1 "$@"
python3 -m bounty_hunter.workflow summary
