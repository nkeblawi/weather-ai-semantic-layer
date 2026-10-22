#!/usr/bin/env bash
# Deploy the Unity Catalog SQL functions in sql/uc/ to dev or prod.
#
# Usage: scripts/deploy_sql.sh --env dev|prod [--dry-run] [function ...]
#
# --dry-run rewrites each file for the target catalog, writes the result to
# a temp directory for inspection, and lists every catalog reference in it.
# Nothing is sent to Databricks and no prod confirmation is asked.
#
# With no function names, deploys the query layer in dependency order:
# query_by_day, query_by_event, query_by_period, answer_query (answer_query
# calls the other three, so it goes last). lookup_station is deployed only
# when named explicitly.
#
# The .sql files are written against the prod catalog (weather.analytics.*);
# --env dev rewrites every "weather.analytics." to "weather_dev.analytics."
# before submitting. --env prod prints the plan and asks for confirmation.
#
# Statements run on the SQL warehouse from DATABRICKS_HTTP in .env, through
# the SQL Statements API as the CLI's authenticated user (not the app's
# service principal).
#
# GRANTS: CREATE OR REPLACE FUNCTION drops and recreates the function
# (new function_id, created_at = deploy time), so grants made on the
# function itself are lost (verified on weather_dev and weather, 2026-10-17).
# The app's service principal has EXECUTE on the analytics schema, which
# covers recreated functions, so no re-grant is needed. Keep that grant at
# the schema level (see docs/governance.md).
#
# Requires: databricks CLI (authenticated), jq.

set -euo pipefail

usage() {
    echo "Usage: $0 --env dev|prod [--dry-run] [function ...]" >&2
    exit 2
}

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SQL_DIR="$ROOT/sql/uc"
ENV=""
DRY_RUN=0
FUNCTIONS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --env) ENV="${2:-}"; shift 2 ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h|--help) usage ;;
        -*) echo "Unknown flag: $1" >&2; usage ;;
        *) FUNCTIONS+=("$1"); shift ;;
    esac
done

case "$ENV" in
    dev) CATALOG="weather_dev" ;;
    prod) CATALOG="weather" ;;
    *) usage ;;
esac

if [[ ${#FUNCTIONS[@]} -eq 0 ]]; then
    FUNCTIONS=(query_by_day query_by_event query_by_period answer_query)
fi

for fn in "${FUNCTIONS[@]}"; do
    [[ -f "$SQL_DIR/$fn.sql" ]] || { echo "No such file: sql/uc/$fn.sql" >&2; exit 2; }
done

HTTP_PATH="$(grep -E '^DATABRICKS_HTTP=' "$ROOT/.env" | cut -d= -f2- | tr -d '"'"'"'')"
WAREHOUSE_ID="${HTTP_PATH##*/}"
[[ -n "$WAREHOUSE_ID" ]] || { echo "DATABRICKS_HTTP not found in .env" >&2; exit 2; }

rewrite() {
    sed "s/weather\.analytics\./$CATALOG.analytics./g" "$SQL_DIR/$1.sql"
}

if [[ "$DRY_RUN" -eq 1 ]]; then
    TMP_BASE="${TMPDIR:-/tmp}"
    OUT_DIR="$(mktemp -d "${TMP_BASE%/}/deploy_sql.XXXXXX")"
    echo "DRY RUN: would deploy to $CATALOG.analytics on warehouse $WAREHOUSE_ID"
    echo "Rewritten SQL in $OUT_DIR"
    for fn in "${FUNCTIONS[@]}"; do
        rewrite "$fn" > "$OUT_DIR/$fn.sql"
        echo
        echo "  $fn.sql ($(wc -l < "$OUT_DIR/$fn.sql" | tr -d ' ') lines)"
        grep -n -oE '[a-z_]+\.analytics\.[a-z_]+' "$OUT_DIR/$fn.sql" | sed 's/^/    line /'
        # any prod reference surviving a dev rewrite is a bug in the rewrite
        if [[ "$CATALOG" != "weather" ]] && grep -q 'weather\.analytics\.' "$OUT_DIR/$fn.sql"; then
            echo "    WARNING: still references weather.analytics"
        fi
    done
    exit 0
fi

echo "Deploying to $CATALOG.analytics on warehouse $WAREHOUSE_ID:"
printf '  %s\n' "${FUNCTIONS[@]}"

if [[ "$ENV" == "prod" ]]; then
    if [[ ! -t 0 ]]; then
        echo "Refusing to deploy to prod without a terminal to confirm." >&2
        exit 1
    fi
    while true; do
        read -r -p "Deploy to PROD? [y/n] " answer
        case "$answer" in
            y|Y|yes) break ;;
            n|N|no) echo "Aborted."; exit 1 ;;
        esac
    done
fi

failed=0
for fn in "${FUNCTIONS[@]}"; do
    sql="$(rewrite "$fn")"
    body="$(jq -n --arg s "$sql" --arg w "$WAREHOUSE_ID" \
        '{statement: $s, warehouse_id: $w, wait_timeout: "50s", on_wait_timeout: "CANCEL"}')"
    resp="$(databricks api post /api/2.0/sql/statements --json "$body")"
    state="$(jq -r '.status.state' <<<"$resp")"
    if [[ "$state" == "SUCCEEDED" ]]; then
        echo "  PASS  $CATALOG.analytics.$fn"
    else
        echo "  FAIL  $CATALOG.analytics.$fn ($state): $(jq -r '.status.error.message // "no message"' <<<"$resp")"
        failed=1
        # answer_query depends on the others; stop rather than deploy a
        # dispatcher whose callee failed to update
        break
    fi
done

exit "$failed"
