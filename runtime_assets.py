"""Public release resources must be readable by the unprivileged service."""
import json
from pathlib import Path

PUBLIC_TREES = ("locales", "systemd", "data")
UI_KEYS = ("btn.lang_zh", "btn.lang_en", "btn.overview", "btn.refresh", "fleet.title", "query.fail")


def normalize_permissions(release):
    """Only packaged public assets; credentials and service state live elsewhere."""
    release = Path(release)
    for name in PUBLIC_TREES:
        directory = release / name
        for path in [directory] + sorted(directory.rglob("*")):
            if path.is_symlink():
                raise ValueError("release resources must not be symlinks")
            if path.is_dir():
                path.chmod(0o755)
            elif path.is_file():
                path.chmod(0o644)
            else:
                raise ValueError("missing or unsupported release resource")


def verify_readable(release):
    """Run as the service user so root-readable catalogs cannot pass readiness."""
    release = Path(release)
    for code in ("zh", "en"):
        catalog = json.loads((release / "locales" / (code + ".json")).read_text(encoding="utf-8"))
        if not isinstance(catalog, dict) or any(not isinstance(catalog.get(key), str) or not catalog[key] for key in UI_KEYS):
            raise ValueError("incomplete language catalog")
        if catalog["btn.lang_zh"] != "中文" or catalog["btn.lang_en"] != "English":
            raise ValueError("invalid language switch labels")
    rules = json.loads((release / "data/ip-rules.json").read_text(encoding="utf-8"))
    if not isinstance(rules, dict) or rules.get("schema_version") != 1:
        raise ValueError("invalid bundled IP rules")
    return True
