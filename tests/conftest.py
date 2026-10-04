"""Shared test setup.

Run artifacts default to ./runs in the working directory. Point them at a
throwaway directory for the whole test session so running the suite never
writes into the repository.
"""
import os
import tempfile

os.environ.setdefault("HARNESS_RUNS_DIR", tempfile.mkdtemp(prefix="harness-test-runs-"))
