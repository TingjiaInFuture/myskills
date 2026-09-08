"""Run with: python -m unittest discover -s tests -v (stdlib only)."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

# This also permits running the test file directly from any directory.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import kb


def brief(title="测试标题", summary="测试摘要", keywords="", body="可复用结论",
          status="有效", extra=""):
    return (f"标题: {title}\n摘要: {summary}\n关键词: {keywords}\n日期: 2026-01-01\n"
            f"状态: {status}\n\n## 可复用经验\n- {body}\n{extra}")


class TokenizerTests(unittest.TestCase):
    def test_header_original_bom_crlf(self):
        parsed = kb.parse_brief("\ufeff" + brief(keywords="SQLite，中文;检索、词法").replace("\n", "\r\n"), "sample")
        self.assertEqual(parsed.title, "测试标题")
        self.assertEqual(parsed.keywords, ("SQLite", "中文", "检索", "词法"))
        self.assertEqual(parsed.status, "active")
        self.assertNotIn("状态", parsed.body)

    def test_metadata_english_and_unknown_status(self):
        parsed = kb.parse_brief("# Heading\nSummary: compact\nStatus: draft\n\n## Notes\nwork", "sample")
        self.assertEqual(parsed.title, "Heading")
        self.assertEqual(parsed.status, "inactive")
        self.assertEqual(parsed.summary, "compact")
        self.assertEqual(kb.parse_brief("plain text", "fallback").title, "fallback")

    def test_references_and_history_excluded(self):
        parsed = kb.parse_brief(brief(extra="\n## 上下文引用\n/private/SECRET_REFERENCE\n"
                                     "## 更新记录\nHISTORY_ONLY\n## More\nuseful"), "x")
        self.assertNotIn("SECRET_REFERENCE", parsed.body)
        self.assertNotIn("HISTORY_ONLY", parsed.body)
        self.assertIn("useful", parsed.body)
        self.assertIn("SECRET_REFERENCE", parsed.raw)

    def test_han_bigram_and_unigram(self):
        indexed = " " + kb.index_tokens("解决数据库备份") + " "
        for word in ["数据库", "数据", "据", "库备"]:
            _, groups = kb.compile_query(word)
            self.assertIn(" " + " ".join(groups[0].tokens) + " ", indexed)

    def test_case_width_accents_code_and_dedup(self):
        _, groups = kb.compile_query("ＳＱＬｉｔｅ sqlite café C++ C# ERR_CONNECTION_RESET")
        self.assertEqual([g.label for g in groups],
                         ["sqlite", "cafe", "cplusplus", "csharp", "err", "connection", "reset"])
        self.assertEqual(list(kb.lexemes("用C++开发C#工具")),
                         [(True, "用"), (False, "cplusplus"), (True, "开发"),
                          (False, "csharp"), (True, "工具")])

    def test_query_caps_and_validation(self):
        for text in ["a" * 2049, " ".join(f"term{i}" for i in range(33)), "中" * 130]:
            with self.assertRaises(kb.KBError):
                kb.compile_query(text)
        with self.assertRaises(kb.KBError):
            kb.compile_query("hello", mode="sql")
        self.assertEqual(kb.compile_query(' \" , () * - '), ("", []))


class KBTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="task-kb-test-")
        self.root = Path(self.temp.name) / "知识库"
        self.root.mkdir()
        self.k = kb.KnowledgeBase(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def write(self, slug, text=None, **kwargs):
        path = self.root / slug / "brief.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text if text is not None else brief(**kwargs), encoding="utf-8")
        return path

    def ids(self, query, **kwargs):
        return [row["id"] for row in self.k.search(query, **kwargs)["results"]]

    def test_empty_and_missing_index(self):
        with self.assertRaises(kb.KBError):
            self.k.search("sqlite")
        self.assertFalse(self.k.db.exists())
        self.assertEqual(self.k.sync()["documents"], 0)
        self.assertEqual(self.k.search("sqlite")["results"], [])
        self.assertTrue(self.k.doctor()["ok"])

    def test_crud_rename_and_integrity(self):
        self.write("first", keywords="uniquefirst")
        first = self.k.sync()
        self.assertEqual(first["added"], 1)
        self.assertEqual(self.ids("uniquefirst"), ["first"])
        self.write("first", keywords="uniquenewvalue")
        changed = self.k.sync(["first"])
        self.assertEqual(changed["updated"], 1)
        self.assertEqual(self.ids("uniquefirst"), [])
        self.assertEqual(self.ids("uniquenewvalue"), ["first"])
        (self.root / "first").rename(self.root / "renamed")
        renamed = self.k.sync()
        self.assertEqual((renamed["added"], renamed["deleted"]), (1, 1))
        self.assertEqual(self.ids("uniquenewvalue"), ["renamed"])
        shutil.rmtree(self.root / "renamed")
        self.assertEqual(self.k.sync(["renamed"])["deleted"], 1)
        self.assertEqual(self.ids("uniquenewvalue"), [])
        self.assertTrue(self.k.doctor()["ok"])

    def test_han_substrings_two_chars_single_and_supplementary(self):
        self.write("han", title="使用数据库备份恢复流程", summary="", body="𠀀𠀁扩展汉字")
        self.k.sync()
        for query in ["数据库", "数据", "据", "库备", "𠀀𠀁", "𠀀"]:
            with self.subTest(query=query):
                self.assertEqual(self.ids(query), ["han"])

    def test_chinese_phrase_requires_adjacency(self):
        self.write("good", title="数据库恢复")
        self.write("bad", title="数据 其他 据库恢复")
        self.k.sync()
        self.assertEqual(self.ids("数据库"), ["good"])

    def test_non_han_exact_tokens_and_normalization(self):
        self.write("code", title="SQLite ＣＡＦÉ C++ C#", body="ERR_CONNECTION_RESET utf-8")
        self.write("distractor", title="SQLiteish", body="unrelated")
        self.k.sync()
        for query in ["sqlite", "cafe", "C++", "C#", "connection", "UTF-8"]:
            self.assertEqual(self.ids(query, mode="all"), ["code"])

    def test_any_all_and_mixed_script(self):
        self.write("both", title="SQLite中文检索")
        self.write("one", title="SQLite storage")
        self.k.sync()
        self.assertEqual(set(self.ids("SQLite 中文")), {"both", "one"})
        self.assertEqual(self.ids("SQLite 中文", mode="all"), ["both"])
        self.assertEqual(self.ids("SQLite中文", mode="all"), ["both"])

    def test_weights_title_keywords_above_body(self):
        self.write("title-hit", title="needle", body="some experience")
        self.write("keyword-hit", title="other", keywords="needle", body="some experience")
        self.write("body-hit", title="other", body="some needle experience")
        self.k.sync()
        ordered = self.ids("needle")
        self.assertEqual(ordered, ["title-hit", "keyword-hit", "body-hit"])
        result = self.k.search("needle", explain=True)
        scores = [r["bm25"] for r in result["results"]]
        self.assertEqual(scores, sorted(scores))
        self.assertEqual(result["query"]["weights"], [8, 6, 3, 1])

    def test_status_filter_before_top_k(self):
        for i in range(7):
            self.write(f"old-{i}", title="needle", status="已过时")
        for i in range(4):
            self.write(f"current-{i}", title="other", body="needle")
        self.k.sync()
        active = self.ids("needle", k=3)
        self.assertEqual(len(active), 3)
        self.assertTrue(all(slug.startswith("current-") for slug in active))
        self.assertTrue(self.ids("needle", k=3, all_status=True)[0].startswith("old-"))
        self.assertEqual(self.k.stats()["active"], 4)

    def test_status_only_edit_is_synced(self):
        self.write("entry", keywords="needle", status="有效")
        self.k.sync()
        self.write("entry", keywords="needle", status="过时")
        self.k.sync(["entry"])
        self.assertEqual(self.ids("needle"), [])
        self.assertEqual(self.ids("needle", all_status=True), ["entry"])

    def test_skip_metadata_references_experience_and_assets(self):
        self.write("entry", title="normal", extra="\n## 上下文引用\n/private/refneedle\n"
                                                   "## 更新记录\nhistoryneedle")
        (self.root / "entry" / "experience.md").write_text("deepneedle", encoding="utf-8")
        (self.root / "entry" / "assets").mkdir()
        (self.root / "entry" / "assets" / "brief.txt").write_text("assetneedle", encoding="utf-8")
        self.k.sync()
        for query in ["refneedle", "historyneedle", "deepneedle", "assetneedle"]:
            self.assertEqual(self.ids(query), [])
        self.assertEqual(self.k.get("entry", "experience")["content"], "deepneedle")

    def test_search_does_not_scan_or_read_knowledge_files(self):
        self.write("entry", keywords="needle")
        self.k.sync()
        with patch.object(kb.KnowledgeBase, "archive_ids", side_effect=AssertionError("scanned")), \
             patch.object(kb, "read_file", side_effect=AssertionError("read source")), \
             patch.object(os, "scandir", side_effect=AssertionError("scanned directory")):
            self.assertEqual(self.ids("needle"), ["entry"])

    def test_explicit_freshness_and_get_reads_live_file(self):
        self.write("entry", keywords="before")
        self.k.sync()
        self.write("entry", keywords="aftervalue")
        self.assertEqual(self.ids("before"), ["entry"])
        self.assertEqual(self.ids("aftervalue"), [])
        self.assertIn("aftervalue", self.k.get("entry")["content"])
        self.k.sync(["entry"])
        self.assertEqual(self.ids("aftervalue"), ["entry"])

    def test_target_sync_is_local_and_deduplicated(self):
        self.write("entry", keywords="needle")
        self.write("ignored", keywords="other")
        with patch.object(kb.KnowledgeBase, "archive_ids", side_effect=AssertionError("scanned")):
            result = self.k.sync(["entry", "entry"])
        self.assertEqual(result["added"], 1)
        self.assertEqual(result["documents"], 1)
        self.assertEqual(self.ids("other"), [])
        self.assertIsNone(self.k.stats()["last_full_sync"])

    def test_unchanged_does_not_read_contents(self):
        self.write("entry")
        self.k.sync()
        with patch.object(kb, "read_file", side_effect=AssertionError("read unchanged file")):
            result = self.k.sync()
        self.assertEqual(result["unchanged"], 1)
        self.assertEqual(result["rehashed"], 0)

    def test_timestamp_only_change_does_not_rewrite_fts(self):
        path = self.write("entry", keywords="needle")
        self.k.sync()
        info = path.stat()
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 1000000000))
        result = self.k.sync()
        self.assertEqual((result["updated"], result["unchanged"], result["rehashed"]), (0, 1, 1))
        self.assertTrue(self.k.doctor()["ok"])

    def test_force_hash_detects_same_size_preserved_mtime(self):
        path = self.write("entry", title="aaaa")
        self.k.sync()
        info = path.stat()
        self.write("entry", title="bbbb")
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
        self.assertEqual(self.k.sync()["unchanged"], 1)
        self.assertEqual(self.ids("bbbb"), [])
        self.assertEqual(self.k.sync(full=True)["updated"], 1)
        self.assertEqual(self.ids("bbbb"), ["entry"])

    def test_error_rolls_back_whole_sync(self):
        self.write("good", keywords="before")
        self.write("deleted", keywords="preserve")
        self.k.sync()
        self.write("good", keywords="aftervalue")
        bad = self.write("bad")
        bad.write_bytes(b"\xff\xfeNOT_UTF8")
        shutil.rmtree(self.root / "deleted")
        with self.assertRaises(kb.KBError):
            self.k.sync()
        self.assertEqual(self.ids("before"), ["good"])
        self.assertEqual(self.ids("aftervalue"), [])
        self.assertEqual(self.ids("preserve"), ["deleted"])
        self.assertTrue(self.k.doctor()["ok"])

    def test_oversize_rejected_without_partial_index(self):
        path = self.write("big")
        path.write_bytes(b"x" * (kb.MAX_FILE_BYTES + 1))
        with self.assertRaises(kb.KBError):
            self.k.sync()
        self.assertEqual(self.k.stats()["documents"], 0)

    def test_fts_and_sql_special_characters_are_not_executable(self):
        self.write("normal", title="needle")
        self.k.sync()
        self.assertEqual(self.ids('needle\" OR * - ( ) ; DROP TABLE docs; --'), ["normal"])
        self.assertEqual(self.k.stats()["documents"], 1)
        self.assertEqual(self.k.search('* () : " -')["results"], [])
        self.assertTrue(self.k.doctor()["ok"])

    def test_budget_json_including_escaping_and_utf8(self):
        for i in range(8):
            self.write(f"item-{i}", title='中文预算测试"\\' * 12,
                       summary="摘要中文预算测试" * 70, keywords="needle",
                       body='needle\\\t"内容' * 150)
        self.k.sync()
        for budget in [256, 512, 1000, 1800, 4000, 6000, 16000]:
            result = self.k.search("needle", k=8, budget=budget, explain=True)
            encoded = kb.json_bytes(result)
            self.assertLessEqual(len(encoded), budget)
            self.assertEqual(json.loads(encoded), result)
            self.assertLessEqual(len(result["results"]), 8)
        self.assertTrue(self.k.search("needle", k=8, budget=256)["budget_limited"])

    def test_get_budget_and_live_paths(self):
        self.write("entry", body="中文\\\n" * 1000)
        result = self.k.get("entry", budget=700)
        self.assertTrue(result["truncated"])
        self.assertLessEqual(len(kb.json_bytes(result)), 700)
        self.assertEqual(result["path"], str(self.root / "entry" / "brief.txt"))
        with self.assertRaises(kb.KBError):
            self.k.get("entry", section="secrets")
        with self.assertRaises(kb.KBError):
            self.k.get("missing")

    def test_argument_limits(self):
        self.k.sync()
        for k in [0, 51, -1]:
            with self.assertRaises(kb.KBError):
                self.k.search("query", k=k)
        for budget in [0, 255, 1048577]:
            with self.assertRaises(kb.KBError):
                self.k.search("query", budget=budget)

    def test_path_traversal_rejected(self):
        for slug in ["../outside", "/tmp/secret", "a/b", "a\\b", ".", "..", "C:evil", "bad\nname", ""]:
            with self.subTest(slug=slug), self.assertRaises(kb.KBError):
                self.k.get(slug)

    def make_symlink(self, target, link, directory=False):
        try:
            link.symlink_to(target, target_is_directory=directory)
        except (OSError, NotImplementedError):
            self.skipTest("This platform/account cannot create symlinks")

    def test_symlink_archive_not_imported(self):
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        (outside / "brief.txt").write_text(brief(keywords="outside"), encoding="utf-8")
        self.make_symlink(outside, self.root / "link", True)
        self.assertEqual(self.k.sync()["documents"], 0)
        with self.assertRaises(kb.KBError):
            self.k.get("link")
        with self.assertRaises(kb.KBError):
            self.k.sync(["link"])

    def test_symlink_brief_rejected(self):
        outside = Path(self.temp.name) / "outside.txt"
        outside.write_text(brief(), encoding="utf-8")
        (self.root / "link").mkdir()
        self.make_symlink(outside, self.root / "link" / "brief.txt")
        with self.assertRaises(kb.KBError):
            self.k.sync()

    def test_rebuild_and_rollback(self):
        self.write("entry", keywords="needle")
        self.k.sync()
        # Deliberately remove FTS entries only; doctor must detect the divergence.
        with self.k.connect(write=True) as conn:
            conn.execute("INSERT INTO docs_fts(docs_fts) VALUES('delete-all')")
        with self.assertRaises(sqlite3.DatabaseError):
            self.k.doctor()
        self.assertEqual(self.k.sync(full=True, rebuild=True)["added"], 1)
        self.assertEqual(self.ids("needle"), ["entry"])
        self.assertTrue(self.k.doctor()["ok"])
        bad = self.write("bad")
        bad.write_bytes(b"\xff")
        with self.assertRaises(kb.KBError):
            self.k.sync(full=True, rebuild=True)
        self.assertEqual(self.ids("needle"), ["entry"])
        self.assertTrue(self.k.doctor()["ok"])

    def test_legacy_index_is_ignored_and_sources_unchanged(self):
        path = self.write("entry", keywords="needle")
        legacy = self.root / "INDEX.md"
        legacy.write_bytes(b"indexonly \xff deliberately not UTF-8")
        before = (path.read_bytes(), legacy.read_bytes())
        self.k.sync()
        self.assertEqual(self.ids("indexonly"), [])
        self.k.sync(full=True, rebuild=True)
        self.k.get("entry")
        self.assertEqual((path.read_bytes(), legacy.read_bytes()), before)

    def test_directory_name_is_searchable(self):
        self.write("tool-needle", title="different", body="other")
        self.k.sync()
        self.assertEqual(self.ids("needle"), ["tool-needle"])

    def test_file_changed_during_read_is_rejected(self):
        path = self.write("entry")
        before = path.stat()
        after = SimpleNamespace(st_mode=before.st_mode, st_size=before.st_size,
                                st_mtime_ns=before.st_mtime_ns + 1)
        with patch.object(os, "fstat", side_effect=[before, after]):
            with self.assertRaises(kb.KBError):
                kb.read_file(path)

    def test_missing_fts5_has_actionable_error(self):
        original = kb.execute_statements
        def without_fts(conn, script):
            if "CREATE VIRTUAL TABLE" in script:
                raise sqlite3.OperationalError("no such module: fts5")
            return original(conn, script)
        with patch.object(kb, "execute_statements", side_effect=without_fts):
            with self.assertRaisesRegex(kb.KBError, "FTS5"):
                self.k.sync()

    def test_symlink_database_rejected(self):
        target = Path(self.temp.name) / "target.sqlite3"
        target.touch()
        link = Path(self.temp.name) / "link.sqlite3"
        self.make_symlink(target, link)
        with self.assertRaises(kb.KBError):
            kb.KnowledgeBase(self.root, link)

    def test_wrong_database_not_modified(self):
        other = Path(self.temp.name) / "other.sqlite3"
        conn = sqlite3.connect(other)
        conn.execute("CREATE TABLE important(x)")
        conn.execute("INSERT INTO important VALUES(42)")
        conn.commit()
        conn.close()
        before = other.read_bytes()
        wrong = kb.KnowledgeBase(self.root, other)
        with self.assertRaises(kb.KBError):
            wrong.sync()
        self.assertEqual(other.read_bytes(), before)

    def test_wrong_root_rejected(self):
        self.k.sync()
        another = Path(self.temp.name) / "another"
        another.mkdir()
        wrong = kb.KnowledgeBase(another, self.k.db)
        with self.assertRaises(kb.KBError):
            wrong.sync()
        with self.assertRaises(kb.KBError):
            wrong.search("hello")

    def test_external_local_database(self):
        self.write("entry", keywords="needle")
        external = kb.KnowledgeBase(self.root, Path(self.temp.name) / "cache" / "index.sqlite3")
        external.sync()
        self.assertEqual(external.search("needle")["results"][0]["id"], "entry")
        self.assertFalse(self.k.db.exists())

    def test_concurrent_writers(self):
        self.k.sync()
        for i in range(6):
            self.write(f"entry-{i}", keywords="needle")
        def sync_one(i):
            return kb.KnowledgeBase(self.root).sync([f"entry-{i}"])
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(sync_one, range(6)))
        self.assertEqual(sum(r["added"] for r in results), 6)
        self.assertEqual(self.k.stats()["documents"], 6)
        self.assertTrue(self.k.doctor()["ok"])

    def test_reader_snapshot_while_writing(self):
        self.write("entry", keywords="before")
        self.k.sync()
        with self.k.connect() as reader, kb.transaction(reader):
            old = reader.execute("SELECT title FROM docs").fetchone()[0]
            self.write("entry", title="newtitle", keywords="aftervalue")
            self.k.sync(["entry"])
            self.assertEqual(reader.execute("SELECT title FROM docs").fetchone()[0], old)
        self.assertEqual(self.ids("aftervalue"), ["entry"])

    def test_read_connection_rejects_writes(self):
        self.k.sync()
        with self.k.connect() as conn, self.assertRaises(sqlite3.OperationalError):
            conn.execute("DELETE FROM docs")


class CLITests(unittest.TestCase):
    def test_end_to_end_json_stdout_and_exit_codes(self):
        with tempfile.TemporaryDirectory() as root:
            command = [sys.executable, str(Path(kb.__file__).resolve()), "--root", root]
            result = subprocess.run(command + ["init"], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["documents"], 0)
            entry = Path(root) / "条目"
            entry.mkdir()
            (entry / "brief.txt").write_text(brief(keywords="中文 sqlite"), encoding="utf-8")
            result = subprocess.run(command + ["sync", "条目"], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            result = subprocess.run(command + ["search", "中文", "--budget-bytes", "512"], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertLessEqual(len(result.stdout), 512)
            self.assertEqual(result.stderr, b"")
            self.assertEqual(json.loads(result.stdout)["results"][0]["id"], "条目")
            result = subprocess.run(command + ["search", "sqlite", "-k", "0"], capture_output=True)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, b"")
            self.assertIn("error", json.loads(result.stderr))
            result = subprocess.run(command + ["get", "../escape"], capture_output=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn("error", json.loads(result.stderr))


if __name__ == "__main__":
    unittest.main()
