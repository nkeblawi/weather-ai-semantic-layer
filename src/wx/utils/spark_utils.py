"""The Databricks job's Spark session (locally, this needs Databricks Connect)."""

from pyspark.sql import SparkSession


def get_spark() -> SparkSession:
    """Return the active SparkSession, creating one if none exists."""
    return SparkSession.builder.getOrCreate()
