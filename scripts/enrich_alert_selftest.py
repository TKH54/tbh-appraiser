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
PARSER.add_argument("--case", choices=["enrich"], default="enrich")
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
    if ARGS.revision and not names.issubset({n.name for n in defs}):
        raise unittest.SkipTest("comparison revision has no detail-alert implementation")
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
              ENRICH_REFRESHED=0, ENRICH_SKIP_REASON="")
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in {
                        "STALE_ALERT_SEC", "STALE_ALERT_REPEAT_SEC",
                        "ENRICH_STALE_SEC", "ENRICH_ALERT_REPEAT_SEC"}:
                    ns[target.id] = ast.literal_eval(node.value)
    exec(compile(ast.Module(body=defs, type_ignores=[]), PATH, "exec"), ns)
    return ns, now, state, doc, messages


class EnrichTests(unittest.TestCase):
    def setUp(self):
        self.ns, self.now, self.state, self.doc, _ = harness()
        self.post = Mock(return_value=True)
        self.ns["_post_discord"] = self.post

    def record(self, count=0, error=None):
        self.ns["_record_enrich_health"](count, error)

    def watch(self, **kwargs):
        return self.ns["_maybe_alert_enrich"](**kwargs)

    def test_skip_reason_is_recorded_but_never_rings(self):
        """A frozen detail ring has to leave its cause in the repo -- the phone's
        stderr is on the phone. It must not become an alert: a priceoverview
        squeeze is the self-healing case the 6h threshold is there for."""
        self.ns["ENRICH_SKIP_REASON"] = "probe HTTP 429 (signals=4)"
        self.record()
        self.assertEqual(self.state["enrich_last_skip"], "probe HTTP 429 (signals=4)")
        self.watch()
        self.post.assert_not_called()
        self.assertNotIn("enrich_error", self.state)

    def test_a_success_clears_the_skip_reason(self):
        self.ns["ENRICH_SKIP_REASON"] = "probe HTTP 429 (signals=4)"
        self.record()
        self.ns["ENRICH_SKIP_REASON"] = ""
        self.record(count=3)
        self.assertNotIn("enrich_last_skip", self.state)

    def test_fresh_t_with_no_detail_success_daily_limit(self):
        self.record()
        self.watch()
        self.post.assert_not_called()
        self.now[0] += 21600
        self.doc["_eoff"] += 10  # failed requests can advance this!
        self.watch()
        self.assertEqual(self.post.call_count, 1)
        self.now[0] += 600
        self.watch()
        self.assertEqual(self.post.call_count, 1)
        self.now[0] += 86400
        self.watch()
        self.assertEqual(self.post.call_count, 2)

    def test_success_resets_even_when_offset_wraps(self):
        self.record()
        self.now[0] += 21500
        self.record(1)
        self.now[0] += 21500
        self.watch()
        self.post.assert_not_called()

    def test_legacy_offset_progress_and_missing_offset(self):
        self.watch()
        self.now[0] += 21500
        self.doc["_eoff"] = 0  # wrap counts as movement
        self.watch()
        self.now[0] += 21500
        self.watch()
        self.post.assert_not_called()
        self.doc.pop("_eoff")
        self.now[0] += 101
        self.watch()
        self.post.assert_called_once()

    def test_exception_immediate_deduped_recovery_new_incident(self):
        self.record(error="ValueError")
        self.watch()
        self.post.assert_called_once()
        self.now[0] += 600
        self.record(error="ValueError")
        self.watch()
        self.post.assert_called_once()
        self.record(1)
        self.now[0] += 600
        self.record(error="ValueError")
        self.watch()
        self.assertEqual(self.post.call_count, 2)

    def test_failed_send_retries(self):
        self.record(error="ValueError")
        self.post.return_value = False
        self.watch()
        self.assertNotIn("last_enrich_alert_at", self.state)
        self.post.return_value = True
        self.watch()
        self.assertEqual(self.post.call_count, 2)

    def test_whole_source_stop_uses_existing_alert(self):
        self.record()
        self.now[0] += 22000
        self.ns["_prev_snapshot_meta"] = lambda: (self.now[0] - 8000, "local")
        self.watch()
        self.post.assert_not_called()

    def test_known_throttle_is_quiet_until_six_hours(self):
        self.record()
        for elapsed in (7200, 14400, 21599):
            self.now[0] = 2_000_000 + elapsed
            self.watch()
            self.post.assert_not_called()
        self.now[0] += 1
        self.watch()
        self.post.assert_called_once()

    def test_skip_observation_changes_are_not_saved(self):
        self.watch()  # normal run initializes the observation
        before = dict(self.state)
        self.doc["_eoff"] += 10
        self.assertFalse(self.watch(persist_observations=False))
        self.assertEqual(self.state, before)
        self.watch()  # standby/proceed still persists observations
        self.assertEqual(self.state["enrich_offset"], self.doc["_eoff"])

    def test_skip_saves_only_delivered_notification_markers(self):
        self.record(error="ValueError")
        before = dict(self.state)
        self.post.return_value = False
        self.assertFalse(self.watch(persist_observations=False))
        self.assertEqual(self.state, before)
        self.post.return_value = True
        self.assertTrue(self.watch(persist_observations=False))
        self.assertEqual(self.state, {**before, "last_enrich_alert_at": self.now[0],
                                     "enrich_error_notified": before["enrich_error_since"]})
        self.assertFalse(self.watch(persist_observations=False))
        self.assertEqual(self.post.call_count, 2)

    def test_skip_never_calls_heartbeat_or_commits_observation_only(self):
        # Execute only the AST prefix ending at the skip return. Never run main
        # or any sweep/history/snapshot code, even if the prefix is regressed.
        main = next(n for n in ast.parse(SOURCE).body
                    if isinstance(n, ast.FunctionDef) and n.name == "main")
        prefix = []
        for statement in main.body:
            prefix.append(statement)
            if isinstance(statement, ast.If) and ast.unparse(statement.test) == "action == 'skip'":
                break
        else:
            self.fail("main skip guard not found")
        main.name, main.body = "gate_prefix", prefix
        exec(compile(ast.Module(body=[main], type_ignores=[]), PATH, "exec"), self.ns)
        self.ns.update(_maybe_alert_stale=Mock(), _gate=Mock(return_value="skip"),
                       _heartbeat=Mock(), _set_mode=Mock())
        self.ns["gate_prefix"]()
        self.ns["_heartbeat"].assert_not_called()
        self.ns["_set_mode"].assert_not_called()
        self.assertEqual(self.state, {})
        self.record(error="ValueError")
        self.ns["gate_prefix"]()
        self.ns["_heartbeat"].assert_not_called()
        self.ns["_set_mode"].assert_called_once_with("stateonly")

    def test_phone_writes_health_but_ci_sends(self):
        self.ns["SOURCE"] = "local"
        self.record(error="ValueError")
        self.assertFalse(self.watch())
        self.post.assert_not_called()
        self.ns["SOURCE"] = "ci"
        self.watch()
        self.post.assert_called_once()

    def test_real_enrich_counts_success_not_attempts_or_price_changes(self):
        items = {f"item{i:03d}": {"usd": 1, "q": 1} for i in range(100)}
        self.ns["get"] = lambda *a, **kw: {"success": False}
        off, _ = self.ns["enrich_shard"](items, {}, self.now[0])
        self.assertNotEqual(off, 0)
        self.assertEqual(self.ns["ENRICH_REFRESHED"], 0)
        # Confirmed no sales is a valid response, and a full lap wraps to 0.
        self.ns["get"] = lambda *a, **kw: {"success": True}
        self.ns["os"].environ.update(PRICES_SHARD="100", PRICES_HOT="0")
        off, _ = self.ns["enrich_shard"](items, {}, self.now[0])
        self.assertEqual(off, 0)
        self.assertEqual(self.ns["ENRICH_REFRESHED"], 100)


suite = unittest.defaultTestLoader.loadTestsFromTestCase(EnrichTests)
sys.exit(not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful())
