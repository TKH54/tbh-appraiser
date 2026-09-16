"""Offline pending digest tests. No Steam, Discord or repository data writes."""
import ast
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import autocatalog_notify as notify


class PendingTests(unittest.TestCase):
    def test_throttled_or_missing_outputs_still_counts_every_pending_item(self):
        state = {}
        items = {"Plague Fruit": {"base": "Plague Fruit", "rarity": ""}}
        prices = {"Plague Fruit": {}, "Plague Fruit (Divine)": {}, "New Scroll": {}}
        notify.observe(state, prices, items, 1_000_000)
        self.assertEqual(len(state["pending"]), 2)
        self.assertEqual(state["blocked_without_icons"], ["Plague Fruit (Divine)"])
        self.assertIn("2 件", notify.digest(state, 1_000_000))
        self.assertIn("1 件", notify.digest(state, 1_000_000))

    def test_daily_age_and_resolution(self):
        state = {}
        notify.observe(state, {"A": {}}, {}, 1_000_000)
        state["last_sent"] = 1_000_000
        self.assertIsNone(notify.digest(state, 1_000_001))
        notify.observe(state, {"A": {}, "B": {}}, {}, 1_086_400)
        self.assertIn("最長 1 日", notify.digest(state, 1_086_400))
        notify.observe(state, {"B": {}}, {}, 1_086_401)
        self.assertIn("最長 0 日", notify.digest(state, 1_086_401))
        notify.observe(state, {}, {}, 1_086_402)
        self.assertIsNone(notify.digest(state, 1_086_402))

    def test_notify_only_removes_shipped_names_and_dedupes_after_delivery(self):
        # Run the real notifier main against virtual files, not site data.
        for push, status in [("skipped", 204), ("success", 204), ("success", 500)]:
            state = {"pending": {"A": 1, "B": 1}, "blocked_without_icons": []}
            writes = []
            class File:
                def __init__(self, name): self.name = name
                def __truediv__(self, name): return File(name)
                @property
                def parent(self): return self
                def mkdir(self, **kw): pass
                def exists(self): return True
                def read_text(self, **kw):
                    return json.dumps(state if self.name == "state" else {"tier1": ["A"]})
                def write_text(self, value, **kw): writes.append(json.loads(value))
            from unittest.mock import patch
            with patch.object(notify, "STATE", File("state")), \
                 patch.object(notify, "ROOT", File("root")), \
                 patch.object(notify.time, "time", return_value=1_000_000), \
                 patch.dict(notify.os.environ, {"PUSH1": push, "PUSH2": "skipped",
                            "DISCORD_WEBHOOK": "fake", "RUN_URL": "offline"}, clear=True), \
                 patch.object(notify.requests, "post", return_value=SimpleNamespace(status_code=status)), \
                 patch.object(sys, "argv", ["notify", "notify"]):
                if status == 500:
                    with self.assertRaises(RuntimeError): notify.main()
                    self.assertEqual(writes, [])
                else:
                    notify.main()
                    self.assertEqual(set(writes[-1]["pending"]), {"B"} if push == "success" else {"A", "B"})
                    self.assertEqual(writes[-1]["last_sent"], 1_000_000)

    def steam_harness(self):
        # Extract only the network wrapper/parser; do not import OpenCV or run main.
        tree = ast.parse(Path("scripts/autocatalog.py").read_text(encoding="utf-8"))
        defs = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))
                and n.name in {"_get", "resolve_icons", "Throttled"}]
        ns = dict(time=SimpleNamespace(sleep=lambda _: None),
                  requests=SimpleNamespace(RequestException=OSError), sys=sys,
                  MAX_429=8, REQ_SLEEP=0, SEARCH="fake", APPID=1)
        exec(compile(ast.Module(body=defs, type_ignores=[]), "autocatalog.py", "exec"), ns)
        return ns

    def response(self, status=200, body=None, invalid_json=False):
        return SimpleNamespace(status_code=status,
            raise_for_status=Mock(side_effect=OSError(f"HTTP {status}") if status >= 400 else None),
            json=Mock(side_effect=ValueError("non-JSON") if invalid_json else None,
                      return_value=body))

    def test_temporary_steam_failures_retry_then_hold(self):
        for reply in [self.response(429), self.response(500), self.response(502),
                      self.response(invalid_json=True), self.response(body={"success": False})]:
            with self.subTest(status=reply.status_code, body=reply.json.return_value):
                ns = self.steam_harness()
                sess = SimpleNamespace(n429=0, get=Mock(return_value=reply))
                with self.assertRaises(ns["Throttled"]):
                    ns["resolve_icons"](sess, ["A"], {})
                self.assertEqual(sess.get.call_count, 4)

    def test_temporary_steam_failures_can_recover_within_same_budget(self):
        ns = self.steam_harness()
        good = self.response(body={"success": True, "results": [
            {"hash_name": "A", "asset_description": {"icon_url": "ICON_A"}}]})
        sess = SimpleNamespace(n429=0, get=Mock(side_effect=[
            self.response(502), self.response(invalid_json=True),
            self.response(body={"success": False}), good]))
        self.assertEqual(ns["resolve_icons"](sess, ["A"], {}), {"A": "ICON_A"})
        self.assertEqual(sess.get.call_count, 4)

    def test_unexpected_steam_errors_still_fail(self):
        for reply in [self.response(403), self.response(404), self.response(body=[]),
                      self.response(body={}), self.response(body={"success": True, "results": {}}),
                      self.response(body={"success": "invalid"})]:
            ns = self.steam_harness()
            sess = SimpleNamespace(n429=0, get=Mock(return_value=reply))
            with self.assertRaises((OSError, ValueError)):
                ns["resolve_icons"](sess, ["A"], {})
            self.assertEqual(sess.get.call_count, 1)
        sess.get.side_effect = OSError("offline timeout")
        with self.assertRaises(OSError): ns["_get"](sess, "fake")


if __name__ == "__main__":
    unittest.main()
