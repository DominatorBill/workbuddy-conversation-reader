import hashlib
import importlib.util
import json
import pathlib
import sqlite3
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "wb_catalog.py"
SPEC = importlib.util.spec_from_file_location("wb_catalog", MODULE_PATH)
catalog = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(catalog)


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = pathlib.Path(self.tmp.name) / "wb #data"
        self.home.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def make_jsonl(self, project, sid):
        directory = self.home / "projects" / project
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / (sid + ".jsonl")
        path.write_text('{"synthetic":"body must not be read"}\n', encoding="utf-8")
        return path

    def test_missing_database_still_finds_jsonl_and_does_not_create_files(self):
        path = self.make_jsonl("one", "isolated")
        before = sorted(str(p.relative_to(self.home)) for p in self.home.rglob("*"))
        result = catalog.load_catalog(self.home)
        after = sorted(str(p.relative_to(self.home)) for p in self.home.rglob("*"))
        self.assertEqual(result["index"]["status"], "missing")
        self.assertEqual(result["index"]["detail"], "index database is missing")
        self.assertEqual(before, after)
        self.assertEqual(result["sessions"][0]["id"], "isolated")
        self.assertEqual(result["sessions"][0]["paths"], [str(path.absolute())])
        self.assertFalse(result["sessions"][0]["indexed"])
        self.assertIsNone(result["sessions"][0]["title"])

    def test_nonexistent_home_is_missing_without_creating_it(self):
        absent = self.home / "not-created"
        result = catalog.load_catalog(absent)
        self.assertEqual(result["index"]["status"], "missing")
        self.assertFalse(absent.exists())
        self.assertEqual(set(result), {"index", "sessions"})

    def test_corrupt_database_and_missing_sessions_table_are_invalid(self):
        db = self.home / "workbuddy.db"
        db.write_bytes(b"not a sqlite database")
        digest = hashlib.sha256(db.read_bytes()).hexdigest()
        result = catalog.load_catalog(self.home)
        self.assertEqual(result["index"]["status"], "invalid")
        self.assertNotIn(str(self.home), result["index"]["detail"])
        self.assertEqual(digest, hashlib.sha256(db.read_bytes()).hexdigest())

        db.unlink()
        sqlite3.connect(db).close()
        result = catalog.load_catalog(self.home)
        self.assertEqual(result["index"]["status"], "invalid")
        self.assertEqual(result["index"]["detail"], "sessions table is missing")

    def test_minimal_old_schema_and_index_only_session(self):
        db = sqlite3.connect(self.home / "workbuddy.db")
        db.execute("create table sessions (id text primary key)")
        db.execute("insert into sessions values (?)", ("indexed-only",))
        db.commit()
        db.close()

        row = catalog.load_catalog(self.home)["sessions"][0]
        self.assertEqual(row["id"], "indexed-only")
        self.assertTrue(row["indexed"])
        self.assertEqual(row["paths"], [])
        for key in ("title", "cwd", "status", "updated_ms", "created_at",
                    "raw_title", "custom_title", "updated_at", "mode", "model",
                    "permission_mode"):
            self.assertIsNone(row[key])
        self.assertFalse(row["automation"])
        self.assertFalse(row["deleted"])

    def test_full_schema_title_precedence_timestamps_and_deleted_flags(self):
        db = sqlite3.connect(self.home / "workbuddy.db")
        db.execute("""create table sessions (
            id text, cwd text, title text, custom_title text, status text,
            updated_at, last_activity_at, created_at, is_background_automation,
            deleted_at, mode, model, permission_mode)""")
        db.executemany("insert into sessions values (?,?,?,?,?,?,?,?,?,?,?,?,?)", [
            ("prefer-custom", "C:/work", "old", "new", "done", 40, 70, 10, 1, None,
             "chat", "model-x", "ask"),
            ("empty-custom", None, "original", "", None, 30, None, 3, 0, None,
             None, None, None),
            ("fallback", None, "stored", None, None, "bad", "bad", "bad", 0, 0,
             7, 42, b"not-text"),
            ("fallback-updated", None, "t", None, None, 45, None, 2, "false", None,
             None, None, None),
        ])
        db.commit()
        db.close()
        self.make_jsonl("project", "prefer-custom")
        result = catalog.load_catalog(self.home)
        self.assertEqual(result["index"]["status"], "ok")
        rows = {r["id"]: r for r in result["sessions"]}
        self.assertEqual(rows["prefer-custom"]["title"], "new")
        self.assertEqual(rows["prefer-custom"]["raw_title"], "old")
        self.assertEqual(rows["prefer-custom"]["custom_title"], "new")
        self.assertEqual(rows["prefer-custom"]["updated_at"], 40)
        self.assertEqual(rows["prefer-custom"]["updated_ms"], 70)
        self.assertEqual(rows["prefer-custom"]["created_at"], 10)
        self.assertEqual(rows["prefer-custom"]["cwd"], "C:/work")
        self.assertTrue(rows["prefer-custom"]["automation"])
        self.assertEqual(rows["prefer-custom"]["mode"], "chat")
        self.assertEqual(rows["prefer-custom"]["model"], "model-x")
        self.assertEqual(rows["prefer-custom"]["permission_mode"], "ask")
        self.assertEqual(rows["empty-custom"]["title"], "original")
        self.assertIsNone(rows["fallback"]["updated_ms"])
        self.assertIsNone(rows["fallback"]["created_at"])
        self.assertIsNone(rows["fallback"]["mode"])
        self.assertIsNone(rows["fallback"]["model"])
        self.assertIsNone(rows["fallback"]["permission_mode"])
        self.assertTrue(rows["fallback"]["deleted"])
        self.assertEqual(rows["fallback-updated"]["updated_ms"], 45)
        self.assertFalse(rows["fallback-updated"]["automation"])

    def test_orphan_duplicate_physical_copies_and_sorted_paths(self):
        db = sqlite3.connect(self.home / "workbuddy.db")
        db.execute("create table sessions (id text primary key)")
        db.execute("insert into sessions values ('missing-body')")
        db.commit()
        db.close()
        p2 = self.make_jsonl("z-project", "dupe")
        p1 = self.make_jsonl("a-project", "dupe")
        self.make_jsonl("other", "orphan")
        false_match = self.home / "projects" / "other" / "directory.jsonl"
        false_match.mkdir()
        rows = catalog.load_catalog(self.home)["sessions"]
        self.assertEqual([r["id"] for r in rows], ["dupe", "missing-body", "orphan"])
        dupe = rows[0]
        self.assertEqual(dupe["paths"], sorted([str(p1.absolute()), str(p2.absolute())]))
        self.assertFalse(dupe["indexed"])
        self.assertTrue(rows[1]["indexed"])
        self.assertEqual(rows[1]["paths"], [])
        self.assertFalse(rows[2]["indexed"])

    def test_paths_with_hash_are_found(self):
        # A literal # is valid in Windows paths and must be percent-encoded in
        # the read-only SQLite URI rather than interpreted as a URI fragment.
        path = self.make_jsonl("project#markers", "abc")
        db = sqlite3.connect(self.home / "workbuddy.db")
        db.execute("create table sessions (id text primary key)")
        db.execute("insert into sessions values ('abc')")
        db.commit()
        db.close()
        result = catalog.load_catalog(self.home)
        self.assertEqual(result["index"]["status"], "ok")
        self.assertEqual(result["sessions"][0]["paths"], [str(path.absolute())])

    def test_database_bytes_and_directory_snapshot_are_unchanged(self):
        db = sqlite3.connect(self.home / "workbuddy.db")
        db.execute("create table sessions (id text primary key, title text)")
        db.execute("insert into sessions values ('db-only', 'Synthetic')")
        db.commit()
        db.close()
        self.make_jsonl("synthetic", "file-only")
        before_tree = sorted((str(p.relative_to(self.home)), p.is_dir()) for p in self.home.rglob("*"))
        before_hash = hashlib.sha256((self.home / "workbuddy.db").read_bytes()).hexdigest()
        catalog.load_catalog(self.home)
        after_tree = sorted((str(p.relative_to(self.home)), p.is_dir()) for p in self.home.rglob("*"))
        after_hash = hashlib.sha256((self.home / "workbuddy.db").read_bytes()).hexdigest()
        self.assertEqual(before_tree, after_tree)
        self.assertEqual(before_hash, after_hash)


if __name__ == "__main__":
    unittest.main()
