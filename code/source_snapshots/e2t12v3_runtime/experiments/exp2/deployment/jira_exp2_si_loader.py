"""Airflow loader for the isolated Experiment 2 candidate package."""

from __future__ import annotations

import os
import sys


runtime_parent = os.environ.get("EXP2_SI_RUNTIME_PARENT", "/opt/airflow3/scripts")
if runtime_parent not in sys.path:
    sys.path.insert(0, runtime_parent)

from exp2_si_runtime.dags.jira_exp2_si_runner import dag  # noqa: E402,F401
