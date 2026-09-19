"""HokieDay — unified campus-life agent for Virginia Tech.

Core package is pure-stdlib so it runs identically:
  * locally (tests against cached fixtures, DEMO_MODE=cache)
  * in a Databricks notebook (thin wrapper writes Delta tables)
  * inside a Databricks App

Nothing in here may import pyspark. Notebooks adapt; the library stays portable.
"""

__version__ = "0.1.0"