# Merge cleaned GHCN and ACIS observations into a unified table (incremental).

"""
Merges cleaned GHCN and ACIS rows into cleaned.obs_merged.

Incremental, with a watermark per source (its max ingested_at in
obs_merged): one shared watermark skipped ACIS rows when a GHCN backfill ran
in between. Re-reading an already merged row is harmless.

Deduplication, when the same (station_id, obs_date, variable) exists in
both sources:
  1. Find the last obs_date where GHCN has non-NULL data, held back
     CUTOFF_LOOKBACK_DAYS (the "GHCN cutoff").
  2. For obs_date AFTER the cutoff -> always use ACIS (even if NULL), since
     ACIS runs 1-2 days ahead of GHCN. GHCN NULLs are excluded past the
     cutoff so they don't shadow ACIS.
  3. For obs_date UP TO the cutoff -> GHCN with a non-NULL value wins
     (authoritative), ACIS with a non-NULL value fills gaps, NULLs are
     lowest priority.

ACIS "T" (trace) -> 0.001, so a trace still counts; "M" (missing) -> NULL.
"""

import os
import sys
import argparse
from datetime import timedelta

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

from pyspark.sql import DataFrame  # type: ignore
from pyspark.sql import functions as F  # type: ignore
from pyspark.sql.window import Window  # type: ignore
from wx.utils.config import (
    CUTOFF_LOOKBACK_DAYS,
    OBS_ACIS_CONV_TABLE,
    OBS_GHCN_CONV_TABLE,
    OBS_MERGED_TABLE,
)
from wx.utils.spark_utils import get_spark
from wx.utils.common import set_cutoff

# ---------------------------------------------------------------------------
# Step 1: Read source tables
# ---------------------------------------------------------------------------


def read_sources(spark, watermarks: dict) -> tuple[DataFrame, DataFrame]:
    """
    Both cleaned tables in one schema, tagged by source (ACIS values become
    doubles), each read from its own watermark (in full when it has none).
    """
    ghcn_df = spark.table(OBS_GHCN_CONV_TABLE)
    acis = spark.table(OBS_ACIS_CONV_TABLE)

    if watermarks.get("GHCN") is not None:
        ghcn_df = ghcn_df.filter(F.col("ingested_at") > F.lit(watermarks["GHCN"]))
    if watermarks.get("ACIS") is not None:
        acis = acis.filter(F.col("ingested_at") > F.lit(watermarks["ACIS"]))

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


def merged_watermarks(spark) -> dict:
    """{"GHCN": ts, "ACIS": ts}: obs_merged's max ingested_at per source; empty on the first run."""
    if not spark.catalog.tableExists(OBS_MERGED_TABLE):
        return {}
    rows = (
        spark.table(OBS_MERGED_TABLE)
        .groupBy("source")
        .agg(F.max("ingested_at").alias("watermark"))
        .collect()
    )
    return {r["source"]: r["watermark"] for r in rows}


def ghcn_cutoff_date(spark):
    """GHCN's last non-NULL obs_date minus CUTOFF_LOOKBACK_DAYS: where ACIS takes over."""
    last_date = (
        spark.table(OBS_GHCN_CONV_TABLE)
        .filter(F.col("value").isNotNull())
        .agg(F.max("obs_date").alias("last_date"))
        .collect()[0]["last_date"]
    )
    return last_date - timedelta(days=CUTOFF_LOOKBACK_DAYS) if last_date else None


# ---------------------------------------------------------------------------
# Step 3: Prioritize and deduplicate GHCN vs ACIS
# ---------------------------------------------------------------------------


def dedupe_with_priority(ghcn_last_date, unioned_df: DataFrame) -> DataFrame:
    """One row per (station_id, obs_date, variable), by the precedence rules above."""
    print(f"GHCN cutoff ({CUTOFF_LOOKBACK_DAYS} days before its last non-NULL date): {ghcn_last_date}")

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
# Step 4: MERGE UPSERT into target (GHCN replaces ACIS; late values fill empty rows)
# ---------------------------------------------------------------------------


def merge_into_obs_merged(spark, merged_df: DataFrame) -> None:
    merged_df.createOrReplaceTempView("merge_source")

    spark.sql(f"""
        MERGE INTO {OBS_MERGED_TABLE} AS target
        USING merge_source AS source
        ON target.station_id = source.station_id
           AND target.obs_date = source.obs_date
           AND target.variable = source.variable
        WHEN MATCHED AND (
            -- GHCN replaces ACIS, unless it has nothing to offer (a QC-flagged
            -- NULL still counts, so a bad value doesn't survive through ACIS)
            (source.source = 'GHCN' AND target.source = 'ACIS'
                AND (source.value IS NOT NULL OR source.qflag IS NOT NULL))
            -- a value that arrives late fills an empty row (not one QC blanked)
            OR (target.value IS NULL AND target.qflag IS NULL AND source.value IS NOT NULL)
        ) THEN
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

    watermarks = {} if first_run else merged_watermarks(spark)
    print(f"Watermarks: {watermarks}")
    ghcn_df, acis_df = read_sources(spark, watermarks)
    unioned_df = ghcn_df.unionByName(acis_df, allowMissingColumns=True)
    total = unioned_df.count()
    print(f"Total records to process: {total:,}")

    if total == 0:
        print("No new records to merge.")
        return

    # Published for pivot_obs/derived_vars in this job run, saving their rescans
    cutoff = ghcn_cutoff_date(spark)
    set_cutoff(cutoff)

    merged_df = dedupe_with_priority(cutoff, unioned_df)

    if first_run:
        merged_df.write.format("delta").saveAsTable(OBS_MERGED_TABLE)
        print(f"Created {OBS_MERGED_TABLE}")
        return

    merge_into_obs_merged(spark, merged_df)
    print(f"MERGE complete for {OBS_MERGED_TABLE}")


def main():
    spark = get_spark()
    merge_obs(spark)


if __name__ == "__main__":
    main()
