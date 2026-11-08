"""
One-time history load for a station whose GHCN ID changed: the old IDs' .dly
data goes into raw.obs_ghcn under the current ID, so the record runs across
the change. Method and record of each load: docs/station_lineage.md.

- Fill rule: an old ID supplies a (date, variable) only where the current ID
  has no value; old IDs apply in the order given (most recent first).
- It stays filled because GHCN ingest merges with keep_existing_values=True.
- Only for a clean handoff: if the current ID also has an earlier, differently
  measured record, don't fill from the old one (why Vancouver was dropped).
- Run this first (dry_run defaults to true; check the overlap report), then
  wx_activate_station. Reruns are safe; never activate or ingest an old ID.

Run with the wx_load_station_history job (station_id, history_ids separated
by commas, dry_run), or by hand:

    python load_station_history.py --workspace-root <.../files/src> --catalog weather \\
        --station-id CAN07025251 --history-ids CAN07025250 --dry-run
"""

import os
import re
import sys
import argparse

parser = argparse.ArgumentParser()
parser.add_argument("--workspace-root", default=os.environ.get("WX_WORKSPACE_ROOT"))
args, _ = parser.parse_known_args()
if not args.workspace_root:
    parser.error(
        "--workspace-root is required (or set WX_WORKSPACE_ROOT for manual/notebook testing)"
    )
sys.path.append(args.workspace_root)

from wx.utils.bootstrap import bootstrap

bootstrap()

import pandas as pd
from wx.obs.ingest_ghcn import TARGET_SCHEMA, download_dly, parse_dly
from wx.utils.common import merge_into_target
from wx.utils.config import OBS_GHCN_TABLE, STATION_MERGED_TABLE
from wx.utils.http_utils import get_session
from wx.utils.spark_utils import get_spark

# Tenths of deg C -> deg F difference (offset cancels out in a difference)
TENTHS_C_TO_F_DIFF = 0.18

# ---------------------------------------------------------------------------
# Fill logic (pure pandas, no Spark)
# ---------------------------------------------------------------------------


def day_keys(df: pd.DataFrame) -> pd.Series:
    """(date, variable) key per row, as a string, e.g. '1995-07-04|TMAX'."""
    return df["obs_date"].astype(str) + "|" + df["variable"]


def select_history(
    current_df: pd.DataFrame, history: list[tuple[str, pd.DataFrame]]
) -> tuple[pd.DataFrame, list[dict]]:
    """Old-ID rows to load, relabeled to the current ID, and a summary per ID and variable."""
    current_id = current_df["station_id"].iloc[0]
    covered = set(day_keys(current_df[current_df["value"].notna()]))
    selected, summary = [], []

    for old_id, old_df in history:
        if old_df.empty:
            continue
        candidates = old_df[old_df["value"].notna()]
        keys = day_keys(candidates)
        keep = candidates[~keys.isin(covered)].copy()
        covered |= set(keys[~keys.isin(covered)])

        for variable, rows in keep.groupby("variable"):
            summary.append(
                {
                    "old_id": old_id,
                    "variable": variable,
                    "rows": len(rows),
                    "first": rows["obs_date"].min(),
                    "last": rows["obs_date"].max(),
                }
            )

        keep["station_id"] = current_id
        selected.append(keep)

    loaded = (
        pd.concat(selected, ignore_index=True) if selected else current_df.iloc[0:0]
    )
    return loaded, summary


def overlap_check(
    current_df: pd.DataFrame, old_id: str, old_df: pd.DataFrame
) -> list[dict]:
    """Days both IDs have, compared: a large difference suggests a site or instrument change."""
    both = current_df.merge(
        old_df, on=["obs_date", "variable"], suffixes=("_new", "_old")
    ).dropna(subset=["value_new", "value_old"])

    results = []
    for variable in ("TMAX", "TMIN", "PRCP"):
        rows = both[both["variable"] == variable]
        if rows.empty:
            continue
        diff = rows["value_old"] - rows["value_new"]
        result = {
            "old_id": old_id,
            "variable": variable,
            "days": len(rows),
            "first": rows["obs_date"].min(),
            "last": rows["obs_date"].max(),
        }
        if variable == "PRCP":
            new_total = rows["value_new"].sum()
            result["old/new total"] = (
                round(rows["value_old"].sum() / new_total, 3) if new_total else None
            )
        else:
            result["mean diff F (old-new)"] = round(diff.mean() * TENTHS_C_TO_F_DIFF, 2)
            result["days differing"] = int((diff != 0).sum())
        results.append(result)
    return results


