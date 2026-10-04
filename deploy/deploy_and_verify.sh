#!/usr/bin/env bash
# One command: update the checkout, restart the paper engine, verify what it reports.
#
#   deploy/deploy_and_verify.sh [--dry-run] [--skip-tests]
#
# Env: LIP_DEPLOY_BRANCH (default: current branch), LIP_RESTART_CMD (default
# "sudo systemctl restart lip-unattended.service"), LIP_STATUS_URL (default
# http://127.0.0.1:8765/status), LIP_VERIFY_WAIT_S (default 120).
# It never reads or sets an arming flag (paper/live acknowledgement, maker-only
# enforcement, watchdog arming). Stops at the first failure.
set -euo pipefail
cd "$(dirname "$0")/.."

DRY=0; TESTS=1
for a in "$@"; do
  case "$a" in
    --dry-run) DRY=1 ;;
    --skip-tests) TESTS=0 ;;
    *) echo "unknown argument: $a" >&2; exit 2 ;;
  esac
done

BRANCH="${LIP_DEPLOY_BRANCH:-$(git rev-parse --abbrev-ref HEAD)}"
RESTART="${LIP_RESTART_CMD:-sudo systemctl restart lip-unattended.service}"
URL="${LIP_STATUS_URL:-http://127.0.0.1:8765/status}"
WAIT="${LIP_VERIFY_WAIT_S:-120}"

run() { if [ "$DRY" = 1 ]; then echo "DRY RUN: $*"; else echo "+ $*"; eval "$@"; fi; }

run "git fetch origin '$BRANCH'"
run "git merge --ff-only 'origin/$BRANCH'"
if [ "$TESTS" = 1 ]; then run "python -m pytest tests -q -x"; fi
run "$RESTART"
EXPECT="\$(git rev-parse HEAD)"
run "python deploy/verify_deploy.py --url '$URL' --wait-s '$WAIT' --expect-commit \"$EXPECT\""
