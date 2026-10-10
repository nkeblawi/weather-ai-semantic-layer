#!/usr/bin/env bash
# Backfill the obs pipeline from a given date through yesterday.
#
# Usage: scripts/backfill.sh --date YYYY-MM-DD --env dev|prod
#
# Both flags are required (no default env, so prod is never hit by
# accident). --env picks the bundle target: job IDs, the workspace file
# path, and the catalog all come from `databricks bundle summary -t <env>`,
# so dev runs hit the "[dev ...]" jobs and weather_dev. --env prod prints
# the plan and asks for confirmation before any job is triggered.
#
# Procedure (each run waits for the previous one):
#   1. wx_obs_ingest_ghcn  -- one run, tail bytes sized to cover the gap
#                             (--full if the gap is over 12 months)
#   2. wx_obs_ingest_acis  -- one run per date for the last 2 days only;
#                             GHCN wins in merge_obs wherever it has data,
#                             so ACIS only matters for days GHCN hasn't
#                             published yet
#   3. wx_obs_transform    -- one run per selected station with --station-id.
#                             Without it, pivot_obs/derived_vars only
#                             reprocess obs_date >= ghcn_last_date, which
#                             the GHCN catch-up has already moved past the
#                             gap. --station-id skips that watermark and
#                             re-pivots the station's full history.
#
# Failure handling: a failed ingest run skips the transform step (merging
# partial ingest would leave the gap half-filled). A failed transform run
# for one station doesn't stop the others.
#
# Ends with a pass/fail summary of every run and, for stations whose
# transform succeeded, the latest obs_date in analytics.observations.
# Exits non-zero if any run failed.
#
# Requires: databricks CLI (authenticated), jq, python3.
# Does NOT unpause job schedules -- do that separately.

set -euo pipefail

usage() {
    echo "Usage: $0 --date YYYY-MM-DD --env dev|prod" >&2
    exit 1
}

START_DATE=""
TARGET=""
while (( $# )); do
    case "$1" in
        --date) START_DATE="${2:-}"; shift 2 || usage ;;
        --env)  TARGET="${2:-}";     shift 2 || usage ;;
        -h|--help) usage ;;
        *) echo "Unknown argument: $1" >&2; usage ;;
    esac
done

