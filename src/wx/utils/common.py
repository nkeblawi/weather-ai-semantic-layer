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
    """station_id values from station_wmo where selected = true."""
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
    keep_existing_values: bool = False,
) -> None:
    """
    Creates target_table from a pandas df, or upserts into it on merge_keys.
    keep_existing_values: a NULL incoming value doesn't overwrite an existing
    one, so a missing GHCN day can't erase a value loaded from an older
    station id (side effect: a value NOAA later blanks stays).
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
        .whenMatchedUpdateAll(
            condition="s.value IS NOT NULL OR t.value IS NULL"
            if keep_existing_values
            else None
        )
        .whenNotMatchedInsertAll()
        .execute()
    )


def upsert_dataframe(
    spark,
    df: DataFrame,
    target_table: str,
    merge_keys: tuple[str, ...] = ("station_id", "obs_date", "variable"),
) -> None:
    """merge_into_target for a Spark DataFrame."""
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

# Passes ghcn_last_date between a job run's tasks (task values), saving each a
# full-table scan. Best-effort: get_cutoff() returns None when unavailable and
# the caller computes it; set_cutoff() never raises.

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
    """The cutoff upstream_task_key published this run, or None."""
    try:
        # "" when unset: get() rejects a None default
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
