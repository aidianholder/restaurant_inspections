"""Migrations.

`_latest_prompt` is an alias for the most recent prompt-retune migration, so its
behaviour can be tested — a module whose name starts with a digit cannot be
imported with a plain import statement.
"""

import importlib

_latest_prompt = importlib.import_module("inspections.migrations.0014_markdown_summary_prompt")
