# Merge cleaned GHCN and ACIS observations into a unified table (incremental).

"""
Converted from the OBS_03_Merge notebook.

Incremental processing: only new ACIS records, new GHCN records, and GHCN
records that would replace an existing ACIS record are read/processed on
each run -- no full table scans of the target.

Deduplication, when the same (station_id, obs_date, variable) exists in
both sources:
  1. Find the last obs_date where GHCN has non-NULL data (the "GHCN cutoff").
  2. For obs_date AFTER the cutoff -> always use ACIS (even if NULL), since
     ACIS runs 1-2 days ahead of GHCN. GHCN NULLs are excluded past the
     cutoff so they don't shadow ACIS.
  3. For obs_date UP TO the cutoff -> GHCN with a non-NULL value wins
     (authoritative), ACIS with a non-NULL value fills gaps, NULLs are
     lowest priority.

ACIS special values: "T" (trace) -> 0.001 to preserve the fact a trace
amount was observed; "M" (missing) -> NULL.

Sources: weather.cleaned.obs_ghcn_conv, weather.cleaned.obs_acis_conv
Target:  weather.cleaned.obs_merged
"""

import os
import sys
import argparse

parser = argparse.ArgumentParser()
parser.add_argument("--workspace-root", default=os.environ.get("WX_WORKSPACE_ROOT"))
args, _ = parser.parse_known_args()
if not args.workspace_root:
    parser.error("--workspace-root is required (or set WX_WORKSPACE_ROOT for manual/notebook testing)")
sys.path.append(args.workspace_root)

from wx.utils.bootstrap import bootstrap  # noqa: E402
bootstrap()

from pyspark.sql import DataFrame  # type: ignore
from pyspark.sql import functions as F  # type: ignore
from pyspark.sql.window import Window  # type: ignore
from wx.utils.config import OBS_ACIS_CONV_TABLE, OBS_GHCN_CONV_TABLE, OBS_MERGED_TABLE
from wx.utils.spark_utils import get_spark

# ---------------------------------------------------------------------------
# Step 1: Read source tables
# ---------------------------------------------------------------------------


def read_sources(spark) -> tuple[DataFrame, DataFrame]:
    """
    Read cleaned GHCN and ACIS tables, tagging each with its source and
    normalizing ACIS's string 'value' column ("T"/"M"/numeric) to double
    with matching flag columns so both sources share a schema.
    """
    ghcn_df = spark.table(OBS_GHCN_CONV_TABLE).withColumn("source", F.lit("GHCN"))

    acis = spark.table(OBS_ACIS_CONV_TABLE)
    acis_df = (
        acis.withColumn(
            "value",
            F.when(F.col("value") == "T", 0.001)  # Trace amount
            .when(F.col("value") == "M", None)  # Missing
            .otherwise(F.col("value").cast("double")),
        )
        .withColumn("source", F.lit("ACIS"))
        .withColumn("mflag", F.lit(None).cast("string"))
        .withColumn("qflag", F.lit(None).cast("string"))
        .withColumn("sflag", F.lit(None).cast("string"))
    )

    return ghcn_df, acis_df


# ---------------------------------------------------------------------------
# Step 2: Filter down to new/updated records only (incremental)
# ---------------------------------------------------------------------------


def build_incremental_source(
    spark, ghcn_df: DataFrame, acis_df: DataFrame
) -> DataFrame:
    """
    Return only the rows that are new or need to replace an existing row
    in the merged table: new ACIS rows, new GHCN rows, and GHCN rows that
    would replace an ACIS row already in the merged table. On first run
    (no merged table yet), returns everything.
    """
    if not spark.catalog.tableExists(OBS_MERGED_TABLE):
        print("Merged table doesn't exist yet - processing all records (first run)")
        return ghcn_df.unionByName(acis_df, allowMissingColumns=True)

    existing_df = spark.table(OBS_MERGED_TABLE)
    print(f"Existing merged records: {existing_df.count():,}")

    new_acis_df = acis_df.join(
        existing_df.select("station_id", "obs_date", "variable").distinct(),
        on=["station_id", "obs_date", "variable"],
        how="left_anti",
    )
    print(f"New ACIS records to add: {new_acis_df.count():,}")

    acis_keys_in_merged = (
        existing_df.filter(F.col("source") == "ACIS")
        .select("station_id", "obs_date", "variable")
        .distinct()
    )

    new_ghcn_df = ghcn_df.join(
        existing_df.filter(F.col("source") == "GHCN")
        .select("station_id", "obs_date", "variable")
        .distinct(),
        on=["station_id", "obs_date", "variable"],
        how="left_anti",
    )

    ghcn_to_replace_acis = ghcn_df.join(
        acis_keys_in_merged,
        on=["station_id", "obs_date", "variable"],
        how="inner",
    )

    ghcn_to_process = new_ghcn_df.unionByName(ghcn_to_replace_acis).distinct()

    print(f"New GHCN records: {new_ghcn_df.count():,}")
    print(f"GHCN records replacing ACIS: {ghcn_to_replace_acis.count():,}")
    print(f"Total GHCN records to process: {ghcn_to_process.count():,}")

    return ghcn_to_process.unionByName(new_acis_df, allowMissingColumns=True)


