"""Offline alert tests: extract selected functions with AST, never run/import
build_prices.py. All HTTP, time, state and snapshot I/O are in-memory fakes.
No arguments runs all tests; --revision main reads baseline via git show.
"""
import argparse
import ast
import json
import os
import re
import subprocess
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

PARSER = argparse.ArgumentParser()
PARSER.add_argument("--case", choices=["discord"], default="discord")
PARSER.add_argument("--revision")
ARGS = PARSER.parse_args()
PATH = "scripts/build_prices.py"
SOURCE = (subprocess.check_output(["git", "show", f"{ARGS.revision}:{PATH}"], encoding="utf-8")
          if ARGS.revision else Path(PATH).read_text(encoding="utf-8"))


def harness():
    names = {"_post_discord", "_maybe_alert_stale", "_record_enrich_health",
             "_maybe_alert_enrich", "enrich_shard", "hot_ring"}
    tree = ast.parse(SOURCE)
    defs = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    now, state, doc, messages = [2_000_000.0], {}, {"_eoff": 10}, []
    ns = dict(json=json, os=SimpleNamespace(environ={"DISCORD_WEBHOOK": "fake"}),
              sys=sys, re=re, datetime=datetime, timezone=timezone, SOURCE="ci",
              time=SimpleNamespace(time=lambda: now[0], sleep=lambda _: None),
              requests=SimpleNamespace(post=Mock(return_value=SimpleNamespace(status_code=204))),
              _load_state=lambda: dict(state),
              _write_state=lambda value: (state.clear(), state.update(value)),
              OUT=SimpleNamespace(read_text=lambda **kw: json.dumps(doc)),
              _prev_snapshot_meta=lambda: (now[0], "local"),
              _throttled=lambda: False, APPID=3678970, THROTTLE_SIGNALS=0,
              ENRICH_REFRESHED=0)
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in {
                        "STALE_ALERT_SEC", "STALE_ALERT_REPEAT_SEC",
                        "ENRICH_STALE_SEC", "ENRICH_ALERT_REPEAT_SEC"}:
                    ns[target.id] = ast.literal_eval(node.value)
    exec(compile(ast.Module(body=defs, type_ignores=[]), PATH, "exec"), ns)
    return ns, now, state, doc, messages


class DiscordTests(unittest.TestCase):
    def test_http_results(self):
        ns, *_ = harness()
        for status in (204, 200, 302, 400, 429, 500):
            ns["requests"].post.return_value.status_code = status
            with self.subTest(status=status):
                self.assertEqual(ns["_post_discord"]("test"), 200 <= status < 300)

    def test_transport_failure_and_missing_webhook(self):
        ns, *_ = harness()
        ns["requests"].post.side_effect = TimeoutError("fake timeout")
        self.assertFalse(ns["_post_discord"]("test"))
        ns["requests"].post.reset_mock()
        ns["os"].environ.clear()
        self.assertFalse(ns["_post_discord"]("test"))
        ns["requests"].post.assert_not_called()

    def test_stale_failed_delivery_remains_retryable(self):
        ns, now, state, *_ = harness()
        ns["_prev_snapshot_meta"] = lambda: (now[0] - 8000, "local")
        ns["requests"].post.return_value.status_code = 500
        ns["_maybe_alert_stale"]()
        self.assertNotIn("last_stale_alert_utc", state)
        ns["requests"].post.return_value.status_code = 204
        ns["_maybe_alert_stale"]()
        self.assertIn("last_stale_alert_utc", state)
        ns["_maybe_alert_stale"]()
        self.assertEqual(ns["requests"].post.call_count, 2)


suite = unittest.defaultTestLoader.loadTestsFromTestCase(DiscordTests)
sys.exit(not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful())
