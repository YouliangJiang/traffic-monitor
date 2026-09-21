#!/usr/bin/env python3
"""UI language catalogs. Strings live in locales/*.json; code only formats keys."""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

LANGS = ("zh", "en")
_LOCK = threading.Lock()
_CACHE: dict[str, dict[str, str]] = {}
_MTIME: dict[str, float] = {}


def _root() -> Path:
    return Path(__file__).resolve().parent


def _ui_path() -> Path:
    import util

    return util.state_dir() / "ui.json"


def env_lang() -> str:
    raw = (os.environ.get("UI_LANG") or "zh").strip().lower()
    aliases = {
        "cn": "zh",
        "zh-cn": "zh",
        "zh_cn": "zh",
        "chinese": "zh",
        "en-us": "en",
        "en_us": "en",
        "english": "en",
    }
    raw = aliases.get(raw, raw)
    return raw if raw in LANGS else "zh"


def lang() -> str:
    try:
        import util

        saved = str(util.load_json(_ui_path()).get("lang") or "").strip().lower()
        if saved in LANGS:
            return saved
    except Exception:
        pass
    return env_lang()


def set_lang(code: str) -> str:
    aliases = {
        "cn": "zh",
        "zh-cn": "zh",
        "zh_cn": "zh",
        "chinese": "zh",
        "en-us": "en",
        "en_us": "en",
        "english": "en",
    }
    code = aliases.get(str(code or "").strip().lower(), str(code or "").strip().lower())
    if code not in LANGS:
        code = "zh"
    import util

    path = _ui_path()
    data = util.load_json(path)
    data["lang"] = code
    util.save_json(path, data)
    return code


def _load(code: str) -> dict[str, str]:
    path = _root() / "locales" / f"{code}.json"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    with _LOCK:
        if code in _CACHE and _MTIME.get(code) == mtime:
            return _CACHE[code]
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        flat = {str(k): str(v) for k, v in raw.items()}
        _CACHE[code] = flat
        _MTIME[code] = mtime
        return flat


class _Safe(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def catalog(code: str) -> dict[str, str]:
    code = code if code in LANGS else "zh"
    return _load(code)


def t_lang(code: str, key: str, **kwargs: Any) -> str:
    cat = _load(code if code in LANGS else "zh")
    text = cat.get(key)
    if text is None:
        text = _load("zh").get(key) or _load("en").get(key) or key
    if kwargs:
        try:
            text = text.format_map(_Safe(kwargs))
        except Exception:
            pass
    return text


def t(key: str, **kwargs: Any) -> str:
    cat = _load(lang())
    text = cat.get(key)
    if text is None:
        text = _load("zh").get(key) or _load("en").get(key) or key
    if kwargs:
        try:
            text = text.format_map(_Safe(kwargs))
        except Exception:
            pass
    return text
