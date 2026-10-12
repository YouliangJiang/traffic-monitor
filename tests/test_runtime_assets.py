import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import bot
import i18n
import runtime_assets


class RuntimeAssetsTests(unittest.TestCase):
    def test_restrictive_packaging_modes_are_repaired_for_service_readers(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            for name in runtime_assets.PUBLIC_TREES:
                shutil.copytree(Path(runtime_assets.__file__).parent / name, root / name)
            for name in runtime_assets.PUBLIC_TREES:
                for p in [root / name] + list((root / name).rglob("*")):
                    p.chmod(0o700 if p.is_dir() else 0o600)
            credential = root / "private.env"
            credential.write_text("fixture=private\n")
            credential.chmod(0o600)
            runtime_assets.normalize_permissions(root)
            self.assertTrue(runtime_assets.verify_readable(root))
            for name in runtime_assets.PUBLIC_TREES:
                for p in [root / name] + list((root / name).rglob("*")):
                    self.assertEqual(p.stat().st_mode & 0o777, 0o755 if p.is_dir() else 0o644)
            self.assertEqual(credential.stat().st_mode & 0o777, 0o600)

    def test_missing_catalog_keys_block_release_readiness(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            shutil.copytree(Path(runtime_assets.__file__).parent / "locales", root / "locales")
            shutil.copytree(Path(runtime_assets.__file__).parent / "data", root / "data")
            p = root / "locales/zh.json"
            value = json.loads(p.read_text())
            value.pop("btn.lang_zh")
            p.write_text(json.dumps(value))
            with self.assertRaises(ValueError):
                runtime_assets.verify_readable(root)

    def test_language_callbacks_persist_and_render_both_choices_after_reload(self):
        with tempfile.TemporaryDirectory() as scratch, patch.dict(os.environ, {
            "TRAFFIC_MONITOR_STATE_DIR": scratch,
            "UI_LANG": "en", "TELEGRAM_BOT_TOKEN": "not-transmitted",
        }), patch.object(bot, "register_bot"), patch.object(bot, "nodes", return_value=[]):
            for code, title in [("zh", "机群总览"), ("en", "Fleet overview")]:
                reply = bot.handle_callback("lang:" + code)
                self.assertIn(title, reply.text)
                labels = [button["text"] for row in reply.markup["inline_keyboard"] for button in row]
                self.assertIn("中文", labels)
                self.assertIn("English", labels)
                self.assertFalse(any(label.startswith("btn.") for label in labels))
                i18n._CACHE.clear()
                i18n._MTIME.clear()
                self.assertEqual(i18n.lang(), code)
                self.assertEqual(i18n.t("btn.refresh"), "刷新" if code == "zh" else "Refresh")


if __name__ == "__main__":
    unittest.main()
