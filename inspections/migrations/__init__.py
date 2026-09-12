"""Migrations.

`_0013` is an alias so the retune migration's behaviour can be tested — a module
whose name starts with a digit cannot be imported with a plain import statement.
"""

import importlib

_0013 = importlib.import_module("inspections.migrations.0013_retune_summary_prompt")
