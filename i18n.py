#!/usr/bin/env python3
"""Message catalogs. Strings live in locales/*.json; code only formats keys."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

LANGS = ("zh", "en")
_CACHE: dict[str, dict[str, str]] = {}


def lang() -> str:
    code = (os.environ.get("UI_LANG") or "zh").strip().lower()
    return code if code in LANGS else "zh"


def _load(code: str) -> dict[str, str]:
    if code not in _CACHE:
        path = Path(__file__).resolve().parent / "locales" / f"{code}.json"
        try:
            _CACHE[code] = {str(k): str(v) for k, v in json.loads(path.read_text(encoding="utf-8")).items()}
        except (OSError, ValueError, AttributeError):
            _CACHE[code] = {}
    return _CACHE[code]


class _Safe(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def t(key: str, **kwargs: Any) -> str:
    text = _load(lang()).get(key) or _load("zh").get(key) or key
    return text.format_map(_Safe(kwargs)) if kwargs else text