if [[ ! "$START_DATE" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]; then
    echo "--date must be YYYY-MM-DD (got: ${START_DATE:-nothing})" >&2
    usage
fi
# Format alone lets through dates like 2026-02-30; check it's a real
# calendar date and that there's at least one day (through yesterday) to fill.
if ! DATE_ERR=$(python3 - "$START_DATE" 2>&1 <<'EOF'
import sys
from datetime import date, timedelta
try:
    start = date.fromisoformat(sys.argv[1])
except ValueError as e:
    sys.exit(f"--date {sys.argv[1]} is not a valid date ({e})")
yesterday = date.today() - timedelta(days=1)
if start > yesterday:
    sys.exit(f"--date must be yesterday ({yesterday}) or earlier (got: {start})")
EOF
); then
    echo "$DATE_ERR" >&2
    usage
fi
if [[ "$TARGET" != "prod" && "$TARGET" != "dev" ]]; then
    echo "--env must be dev or prod (got: ${TARGET:-nothing})" >&2
    usage
fi
WAREHOUSE_ID="${WX_WAREHOUSE_ID:-806413d104ce2150}"  # Serverless Starter Warehouse
RUN_TIMEOUT_SECS=3600
POLL_SECS=20
ACIS_DAYS=2
TAIL_BYTES_PER_MONTH=40000
FULL_THRESHOLD_MONTHS=12

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

job_id() {
    # Job ID for a bundle resource key in the selected target.
    local id
    id=$(jq -r --arg k "$1" '.resources.jobs[$k].id // empty' <<< "$BUNDLE")
    if [[ -z "$id" ]]; then
        echo "Job $1 not deployed in target $TARGET" >&2
        exit 1
    fi
    echo "$id"
}

run_sql() {
    # run_sql "SELECT ..." -- prints rows tab-separated.
    databricks api post /api/2.0/sql/statements --json "$(jq -cn \
        --arg wh "$WAREHOUSE_ID" --arg sql "$1" \
        '{warehouse_id: $wh, statement: $sql, wait_timeout: "50s"}')" \
        | jq -r 'if .status.state == "SUCCEEDED" then (.result.data_array // [])[] | @tsv
                 else error("query \(.status.state): \(.status.error.message // "")") end'
}

# Run results, parallel arrays (bash 3.2 has no associative arrays).
RESULT_LABELS=()
RESULT_STATES=()
RESULT_URLS=()

run_job() {
    # run_job LABEL JOB_ID extra_param...
    # Blocks until the run terminates, records its result, and returns
    # 0 on SUCCESS, 1 otherwise. Never exits the script.
    local label="$1" id="$2"; shift 2
    local params run_id run state life result url elapsed=0

    # Fed via stdin: jq would parse "--flag" positional args as its own options.
    params=$(printf '%s\n' --workspace-root "$WORKSPACE_ROOT" --catalog "$CATALOG" "$@" \
        | jq -R . | jq -cs .)

    echo "  -> $label"
    if ! run_id=$(databricks jobs run-now --no-wait -o json \
            --json "{\"job_id\": $id, \"python_params\": $params}" | jq -er '.run_id'); then
        state="SUBMIT_FAILED"; url=""
    else
        while :; do
            run=$(databricks jobs get-run "$run_id" -o json)
            life=$(jq -r '.state.life_cycle_state' <<< "$run")
            case "$life" in
                TERMINATED|SKIPPED|INTERNAL_ERROR) break ;;
            esac
            if (( elapsed >= RUN_TIMEOUT_SECS )); then
                life="TIMED_OUT"; break
            fi
            sleep "$POLL_SECS"; elapsed=$(( elapsed + POLL_SECS ))
        done
        result=$(jq -r '.state.result_state // empty' <<< "$run")
        url=$(jq -r '.run_page_url // empty' <<< "$run")
        if [[ "$life" == "TERMINATED" ]]; then state="${result:-UNKNOWN}"; else state="$life"; fi
    fi

    echo "     $state"
    RESULT_LABELS+=("$label"); RESULT_STATES+=("$state"); RESULT_URLS+=("$url")
    [[ "$state" == "SUCCESS" ]]
}

# ---------------------------------------------------------------------------
# Date math (python3 for portable date arithmetic)
# ---------------------------------------------------------------------------

read -r MONTHS ACIS_DATES < <(python3 - "$START_DATE" "$ACIS_DAYS" <<'EOF'
import sys
from datetime import date, timedelta
start = date.fromisoformat(sys.argv[1])
acis_days = int(sys.argv[2])
yesterday = date.today() - timedelta(days=1)
if start > yesterday:
    sys.exit(f"Start date {start} is not before today")
# Calendar months touched by [start, yesterday]
months = (yesterday.year - start.year) * 12 + (yesterday.month - start.month) + 1
acis_start = max(start, yesterday - timedelta(days=acis_days - 1))
acis = [(acis_start + timedelta(days=i)).isoformat()
        for i in range((yesterday - acis_start).days + 1)]
print(months, ",".join(acis))
EOF
)
[[ -n "${MONTHS:-}" ]] || exit 1  # python already printed the reason

if (( MONTHS > FULL_THRESHOLD_MONTHS )); then
    GHCN_ARGS=(--full)
else
    GHCN_ARGS=(--tail-bytes $(( (MONTHS + 1) * TAIL_BYTES_PER_MONTH )))
fi

# bundle summary must run from the bundle root
BUNDLE=$(cd "$(dirname "$0")/.." && databricks bundle summary -t "$TARGET" -o json)
CATALOG=$(jq -r '.variables.catalog.value' <<< "$BUNDLE")
WORKSPACE_ROOT="$(jq -r '.workspace.file_path' <<< "$BUNDLE")/src"