# ---------------------------------------------------------------------------
# Step 3: Prioritize and deduplicate GHCN vs ACIS
# ---------------------------------------------------------------------------


def dedupe_with_priority(ghcn_df: DataFrame, unioned_df: DataFrame) -> DataFrame:
    """
    Collapse unioned_df down to one row per (station_id, obs_date,
    variable), preferring GHCN once it has non-NULL data for a date, and
    ACIS for any date after GHCN's most recent non-NULL date (since ACIS
    runs ahead of GHCN).
    """
    ghcn_last_date = (
        ghcn_df.filter(F.col("value").isNotNull())
        .agg(F.max("obs_date").alias("last_date"))
        .collect()[0]["last_date"]
    )
    print(f"GHCN last date with non-NULL data: {ghcn_last_date}")

    prioritized_df = unioned_df.withColumn(
        "priority",
        F.when(
            (F.col("source") == "ACIS") & (F.col("obs_date") > ghcn_last_date),
            1,
        )
        .when((F.col("source") == "GHCN") & F.col("value").isNotNull(), 2)
        .when((F.col("source") == "ACIS") & F.col("value").isNotNull(), 3)
        .otherwise(4),
    )

    window_spec = Window.partitionBy("station_id", "obs_date", "variable").orderBy(
        F.col("priority").asc(),
        F.col("ingested_at").desc(),
    )

    return (
        prioritized_df.withColumn("row_num", F.row_number().over(window_spec))
        .filter(F.col("row_num") == 1)
        .drop("row_num", "priority")
        .filter(
            ~(
                (F.col("source") == "GHCN")
                & (F.col("obs_date") > ghcn_last_date)
                & F.col("value").isNull()
            )
        )
    )


# ---------------------------------------------------------------------------
# Step 4: MERGE UPSERT into target (GHCN replaces ACIS on match)
# ---------------------------------------------------------------------------


def merge_into_obs_merged(spark, merged_df: DataFrame) -> None:
    merged_df.createOrReplaceTempView("merge_source")

    spark.sql(f"""
        MERGE INTO {OBS_MERGED_TABLE} AS target
        USING merge_source AS source
        ON target.station_id = source.station_id
           AND target.obs_date = source.obs_date
           AND target.variable = source.variable
        WHEN MATCHED AND source.source = 'GHCN' AND target.source = 'ACIS' THEN
            UPDATE SET
                target.value = source.value,
                target.mflag = source.mflag,
                target.qflag = source.qflag,
                target.sflag = source.sflag,
                target.ingested_at = source.ingested_at,
                target.source = source.source
        WHEN NOT MATCHED THEN
            INSERT (
                station_id, obs_date, variable, value,
                mflag, qflag, sflag, ingested_at, source
            )
            VALUES (
                source.station_id, source.obs_date, source.variable, source.value,
                source.mflag, source.qflag, source.sflag, source.ingested_at, source.source
            )
    """)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def merge_obs(spark) -> None:
    ghcn_df, acis_df = read_sources(spark)
    print(f"GHCN records: {ghcn_df.count():,}")
    print(f"ACIS records: {acis_df.count():,}")

    unioned_df = build_incremental_source(spark, ghcn_df, acis_df)
    print(f"Total records to process: {unioned_df.count():,}")

    if unioned_df.count() == 0:
        print("No new records to merge.")
        return

    if not spark.catalog.tableExists(OBS_MERGED_TABLE):
        merged_df = dedupe_with_priority(ghcn_df, unioned_df)
        merged_df.write.format("delta").saveAsTable(OBS_MERGED_TABLE)
        print(f"Created {OBS_MERGED_TABLE} with {merged_df.count():,} records")
        return

    merged_df = dedupe_with_priority(ghcn_df, unioned_df)
    print(f"Merged records (after deduplication): {merged_df.count():,}")

    merge_into_obs_merged(spark, merged_df)

    print(f"MERGE complete for {OBS_MERGED_TABLE}")
    print(f"Total records in table: {spark.table(OBS_MERGED_TABLE).count():,}")


def main():
    spark = get_spark()
    merge_obs(spark)


if __name__ == "__main__":
    main()
