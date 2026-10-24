#!/usr/bin/env bash
# Collect the full evaluation: 102 policy repos, then 102 comparison repos.
#
#   bash scripts/run_eval.sh graphql   # terminal 1
#   bash scripts/run_eval.sh rest      # terminal 2, at the same time
#
# GraphQL collectors share one hourly budget, so they run one after another in
# terminal 1; REST collectors use a separate budget in terminal 2. The slowest
# collectors run first. A collector that leaves repositories partial or failed
# exits 2; the script logs it and carries on. Afterwards, check each run's
# summary.json and re-run failures (docs/running-collection.md, section 7).
# Policy dates are the verified reported_policy_date (configs/study.yaml), so no
# --policy-run is needed; comparison repos use their stand_in_date.
set -u
cd "$(dirname "$0")/.."

POLICY="--priority evaluation"
COMPARISON="--candidates data/repositories/sampled_comparison_repos.csv --priority evaluation"
case "${1:-}" in
  graphql) COLLECTORS="ci pulls first-seen churn issues discussions" ;;
  rest) COLLECTORS="comments forks releases context stars profiles" ;;
  *) echo "usage: $0 graphql|rest" >&2; exit 64 ;;
esac

LOGS="logs/eval"
mkdir -p "$LOGS"
for group in policy comparison; do
  if [ "$group" = policy ]; then SEL=$POLICY; else SEL=$COMPARISON; fi
  for c in $COLLECTORS; do
    log="$LOGS/$c-$group.log"
    echo "$(date -u +%FT%TZ) start $c ($group)" | tee -a "$LOGS/$1.log"
    # shellcheck disable=SC2086
    uv run aip collect "$c" $SEL 2>&1 | tee "$log"
    status=${PIPESTATUS[0]}
    echo "$(date -u +%FT%TZ) end $c ($group) exit=$status" | tee -a "$LOGS/$1.log"
  done
done