# ---------------------------------------------------------------------------
# Safety checks
# ---------------------------------------------------------------------------


def check_ids(spark, current_id: str, old_ids: list[str]) -> None:
    """The current ID must exist; old IDs can't be it, repeat, or be active stations."""
    if current_id in old_ids or len(set(old_ids)) != len(old_ids):
        raise ValueError("History IDs must be distinct and differ from --station-id.")

    ids = [current_id] + old_ids
    rows = spark.sql(
        f"""
            SELECT station_id, selected FROM {STATION_MERGED_TABLE}
            WHERE station_id IN ({", ".join(f":id{i}" for i in range(len(ids)))})
        """,
        args={f"id{i}": v for i, v in enumerate(ids)},
    ).collect()
    found = {r["station_id"]: r["selected"] for r in rows}

    if current_id not in found:
        raise ValueError(
            f'"{current_id}" is not in {STATION_MERGED_TABLE}. Refresh the station '
            "list (wx_station_ingest) if NOAA renamed the station."
        )
    active_old = [i for i in old_ids if found.get(i)]
    if active_old:
        raise ValueError(
            f"History IDs are active stations: {active_old}. Deactivate them first."
        )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Load old GHCN IDs' history into raw.obs_ghcn under the current ID."
    )
    parser.add_argument(
        "--station-id", required=True, help="Current GHCN ID, e.g. CAN07025251."
    )
    parser.add_argument(
        "--history-ids",
        required=True,
        help="Old GHCN IDs, most recent first, separated by commas, e.g. "
        '"CAN07025250,CA007025250". Spaces after (or instead of) the commas are fine.',
    )
    parser.add_argument(
        "--dry-run",
        nargs="?",
        const="true",
        default="false",
        help="Report what would be loaded; write nothing. Bare --dry-run or "
        "--dry-run true; the job passes its dry_run parameter (true/false).",
    )
    args, _ = parser.parse_known_args()

    dry_run_value = args.dry_run.strip().lower()
    if dry_run_value not in ("true", "false"):
        parser.error(f'--dry-run must be true or false, got "{args.dry_run}".')
    dry_run = dry_run_value == "true"

    current_id = args.station_id.strip()
    old_ids = [i for i in re.split(r"[,\s]+", args.history_ids) if i]
    # The job passes "" for a parameter left blank
    if not current_id or not old_ids:
        parser.error("--station-id and --history-ids must not be blank.")

    spark = get_spark()
    session = get_session()
    check_ids(spark, current_id, old_ids)

    print(f"Downloading {current_id}.dly (current)...")
    current_df = parse_dly(current_id, download_dly(session, current_id))
    print(f"{current_id}: {len(current_df):,} rows")

    history = []
    for old_id in old_ids:
        print(f"Downloading {old_id}.dly (history)...")
        old_df = parse_dly(old_id, download_dly(session, old_id))
        print(f"{old_id}: {len(old_df):,} rows")
        history.append((old_id, old_df))

    print("\nOverlap check (days where both IDs have a value):")
    overlaps = [r for old_id, old_df in history for r in overlap_check(current_df, old_id, old_df)]
    for result in overlaps:
        print(f"  {result}")
    if not overlaps:
        print("  (none)")

    loaded, summary = select_history(current_df, history)
    print(f"\nRows to load under {current_id}: {len(loaded):,}")
    print(pd.DataFrame(summary).to_string(index=False) if summary else "  (none)")

    if dry_run:
        print("\nDry run: nothing written.")
        return
    if loaded.empty:
        print("\nNothing to load.")
        return

    merge_into_target(
        spark, loaded, OBS_GHCN_TABLE, TARGET_SCHEMA, keep_existing_values=True
    )
    print(f"\nMerged {len(loaded):,} rows into {OBS_GHCN_TABLE} under {current_id}.")
    print(
        "Next: run wx_activate_station for this station, then record the load in docs/station_lineage.md."
    )


if __name__ == "__main__":
    main()
