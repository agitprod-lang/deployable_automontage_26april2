from __future__ import annotations

import importlib.util
from functools import lru_cache
from pathlib import Path


LEGACY_STEP2_PATH = Path(__file__).resolve().parents[3] / "program" / "post_comparser_key_identifyer.py"


@lru_cache(maxsize=1)
def get_legacy_step2_module():
    spec = importlib.util.spec_from_file_location("post_comparser_key_identifyer", LEGACY_STEP2_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load legacy step-2 module from {LEGACY_STEP2_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
