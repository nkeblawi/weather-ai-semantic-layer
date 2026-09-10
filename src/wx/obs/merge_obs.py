# Merge cleaned GHCN and ACIS observations into a unified table (incremental).

"""
Converted from the OBS_03_Merge notebook.

Incremental: Only new ACIS records, new GHCN records, and GHCN
records that would replace an existing ACIS record are read/processed on
each run. Prevents wasted compute with full table scans.

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
    parser.error(
        "--workspace-root is required (or set WX_WORKSPACE_ROOT for manual/notebook testing)"
    )
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


def read_sources(spark, watermark) -> tuple[DataFrame, DataFrame]:
    """
    Read cleaned GHCN and ACIS tables, tagging each with its source and
    normalizing ACIS's string 'value' column ("T"/"M"/numeric) to double
    with matching flag columns so both sources share a schema. When
    watermark is set, only rows ingested after it are read; None reads
    both tables in full (first run).
    """
    ghcn_df = spark.table(OBS_GHCN_CONV_TABLE)
    acis = spark.table(OBS_ACIS_CONV_TABLE)

    if watermark is not None:
        ghcn_df = ghcn_df.filter(F.col("ingested_at") > F.lit(watermark))
        acis = acis.filter(F.col("ingested_at") > F.lit(watermark))

    ghcn_df = ghcn_df.withColumn("source", F.lit("GHCN"))
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
# Step 2: Watermark the source reads (incremental)
# ---------------------------------------------------------------------------


def merged_watermark(spark):
    """
    obs_merged's current max ingested_at, source rows newer than this are
    what a run needs to process. None when obs_merged doesn't exist yet
    (first run).
    """
    if not spark.catalog.tableExists(OBS_MERGED_TABLE):
        return None
    return spark.table(OBS_MERGED_TABLE).agg(F.max("ingested_at")).collect()[0][0]


def ghcn_cutoff_date(spark):
    """
    Last obs_date GHCN has non-NULL data for, across all of obs_ghcn_conv.
    Drives GHCN-vs-ACIS precedence in dedupe_with_priority.
    """
    return (
        spark.table(OBS_GHCN_CONV_TABLE)
        .filter(F.col("value").isNotNull())
        .agg(F.max("obs_date").alias("last_date"))
        .collect()[0]["last_date"]
    )


# ---------------------------------------------------------------------------
# Step 3: Prioritize and deduplicate GHCN vs ACIS
# ---------------------------------------------------------------------------


def dedupe_with_priority(ghcn_last_date, unioned_df: DataFrame) -> DataFrame:
    """
    Collapse unioned_df down to one row per (station_id, obs_date,
    variable), preferring GHCN once it has non-NULL data for a date, and
    ACIS for any date after GHCN's most recent non-NULL date (since ACIS
    runs ahead of GHCN).
    """
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
    first_run = not spark.catalog.tableExists(OBS_MERGED_TABLE)

    ghcn_df, acis_df = read_sources(
        spark, None if first_run else merged_watermark(spark)
    )
    unioned_df = ghcn_df.unionByName(acis_df, allowMissingColumns=True)
    print(f"Total records to process: {unioned_df.count():,}")

    if unioned_df.count() == 0:
        print("No new records to merge.")
        return

    merged_df = dedupe_with_priority(ghcn_cutoff_date(spark), unioned_df)
    print(f"Merged records (after deduplication): {merged_df.count():,}")

    if first_run:
        merged_df.write.format("delta").saveAsTable(OBS_MERGED_TABLE)
        print(f"Created {OBS_MERGED_TABLE} with {merged_df.count():,} records")
        return

    merge_into_obs_merged(spark, merged_df)
    print(f"MERGE complete for {OBS_MERGED_TABLE}")


def main():
    spark = get_spark()
    merge_obs(spark)


if __name__ == "__main__":
    main()
