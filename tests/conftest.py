"""Pytest configuration (path setup is handled by pyproject.toml pythonpath)."""
import os

# Batch 11: never launch MuseScore from the test suite (tests mock it explicitly).
os.environ["SATB_MSCZ"] = "0"
