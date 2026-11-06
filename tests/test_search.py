import json
import datetime as dt
import os
import pathlib
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "wb-conv.py"


def message(role, text, timestamp=None):
    row = {"type": "message", "role": role, "content": [{"type": "input_text", "text": text}]}
    if timestamp is not None:
        row["timestamp"] = timestamp
    return row


class DiscoveryAndSearch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = pathlib.Path(self.tmp.name) / "wb"
        self.projects = self.home / "projects" / "demo"
        self.projects.mkdir(parents=True)
        self.dbpath = self.home / "workbuddy.db"
        self._db()
        self._index("indexed-one", "Indexed title", r"C:\demo")
        self._write("indexed-one", [message("user", "needle", 1000), message("assistant", "needle", 2000)])
        self._write("orphan-one", [message("user", "needle", 3000), message("assistant", "needle")])
        self._index("missing-one", "Missing body", r"C:\demo")
        self._index("deleted-one", "Deleted", r"C:\demo", deleted=1)
        self._write("deleted-one", [message("user", "needle", 4000)])

    def tearDown(self):
        self.tmp.cleanup()

    def _db(self):
        db = sqlite3.connect(self.dbpath)
        db.execute("create table sessions (id text, cwd text, title text, custom_title text, status text, updated_at integer, last_activity_at integer, created_at integer, is_background_automation integer, deleted_at integer, mode text, model text, permission_mode text)")
        db.commit()
        db.close()

    def _index(self, sid, title, cwd, deleted=None, custom=None):
        db = sqlite3.connect(self.dbpath)
        db.execute("insert into sessions values (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (sid, cwd, title, custom, "done", 10, 10, 1, 0, deleted, "chat", "model-x", "ask"))
        db.commit()
        db.close()

    def _write(self, sid, rows, directory=None):
        parent = pathlib.Path(directory) if directory else self.projects
        parent.mkdir(parents=True, exist_ok=True)
        (parent / f"{sid}.jsonl").write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    def run_cli(self, *args, home=None, extra_env=None):
        env = os.environ.copy()
        env.pop("WORKBUDDY_DIR", None)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        if extra_env:
            env.update(extra_env)
        return subprocess.run([sys.executable, "-B", str(CLI), "--home", str(home or self.home), *args],
                              capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)

    def test_discovery_doctor_and_locate_include_index_orphans_and_missing(self):
        p = self.run_cli("doctor", "--json")
        self.assertEqual(p.returncode, 0, p.stderr)
        report = json.loads(p.stdout)
        self.assertEqual(report["index"]["status"], "ok")
        self.assertEqual((report["indexed_count"], report["jsonl_session_count"], report["jsonl_file_count"]), (3, 3, 3))
        self.assertEqual((report["orphan_count"], report["missing_count"], report["duplicate_count"]), (1, 1, 0))
        p = self.run_cli("locate", "missing-one", "--json")
        self.assertEqual(json.loads(p.stdout), {"id": "missing-one", "paths": [], "indexed": True, "body_missing": True})
        p = self.run_cli("locate", "orphan-one", "--json")
        self.assertFalse(json.loads(p.stdout)["indexed"])

    def test_ambiguous_locate_reports_candidates_and_exit_two(self):
        self._write("indexed-two", [message("user", "other")])
        p = self.run_cli("locate", "indexed-", "--json")
        self.assertEqual(p.returncode, 2)
        self.assertEqual({row["id"] for row in json.loads(p.stdout)["matches"]}, {"indexed-one", "indexed-two"})

    def test_duplicate_jsonl_locate_lists_all_but_show_refuses_selection(self):
        other = self.home / "projects" / "other"
        self._write("indexed-one", [message("user", "duplicate")], other)
        p = self.run_cli("locate", "indexed-one", "--json")
        self.assertEqual(len(json.loads(p.stdout)["paths"]), 2)
        p = self.run_cli("show", "indexed-one")
        self.assertEqual(p.returncode, 1)
        self.assertIn("indexed-one", p.stderr)

    def test_missing_and_invalid_index_degrade_to_jsonl_search_and_list(self):
        self.dbpath.unlink()
        for command in (("list", "--json"), ("find", "needle", "--deep", "--json"),
                        ("doctor", "--json"), ("locate", "orphan-one", "--json")):
            p = self.run_cli(*command)
            self.assertEqual(p.returncode, 0, (command, p.stderr))
            json.loads(p.stdout)
        self.assertIn("orphan-one", self.run_cli("find", "needle", "--deep", "--json").stdout)
        self.assertIn("警告", self.run_cli("list", "--json").stderr)
        self.dbpath.write_text("not sqlite", encoding="utf-8")
        self.assertEqual(json.loads(self.run_cli("doctor", "--json").stdout)["index"]["status"], "invalid")

    def test_search_role_date_boundaries_cwd_and_recent_sort(self):
        p = self.run_cli("search", "needle", "--since", "1970-01-01T00:00:02Z", "--before", "1970-01-01T00:00:04Z",
                         "--role", "assistant", "--cwd", "C:\\demo", "--sort", "recent", "--json", "-n", "20")
        self.assertEqual(p.returncode, 0, p.stderr)
        rows = json.loads(p.stdout)
        self.assertEqual([r["id"] for r in rows], ["indexed-one"])
        self.assertEqual(rows[0]["last_match_ms"], 2000)
        self.assertEqual(rows[0]["title"], "Indexed title")

    def test_search_bad_and_reversed_date_ranges_are_argparse_errors(self):
        for opts in (("--since", "2025-02-30"), ("--since", "2025-01-02", "--before", "2025-01-01")):
            p = self.run_cli("search", "needle", *opts)
            self.assertEqual(p.returncode, 2)

    def test_search_all_requires_explicit_role_and_timestamp_for_filters(self):
        rows = [{"type": "reasoning", "text": "event-only-needle", "timestamp": 1000},
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "event-only-needle"}]}]
        self._write("all-events", rows)
        p = self.run_cli("search", "event-only-needle", "--all", "--role", "user", "--since", "1970-01-01", "--json", "-n", "20")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn("all-events", [row["id"] for row in json.loads(p.stdout)])

    def test_search_all_keeps_non_object_json_lines_and_filters_missing_metadata(self):
        self._write("scalar-events", [["nested-needle"], "scalar-needle", 17, None,
                                       {"object": "dict-needle"}])
        p = self.run_cli("search", "needle", "--all", "--json", "-n", "50")
        self.assertEqual(p.returncode, 0, p.stderr)
        row = next(r for r in json.loads(p.stdout) if r["id"] == "scalar-events")
        self.assertEqual(row["count"], 3)
        for options in (("--role", "user"), ("--since", "1970-01-01")):
            p = self.run_cli("search", "needle", "--all", *options, "--json", "-n", "50")
            self.assertNotIn("scalar-events", [r["id"] for r in json.loads(p.stdout)])

    def test_same_id_search_copies_are_aggregated_without_message_deduplication(self):
        other = self.home / "projects" / "copy"
        self._write("indexed-one", [message("user", "needle", 3000)], other)
        p = self.run_cli("search", "needle", "--json", "-n", "50")
        rows = json.loads(p.stdout)
        matches = [r for r in rows if r["id"] == "indexed-one"]
        self.assertEqual(len(matches), 1)
        row = matches[0]
        self.assertEqual(row["count"], 3)
        self.assertEqual(row["last_match_ms"], 3000)
        self.assertEqual(len(row["paths"]), 2)
        self.assertTrue(row["ambiguous"])
        self.assertLessEqual(len(row["samples"]), 2)

    def test_jsonl_named_directory_does_not_break_search(self):
        (self.projects / "not-a-file.jsonl").mkdir()
        p = self.run_cli("search", "needle", "--json", "-n", "50")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue(json.loads(p.stdout))

    def test_list_and_title_preserve_index_metadata_fields(self):
        self._index("compat-row", "raw title", r"C:\compat", custom="display title")
        self._write("compat-row", [message("user", "body")])
        listed = json.loads(self.run_cli("list", "--json", "-n", "50").stdout)
        row = next(r for r in listed if r["id"] == "compat-row")
        self.assertEqual((row["title"], row["mode"], row["model"], row["updated_ms"]),
                         ("display title", "chat", "model-x", 10))
        p = self.run_cli("title", "compat-row")
        self.assertEqual(p.returncode, 0, p.stderr)
        info = json.loads(p.stdout)
        self.assertEqual((info["title"], info["custom_title"], info["updated_at"],
                          info["mode"], info["model"], info["permission_mode"]),
                         ("raw title", "display title", 10, "chat", "model-x", "ask"))

    def test_local_date_midnight_uses_dst_rules_and_offset_timestamp_is_exact(self):
        if not hasattr(time, "tzset"):
            self.skipTest("time.tzset is unavailable on this platform")
        utc = dt.timezone.utc
        points = [dt.datetime(2024, 3, 10, 4, 59, tzinfo=utc),
                  dt.datetime(2024, 3, 10, 5, 0, tzinfo=utc),
                  dt.datetime(2024, 3, 11, 3, 59, tzinfo=utc),
                  dt.datetime(2024, 3, 11, 4, 0, tzinfo=utc)]
        stamps = [int(point.timestamp() * 1000) for point in points]
        self._write("dst-session", [message("user", f"dst-needle-{i}", stamp)
                                    for i, stamp in enumerate(stamps)])
        previous_tz = os.environ.get("TZ")
        os.environ["TZ"] = "America/New_York"
        time.tzset()
        try:
            for start in ("2024-03-10", "2024-03-10T00:00:00-05:00"):
                p = self.run_cli("search", "dst-needle", "--since", start, "--before", "2024-03-11",
                                 "--json", "-n", "50")
                self.assertEqual(p.returncode, 0, p.stderr)
                row = next(r for r in json.loads(p.stdout) if r["id"] == "dst-session")
                self.assertEqual(row["count"], 2)
        finally:
            if previous_tz is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = previous_tz
            time.tzset()

    def test_invalid_timestamps_are_ignored_for_sorting_without_crashing(self):
        self._write("bad-timestamp", [message("user", "needle", "nonsense")])
        p = self.run_cli("search", "needle", "--sort", "recent", "--json", "-n", "20")
        self.assertEqual(p.returncode, 0, p.stderr)
        row = next(x for x in json.loads(p.stdout) if x["id"] == "bad-timestamp")
        self.assertIsNone(row["last_match_ms"])

    def test_tail_counts_message_records_and_preserves_order_with_images(self):
        rows = [message("user", "first", 1000), message("assistant", "second", 2000),
                {"type": "message", "role": "user", "timestamp": 3000,
                 "content": [{"type": "input_image"}]}]
        self._write("tail-session", rows)
        p = self.run_cli("show", "tail-session", "--tail", "2", "--images", "--out", str(pathlib.Path(self.tmp.name) / "tail.md"))
        self.assertEqual(p.returncode, 0, p.stderr)
        body = (pathlib.Path(self.tmp.name) / "tail.md").read_text(encoding="utf-8")
        self.assertIn("消息数: 2", body)
        self.assertNotIn("first", body)
        self.assertLess(body.index("second"), body.index("图片未导出"))

    def test_missing_home_diagnostics_do_not_create_files(self):
        absent = pathlib.Path(self.tmp.name) / "absent"
        p = self.run_cli("doctor", "--json", home=absent)
        self.assertEqual(json.loads(p.stdout)["index"]["status"], "missing")
        self.assertFalse(absent.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
