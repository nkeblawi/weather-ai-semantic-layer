"""
Shared station-selection query, used by all observation ingest scripts.
"""

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
