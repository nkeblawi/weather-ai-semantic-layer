"""
Shared helpers for the observation scripts: station selection, Delta
merge/upsert, and passing the GHCN cutoff date between job tasks.
"""

from datetime import date

import pandas as pd
from delta.tables import DeltaTable  # type: ignore
from pyspark.sql import DataFrame  # type: ignore
from pyspark.sql import functions as F  # type: ignore
from pyspark.sql.types import StructType  # type: ignore
from wx.utils.config import STATION_MERGED_TABLE


def get_selected_stations(spark) -> list[str]:
    """
    Return station_id values from station_wmo where selected = true.
    """
    stations = (
        spark.table(STATION_MERGED_TABLE)
        .filter(F.col("selected") == True)
        .select("station_id")
        .collect()
    )

    station_ids = [s["station_id"] for s in stations]
    if not station_ids:
        raise ValueError("No stations selected")

    return station_ids


def merge_into_target(
    spark,
    df: pd.DataFrame,
    target_table: str,
    target_schema: StructType,
    merge_keys: tuple[str, ...] = ("station_id", "obs_date", "variable"),
) -> None:
    """
    Create target_table from df if it doesn't exist yet, otherwise merge
    (upsert) df into it, matching on merge_keys. Both existing ingest
    sources (GHCN and ACIS) merge on the same three-column key
    (station_id, obs_date, variable), so that's the default, but it's
    overridable in case a future source needs a different key.
    """
    if df.empty:
        print("Nothing to merge, DataFrame is empty.")
        return

    new_df = spark.createDataFrame(df, schema=target_schema)

    if not spark.catalog.tableExists(target_table):
        print(f"{target_table} does not exist yet, creating it from this load.")
        new_df.write.format("delta").saveAsTable(target_table)
        return

    target = DeltaTable.forName(spark, target_table)
    merge_condition = " AND ".join(f"t.{key} = s.{key}" for key in merge_keys)

    (
        target.alias("t")
        .merge(new_df.alias("s"), merge_condition)
        .whenMatchedUpdateAll()
        .whenNotMatchedInsertAll()
        .execute()
    )


def upsert_dataframe(
    spark,
    df: DataFrame,
    target_table: str,
    merge_keys: tuple[str, ...] = ("station_id", "obs_date", "variable"),
) -> None:
    """
    Create target_table from df if it doesn't exist yet, otherwise merge
    (upsert) df into it, matching on merge_keys. Spark-DataFrame
    counterpart to merge_into_target(), for transform steps (map/convert)
    whose input is already a Spark DataFrame rather than a pandas
    DataFrame from an HTTP fetch.
    """
    if not spark.catalog.tableExists(target_table):
        print(f"{target_table} does not exist yet, creating it from this load.")
        df.write.format("delta").saveAsTable(target_table)
        return

    target = DeltaTable.forName(spark, target_table)
    merge_condition = " AND ".join(f"t.{key} = s.{key}" for key in merge_keys)

    (
        target.alias("t")
        .merge(df.alias("s"), merge_condition)
        .whenMatchedUpdateAll()
        .whenNotMatchedInsertAll()
        .execute()
    )


# ---------------------------------------------------------------------------
# GHCN cutoff date shared between job tasks
# ---------------------------------------------------------------------------

# Passes ghcn_last_date between tasks of one job run (Databricks job task
# values), so merge_obs, pivot_obs and derived_vars don't each recompute it
# with a full-table scan. Best-effort: outside a job run, in a job whose
# upstream task key differs (e.g. wx_activate_station's backfill_* tasks),
# or without dbutils, get_cutoff() returns None and the caller computes the
# cutoff itself (the pre-existing behavior). set_cutoff() never raises.

GHCN_LAST_DATE_KEY = "ghcn_last_date"


def _task_values():
    from databricks.sdk.runtime import dbutils  # type: ignore

    return dbutils.jobs.taskValues


def set_cutoff(cutoff: date | None) -> None:
    """Publish cutoff for downstream tasks of this job run. No-op on any failure."""
    if cutoff is None:
        return
    try:
        _task_values().set(key=GHCN_LAST_DATE_KEY, value=cutoff.isoformat())
    except Exception as e:
        print(f"Task value not set ({type(e).__name__}: {e}); downstream will recompute.")


def get_cutoff(upstream_task_key: str) -> date | None:
    """
    Cutoff published by upstream_task_key in this job run, or None if it's
    unavailable for any reason (caller then computes it).
    """
    try:
        # "" (not None, which get() rejects) when the key wasn't set this run
        # or outside a job run.
        value = _task_values().get(
            taskKey=upstream_task_key,
            key=GHCN_LAST_DATE_KEY,
            default="",
            debugValue="",
        )
    except Exception as e:
        print(f"Task value unavailable ({type(e).__name__}: {e}); computing cutoff.")
        return None

    if not value:
        print(f"No {GHCN_LAST_DATE_KEY} from task '{upstream_task_key}'; computing cutoff.")
        return None
    print(f"Using {GHCN_LAST_DATE_KEY}={value} from task '{upstream_task_key}'.")
    return date.fromisoformat(value)