GHCN_JOB=$(job_id wx_obs_ingest_ghcn)
ACIS_JOB=$(job_id wx_obs_ingest_acis)
TRANSFORM_JOB=$(job_id wx_obs_transform)

# Selected stations (same filter as get_selected_stations())
STATIONS=$(run_sql "SELECT station_id FROM ${CATALOG}.cleaned.station_merged WHERE selected = true ORDER BY station_id")
if [[ -z "$STATIONS" ]]; then
    echo "No selected stations found" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

echo "Backfill [$TARGET -> $CATALOG] from $START_DATE ($MONTHS month(s))"
echo "  GHCN:     ${GHCN_ARGS[*]}"
echo "  ACIS:     ${ACIS_DATES//,/ }"
echo "  Stations: $(echo $STATIONS)"
echo

# Prod safeguard: show the plan above, then require an explicit answer.
# y/yes continues, n/no aborts (case-insensitive); anything else re-asks.
# Refuses outright when there's no terminal to answer from.
if [[ "$TARGET" == "prod" ]]; then
    if [[ ! -t 0 ]]; then
        echo "Refusing to run against prod without an interactive terminal to confirm." >&2
        exit 1
    fi
    while :; do
        # read fails on EOF (Ctrl-D) -- treat as abort.
        read -r -p "About to run against PROD ($CATALOG). Continue? [y/n] " answer || answer="n"
        # tr instead of ${answer,,}: macOS ships bash 3.2.
        case "$(tr '[:upper:]' '[:lower:]' <<< "$answer")" in
            y|yes) break ;;
            n|no)  echo "Aborted."; exit 1 ;;
            *)     echo "Please answer y or n." ;;
        esac
    done
    echo
fi

INGEST_OK=1

echo "[1/3] wx_obs_ingest_ghcn ($GHCN_JOB)"
run_job "ingest_ghcn ${GHCN_ARGS[*]}" "$GHCN_JOB" "${GHCN_ARGS[@]}" || INGEST_OK=0

echo "[2/3] wx_obs_ingest_acis ($ACIS_JOB)"
IFS=',' read -ra ACIS_LIST <<< "$ACIS_DATES"
for d in "${ACIS_LIST[@]}"; do
    run_job "ingest_acis $d" "$ACIS_JOB" --date "$d" || INGEST_OK=0
done

OK_STATIONS=()
if (( INGEST_OK )); then
    echo "[3/3] wx_obs_transform ($TRANSFORM_JOB)"
    for s in $STATIONS; do
        run_job "transform $s" "$TRANSFORM_JOB" --station-id "$s" && OK_STATIONS+=("$s")
    done
else
    echo "[3/3] wx_obs_transform -- SKIPPED (an ingest run failed)"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

echo
echo "=== Job runs ==="
FAILED=0
for i in "${!RESULT_LABELS[@]}"; do
    if [[ "${RESULT_STATES[$i]}" == "SUCCESS" ]]; then
        verdict="PASS"
    else
        verdict="FAIL"; FAILED=$(( FAILED + 1 ))
    fi
    printf '  %-4s  %-34s  %-14s  %s\n' "$verdict" "${RESULT_LABELS[$i]}" \
        "${RESULT_STATES[$i]}" "${RESULT_URLS[$i]}"
done

if (( ${#OK_STATIONS[@]} )); then
    in_list=$(printf "'%s'," "${OK_STATIONS[@]}"); in_list="${in_list%,}"
    echo
    echo "=== Backfilled up to (${CATALOG}.analytics.observations) ==="
    UP_TO=$(run_sql "SELECT station_id, CAST(MAX(obs_date) AS STRING)
                     FROM ${CATALOG}.analytics.observations
                     WHERE station_id IN (${in_list})
                     GROUP BY station_id")
    for s in "${OK_STATIONS[@]}"; do
        max_date=$(awk -F'\t' -v s="$s" '$1 == s {print $2}' <<< "$UP_TO")
        printf '  %-12s  %s\n' "$s" "${max_date:-no rows}"
    done
fi

echo
if (( FAILED )); then
    echo "$FAILED run(s) failed."
    exit 1
fi
echo "All runs passed."
