"""
Shared Spark session helper.

These scripts are designed to run as Databricks Job "Python script" tasks on
a job cluster (or serverless), not as notebooks. In that context, calling
SparkSession.builder.getOrCreate() attaches to the Spark session Databricks
already initializes in the runtime, the same way the implicit `spark` global
works in a notebook cell.

This will not work for running these scripts locally unless Databricks Connect
is set up, since there's no local Spark cluster to attach to. Given that the
ingest jobs run inside Databricks only, this should be fine for now.
"""

from pyspark.sql import SparkSession


def get_spark() -> SparkSession:
    """Return the active SparkSession, creating one if none exists."""
    return SparkSession.builder.getOrCreate()
