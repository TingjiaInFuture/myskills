#!/usr/bin/env python3
"""Task Knowledge: files are truth; SQLite FTS5 is a disposable retrieval index.

Python 3.10+ with SQLite FTS5. No pip dependencies, network, model, or daemon.
All successful commands emit one UTF-8 JSON object. See README.md for usage.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
import unicodedata
from typing import Any, Iterator, Sequence

VERSION = "2.0.0"
SCHEMA_VERSION = 1
APPLICATION_ID = 0x544B4232  # TKB2; never overwrite an unrelated SQLite database.
DB_NAME = ".kb.sqlite3"
MAX_FILE_BYTES = 1024 * 1024
MAX_QUERY_CHARS = 2048
MAX_QUERY_GROUPS = 32
MAX_QUERY_TERMS = 128
DEFAULT_BUDGET = 6000
RANK_SPEC = "bm25(8.0,6.0,3.0,1.0)"  # title, keywords, summary, experience points


class KBError(Exception):
    """Actionable errors suitable for the command-line interface."""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def json_bytes(value: Any) -> bytes:
    """The budget includes serialization, JSON escaping, and the final newline."""
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":"),
                       allow_nan=False) + "\n").encode("utf-8")


def clip(text: str, length: int) -> str:
    if len(text) <= length:
        return text
    return text[: max(0, length - 1)] + ("…" if length else "")


def normalize(text: str) -> str:
    # Normalize full-width forms, case and accents on BOTH indexing and querying.
    text = unicodedata.normalize("NFKC", text).casefold()
    text = "".join(c for c in unicodedata.normalize("NFKD", text)
                   if not unicodedata.combining(c))
    # Preserve two common programming-language names instead of collapsing to C.
    text = re.sub(r"(?<![a-z0-9_])c\+\+(?![a-z0-9_])", " cplusplus ", text)
    return re.sub(r"(?<![a-z0-9_])c#(?![a-z0-9_])", " csharp ", text)


def is_han(c: str) -> bool:
    n = ord(c)
    return (0x3400 <= n <= 0x4DBF or 0x4E00 <= n <= 0x9FFF
            or 0xF900 <= n <= 0xFAFF or 0x20000 <= n <= 0x2FA1F
            or 0x30000 <= n <= 0x323AF)


def lexemes(text: str) -> Iterator[tuple[bool, str]]:
    """Yield Han runs and non-Han alphanumeric words; punctuation is a boundary."""
    buf: list[str] = []
    kind: bool | None = None
    for c in normalize(text):
        next_kind = True if is_han(c) else (False if c.isalnum() else None)
        if next_kind != kind or next_kind is None:
            if buf:
                yield bool(kind), "".join(buf)
                buf = []
            kind = next_kind
        if next_kind is not None:
            buf.append(c)
    if buf:
        yield bool(kind), "".join(buf)


def index_tokens(text: str) -> str:
    """Pre-tokenize Chinese without dictionaries or a native tokenizer plugin.

    Consecutive bigrams allow exact Chinese substring phrase queries, including
    two-character words. Unigrams are a separate trailing lane for single-char
    queries. The marker separates lanes/runs so a phrase cannot cross them.
    The ascii tokenizer preserves every non-ASCII character, including newer
    supplementary-plane Han. Case/accent normalization is done in Python.
    """
    tokens: list[str] = []
    for han, word in lexemes(text):
        if not han:
            tokens.append("w" + word)
            continue
        tokens.extend("b" + word[i:i + 2]
                      for i in range(len(word) - 1))
        tokens.append("x")  # Reserved boundary; never emitted as a query term.
        tokens.extend("u" + c for c in word)
        tokens.append("x")
    return " ".join(tokens)


@dataclass(frozen=True)
class QueryGroup:
    label: str
    tokens: tuple[str, ...]

    @property
    def expression(self) -> str:
        # Tokens contain only normalized alphanumerics, not user-supplied FTS syntax.
        return '"' + " ".join(self.tokens) + '"'


def compile_query(query: str, mode: str = "any") -> tuple[str, list[QueryGroup]]:
    if mode not in {"any", "all"}:
        raise KBError("mode 必须为 any 或 all")
    if len(query) > MAX_QUERY_CHARS:
        raise KBError("查询过长；请先提取 3–8 个关键词，用空格分隔")
    groups: list[QueryGroup] = []
    seen: set[tuple[str, ...]] = set()
    for han, word in lexemes(query):
        if han and len(word) > 1:
            tokens = tuple("b" + word[i:i + 2]
                           for i in range(len(word) - 1))
        elif han:
            tokens = ("u" + word,)
        else:
            tokens = ("w" + word,)
        if tokens not in seen:
            seen.add(tokens)
            groups.append(QueryGroup(word, tokens))
    if len(groups) > MAX_QUERY_GROUPS or sum(len(g.tokens) for g in groups) > MAX_QUERY_TERMS:
        raise KBError("查询词过多；请先提取 3–8 个关键词，用空格分隔")
    operator = " OR " if mode == "any" else " AND "
    return operator.join(g.expression for g in groups), groups


HEADER_KEYS = {
    "标题": "title", "title": "title", "摘要": "summary", "summary": "summary",
    "关键词": "keywords", "keywords": "keywords", "日期": "date", "date": "date",
    "状态": "status", "status": "status",
}
SKIP_SECTIONS = {"上下文引用", "相关存档", "更新记录", "context references",
                 "related archives", "changelog", "references", "history"}
ACTIVE = {"有效", "active", "valid", "current"}


@dataclass(frozen=True)
class Brief:
    title: str
    summary: str
    keywords: tuple[str, ...]
    status: str
    date: str
    body: str
    raw: str


def parse_brief(text: str, slug: str) -> Brief:
    """Read the original brief.txt format, including UTF-8 BOM and CRLF."""
    text = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    fields: dict[str, str] = {}
    body: list[str] = []
    in_header = True
    skip = False
    for line in text.splitlines():
        heading = re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", line.strip())
        if heading:
            label = heading.group(2).strip()
            if in_header and len(heading.group(1)) == 1:
                fields.setdefault("title", label)
                continue
            in_header = False
            skip = label.casefold() in SKIP_SECTIONS
            continue
        field = re.match(r"^\s*([^:：]+)[:：]\s*(.*?)\s*$", line)
        if in_header and field and field.group(1).strip().casefold() in HEADER_KEYS:
            fields[HEADER_KEYS[field.group(1).strip().casefold()]] = field.group(2)
        elif not skip and line.strip():
            body.append(line)
    keywords = tuple(k.strip() for k in re.split(r"[,，;；、]", fields.get("keywords", ""))
                     if k.strip())
    return Brief(
        title=fields.get("title") or slug,
        summary=fields.get("summary") or clip(body[0].lstrip("- *") if body else "", 160),
        keywords=keywords,
        status="active" if fields.get("status", "有效").strip().casefold() in ACTIVE else "inactive",
        date=fields.get("date", ""), body="\n".join(body), raw=text,
    )


TABLE_SQL = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE docs(
    id INTEGER PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    keywords TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active','inactive')),
    created_date TEXT NOT NULL,
    brief TEXT NOT NULL,
    mtime_ns INTEGER NOT NULL,
    size INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    indexed_at TEXT NOT NULL,
    title_terms TEXT NOT NULL,
    keyword_terms TEXT NOT NULL,
    summary_terms TEXT NOT NULL,
    body_terms TEXT NOT NULL
);
"""
FTS_SQL = """
CREATE VIRTUAL TABLE docs_fts USING fts5(
    title_terms, keyword_terms, summary_terms, body_terms,
    content='docs', content_rowid='id', tokenize='ascii'
);
CREATE TRIGGER docs_ai AFTER INSERT ON docs BEGIN
  INSERT INTO docs_fts(rowid,title_terms,keyword_terms,summary_terms,body_terms)
  VALUES(new.id,new.title_terms,new.keyword_terms,new.summary_terms,new.body_terms);
END;
CREATE TRIGGER docs_ad AFTER DELETE ON docs BEGIN
  INSERT INTO docs_fts(docs_fts,rowid,title_terms,keyword_terms,summary_terms,body_terms)
  VALUES('delete',old.id,old.title_terms,old.keyword_terms,old.summary_terms,old.body_terms);
END;
CREATE TRIGGER docs_au AFTER UPDATE OF title_terms,keyword_terms,summary_terms,body_terms ON docs BEGIN
  INSERT INTO docs_fts(docs_fts,rowid,title_terms,keyword_terms,summary_terms,body_terms)
  VALUES('delete',old.id,old.title_terms,old.keyword_terms,old.summary_terms,old.body_terms);
  INSERT INTO docs_fts(rowid,title_terms,keyword_terms,summary_terms,body_terms)
  VALUES(new.id,new.title_terms,new.keyword_terms,new.summary_terms,new.body_terms);
END;
"""


def execute_statements(conn: sqlite3.Connection, script: str) -> None:
    """Unlike executescript(), preserve the caller's transaction boundary."""
    statement = ""
    for line in script.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            conn.execute(statement)
            statement = ""
    if statement.strip():
        raise AssertionError("Incomplete internal SQL")


@contextmanager
def transaction(conn: sqlite3.Connection, write: bool = False) -> Iterator[None]:
    conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
    try:
        yield
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def valid_slug(slug: str) -> str:
    if (not slug or slug in {".", ".."} or slug.startswith(".")
            or "/" in slug or "\\" in slug or ":" in slug
            or any(ord(c) < 32 or ord(c) == 127 for c in slug)):
        raise KBError("存档 ID 必须为 KB_ROOT 下的单层文件夹名，不能包含路径或控制字符")
    return slug


def archive_file(root: Path, slug: str, filename: str = "brief.txt") -> Path:
    valid_slug(slug)
    directory = root / slug
    if directory.is_symlink():
        raise KBError(f"拒绝符号链接存档: {slug}")
    path = directory / filename
    if path.is_symlink():
        raise KBError(f"拒绝符号链接文件: {slug}/{filename}")
    return path


def regular_stat(path: Path) -> os.stat_result | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise KBError(f"只允许普通文件: {path}")
    return info


def read_file(path: Path) -> tuple[bytes, os.stat_result]:
    """Bounded read, reject symlink leafs, and detect concurrent file changes."""
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise KBError(f"只允许普通文件: {path}")
        if before.st_size > MAX_FILE_BYTES:
            raise KBError(f"文件超过 {MAX_FILE_BYTES} 字节；请缩短 brief 或拆分大文件: {path}")
        data = stream.read(MAX_FILE_BYTES + 1)
        after = os.fstat(stream.fileno())
    if len(data) > MAX_FILE_BYTES:
        raise KBError(f"文件过大: {path}")
    if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
        raise KBError(f"文件在读取时发生变化，请重试: {path}")
    return data, after


def decode_file(data: bytes, path: Path) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise KBError(f"文件必须为 UTF-8（可带 BOM），未更新索引: {path}") from exc


class KnowledgeBase:
    def __init__(self, root: str | Path, db: str | Path | None = None):
        self.root = Path(root).expanduser().resolve()
        # Do not silently follow a symlink database into an unrelated location.
        self.db = Path(db).expanduser().absolute() if db else self.root / DB_NAME
        if self.db.is_symlink():
            raise KBError("拒绝符号链接数据库；使用 --db 指向真正的本地文件")
        self.db = self.db.resolve()

    @contextmanager
    def connect(self, write: bool = False) -> Iterator[sqlite3.Connection]:
        if write:
            self.db.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.db), timeout=10, isolation_level=None)
        else:
            if not self.db.is_file():
                raise KBError("索引不存在；先运行 init 或 sync（仅首次全量建索引）")
            conn = sqlite3.connect(self.db.as_uri() + "?mode=ro", uri=True,
                                   timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout=10000")
            if write:
                # FULL durability; WAL supports same-host readers while syncing.
                with transaction(conn, write=True):
                    self._schema(conn, create=True)
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=FULL")
            else:
                conn.execute("PRAGMA query_only=ON")
                self._schema(conn, create=False)
            yield conn
        except sqlite3.OperationalError as exc:
            if "no such module: fts5" in str(exc).lower():
                raise KBError("此 Python 的 SQLite 未启用 FTS5；请换用启用 FTS5 的 Python 构建") from exc
            raise
        finally:
            conn.close()

    def _schema(self, conn: sqlite3.Connection, create: bool) -> None:
        app_id = conn.execute("PRAGMA application_id").fetchone()[0]
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if app_id == 0 and version == 0 and create:
            tables = conn.execute("SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
            if tables:
                raise KBError("目标不是 task-knowledge 数据库；拒绝修改，请选择其他 --db 路径")
            execute_statements(conn, TABLE_SQL)
            execute_statements(conn, FTS_SQL)
            conn.execute("INSERT INTO docs_fts(docs_fts,rank) VALUES('rank',?)", (RANK_SPEC,))
            conn.execute(f"PRAGMA application_id={APPLICATION_ID}")
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            conn.execute("INSERT INTO meta VALUES('root',?)", (str(self.root),))
            conn.execute("INSERT INTO meta VALUES('tokenizer','han-bigram-ascii-v1')")
        elif app_id != APPLICATION_ID or version != SCHEMA_VERSION:
            raise KBError("数据库标识/版本不兼容；请指定新 --db 路径重新 sync，勿覆盖其他数据库")
        root_row = conn.execute("SELECT value FROM meta WHERE key='root'").fetchone()
        if not root_row or root_row[0] != str(self.root):
            raise KBError("此索引属于另一个 KB_ROOT；搬迁后请用新的 --db 路径 sync，或离线移除旧索引后 init")

    @staticmethod
    def meta(conn: sqlite3.Connection, key: str) -> str | None:
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def archive_ids(self) -> Iterator[str]:
        # Deliberately only KB_ROOT/*/brief.txt. Never walk assets or references.
        with os.scandir(self.root) as entries:
            for entry in entries:
                if not entry.name.startswith(".") and entry.is_dir(follow_symlinks=False):
                    slug = valid_slug(entry.name)
                    path = archive_file(self.root, slug)
                    if regular_stat(path) is not None:
                        yield slug

    def sync(self, ids: Sequence[str] | None = None, full: bool = False,
             rebuild: bool = False) -> dict[str, Any]:
        if not self.root.is_dir():
            raise KBError("KB_ROOT 不存在或不是目录；首次创建请运行 init")
        if rebuild and ids:
            raise KBError("rebuild 不支持指定单个 ID")
        result: dict[str, Any] = {"added": 0, "updated": 0, "deleted": 0,
                                  "unchanged": 0, "rehashed": 0}
        with self.connect(write=True) as conn, transaction(conn, write=True):
            if rebuild:
                # Recreate the FTS table inside the SAME transaction, no live-file
                # replacement and no old -wal files attached to a new database.
                for trigger in ("docs_ai", "docs_ad", "docs_au"):
                    conn.execute(f"DROP TRIGGER {trigger}")
                conn.execute("DROP TABLE docs_fts")
                conn.execute("DELETE FROM docs")
                execute_statements(conn, FTS_SQL)
                conn.execute("INSERT INTO docs_fts(docs_fts,rank) VALUES('rank',?)", (RANK_SPEC,))
            seen: set[str] = set()
            timestamp = now()
            for slug in (dict.fromkeys(ids) if ids else self.archive_ids()):
                path = archive_file(self.root, slug)
                info = regular_stat(path)
                row = conn.execute("SELECT id,mtime_ns,size,sha256 FROM docs WHERE slug=?", (slug,)).fetchone()
                if info is None:
                    if row:
                        conn.execute("DELETE FROM docs WHERE id=?", (row["id"],))
                        result["deleted"] += 1
                    continue
                seen.add(slug)
                if row and not full and (row["mtime_ns"], row["size"]) == (info.st_mtime_ns, info.st_size):
                    result["unchanged"] += 1
                    continue
                data, info = read_file(path)
                digest = hashlib.sha256(data).hexdigest()
                result["rehashed"] += 1
                if row and digest == row["sha256"]:
                    # The UPDATE trigger is column-specific: no needless FTS write.
                    conn.execute("UPDATE docs SET mtime_ns=?,size=? WHERE id=?",
                                 (info.st_mtime_ns, info.st_size, row["id"]))
                    result["unchanged"] += 1
                    continue
                brief = parse_brief(decode_file(data, path), slug)
                title_text = brief.title if brief.title == slug else brief.title + " " + slug
                values = (slug, brief.title, brief.summary,
                          json.dumps(brief.keywords, ensure_ascii=False), brief.status,
                          brief.date, brief.raw, info.st_mtime_ns, info.st_size,
                          digest, timestamp, index_tokens(title_text),
                          index_tokens(" ".join(brief.keywords)), index_tokens(brief.summary),
                          index_tokens(brief.body))
                conn.execute("""
                    INSERT INTO docs(slug,title,summary,keywords,status,created_date,brief,
                      mtime_ns,size,sha256,indexed_at,title_terms,keyword_terms,summary_terms,body_terms)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(slug) DO UPDATE SET
                      title=excluded.title,summary=excluded.summary,keywords=excluded.keywords,
                      status=excluded.status,created_date=excluded.created_date,brief=excluded.brief,
                      mtime_ns=excluded.mtime_ns,size=excluded.size,sha256=excluded.sha256,
                      indexed_at=excluded.indexed_at,title_terms=excluded.title_terms,
                      keyword_terms=excluded.keyword_terms,summary_terms=excluded.summary_terms,
                      body_terms=excluded.body_terms
                """, values)
                result["updated" if row else "added"] += 1
            if not ids:
                # The scan must complete before pruning; any exception rolls back
                # ALL changes. A bad file can never silently purge good entries.
                stale = [row[0] for row in conn.execute("SELECT slug FROM docs") if row[0] not in seen]
                conn.executemany("DELETE FROM docs WHERE slug=?", ((slug,) for slug in stale))
                result["deleted"] += len(stale)
                conn.execute("INSERT OR REPLACE INTO meta VALUES('last_full_sync',?)", (timestamp,))
            conn.execute("INSERT OR REPLACE INTO meta VALUES('index_updated_at',?)", (timestamp,))
            result["documents"] = conn.execute("SELECT count(*) FROM docs").fetchone()[0]
            result["index_updated_at"] = timestamp
        return result

    def search(self, query: str, k: int = 5, mode: str = "any", all_status: bool = False,
               budget: int = DEFAULT_BUDGET, explain: bool = False) -> dict[str, Any]:
        if not 1 <= k <= 50:
            raise KBError("k 必须在 1–50 之间")
        validate_budget(budget)
        expression, groups = compile_query(query, mode)
        with self.connect() as conn, transaction(conn):
            output: dict[str, Any] = {"results": [],
                "index_updated_at": self.meta(conn, "index_updated_at"), "budget_limited": False}
            if not expression:
                return output
            # Do not fetch the whole corpus or re-rank a truncated candidate set.
            # Status filtering precedes LIMIT. FTS's rank uses the weights above.
            sql = """
                SELECT d.slug,d.title,d.summary,d.brief,d.status,docs_fts.rank AS score,
                       d.title_terms,d.keyword_terms,d.summary_terms,d.body_terms
                FROM docs_fts JOIN docs d ON d.id=docs_fts.rowid
                WHERE docs_fts MATCH ?
            """ + ("" if all_status else " AND d.status='active'") + " ORDER BY docs_fts.rank LIMIT ?"
            rows = conn.execute(sql, (expression, k)).fetchall()
            for row in rows:
                fields = [" " + row[name] + " " for name in
                          ("title_terms", "keyword_terms", "summary_terms", "body_terms")]
                hits = [g.label for g in groups
                        if any(" " + " ".join(g.tokens) + " " in text for text in fields)]
                brief = parse_brief(row["brief"], row["slug"])
                item: dict[str, Any] = {
                    "id": row["slug"], "title": clip(row["title"], 100),
                    "path": str(self.root / row["slug"] / "brief.txt"),
                    "summary": clip(row["summary"], 160),
                    "snippet": make_snippet(brief.body, hits),
                    "matched": [clip(h, 48) for h in hits[:8]],
                }
                if all_status:
                    item["status"] = row["status"]
                if explain:
                    item["bm25"] = row["score"]  # Lower (usually negative) is better.
                output["results"].append(item)
            if explain:
                output["query"] = {"keywords": [g.label for g in groups], "mode": mode,
                                   "weights": [8, 6, 3, 1]}
        return fit_search(output, budget)

    def get(self, slug: str, section: str = "brief", budget: int = 12000) -> dict[str, Any]:
        validate_budget(budget)
        if section not in {"brief", "experience"}:
            raise KBError("section 必须为 brief 或 experience")
        filename = "brief.txt" if section == "brief" else "experience.md"
        path = archive_file(self.root, slug, filename)
        if regular_stat(path) is None:
            raise KBError(f"文件不存在: {path}")
        data, _ = read_file(path)
        text = decode_file(data, path)
        output = {"id": slug, "path": str(path), "content": text, "truncated": False}
        if len(json_bytes(output)) <= budget:
            return output
        output.update(content="", truncated=True)
        if len(json_bytes(output)) > budget:
            raise KBError("budget-bytes 小于路径等必要元数据的大小，请提高预算")
        low, high = 0, len(text)
        while low < high:
            middle = (low + high + 1) // 2
            output["content"] = clip(text, middle)
            if len(json_bytes(output)) <= budget:
                low = middle
            else:
                high = middle - 1
        output["content"] = clip(text, low)
        return output

    def stats(self) -> dict[str, Any]:
        with self.connect() as conn, transaction(conn):
            counts = conn.execute("SELECT count(*),coalesce(sum(status='active'),0) FROM docs").fetchone()
            return {"root": str(self.root), "db": str(self.db), "documents": counts[0],
                    "active": counts[1], "index_updated_at": self.meta(conn, "index_updated_at"),
                    "last_full_sync": self.meta(conn, "last_full_sync"),
                    "sqlite": sqlite3.sqlite_version, "schema": SCHEMA_VERSION,
                    "db_bytes": self.db.stat().st_size}

    def doctor(self) -> dict[str, Any]:
        # FTS5's official external-content integrity check is an INSERT command.
        # It needs a writer connection, but does not edit knowledge files.
        if not self.db.is_file():
            raise KBError("索引不存在；先运行 init 或 sync")
        with self.connect(write=True) as conn, transaction(conn, write=True):
            checks = [row[0] for row in conn.execute("PRAGMA quick_check")]
            if checks != ["ok"]:
                raise KBError("SQLite quick_check 失败: " + "; ".join(checks))
            conn.execute("INSERT INTO docs_fts(docs_fts,rank) VALUES('integrity-check',1)")
            return {"ok": True, "sqlite": sqlite3.sqlite_version,
                    "python": sys.version.split()[0], "fts5": True,
                    "sqlite_check": "ok", "fts_content_check": "ok"}


def validate_budget(budget: int) -> None:
    if not 256 <= budget <= 1024 * 1024:
        raise KBError("budget-bytes 必须在 256–1048576 之间（包括 JSON 和换行）")


def make_snippet(body: str, hits: Sequence[str], limit: int = 240) -> str:
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    if not lines:
        return ""
    # Only top-K rows reach here. Choose the line covering the most query words.
    # This is presentation only, not a second retrieval/ranking algorithm.
    best = max(enumerate(lines),
               key=lambda pair: (sum(hit in normalize(pair[1]) for hit in hits), -pair[0]))[1]
    normalized = normalize(best)
    positions = [normalized.find(hit) for hit in hits if hit in normalized]
    start = max(0, min(positions) - 50) if positions else 0
    # NFKC can change offsets slightly; this excerpt is indicative, not highlighting.
    return ("…" if start else "") + clip(best[start:], limit - bool(start))


def fit_search(output: dict[str, Any], budget: int) -> dict[str, Any]:
    """Keep valid JSON and higher-ranked entries; never cut serialized JSON."""
    if len(json_bytes(output)) <= budget:
        return output
    output["budget_limited"] = True
    # Prefer preserving K paths over long snippets. Fields remain stable in JSON.
    for field in ("snippet", "summary"):
        for length in (120, 60, 0):
            for item in output["results"]:
                item[field] = clip(item[field], length)
            if len(json_bytes(output)) <= budget:
                return output
    # Optional diagnostics must not consume the retrieval budget by themselves.
    output.pop("query", None)
    while output["results"] and len(json_bytes(output)) > budget:
        output["results"].pop()
    if len(json_bytes(output)) > budget:
        raise KBError("budget-bytes 过小，请提高预算")
    return output


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=VERSION)
    parser.add_argument("--root", default=os.environ.get("KNOWLEDGE_ROOT", "~/knowledge"),
                        help="知识库目录；默认 KNOWLEDGE_ROOT 或 ~/knowledge")
    parser.add_argument("--db", help="本地 SQLite 索引路径；默认 KB_ROOT/.kb.sqlite3")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="创建知识库目录，并导入已有 brief.txt")
    sync = sub.add_parser("sync", help="增量同步；指定 ID 时不扫描其他目录")
    sync.add_argument("ids", nargs="*", help="一个或多个存档文件夹名；省略则扫描全库并清理缺失项")
    sync.add_argument("--full", action="store_true", help="忽略 mtime/size 快速判断，重新计算全部目标哈希")
    sub.add_parser("rebuild", help="从 brief.txt 事务性重建可打开数据库的 FTS 索引")
    search = sub.add_parser("search", help="仅查询 SQLite，不扫描目录；输出预算内的 Top-K JSON")
    search.add_argument("query", help="3–8 个关键词，空格分隔；不是 SQL/FTS 表达式")
    search.add_argument("-k", type=int, default=5, help="最多返回多少条，1–50，默认 5")
    search.add_argument("--mode", choices=("any", "all"), default="any", help="关键词间 OR / AND")
    search.add_argument("--all-status", action="store_true", help="同时检索过时、草稿等非有效存档")
    search.add_argument("--budget-bytes", type=int, default=DEFAULT_BUDGET, help="完整 UTF-8 JSON 字节上限，默认 6000")
    search.add_argument("--explain", action="store_true", help="附带 BM25 分数和查询权重")
    get = sub.add_parser("get", help="只读指定存档的源文件；不自动跟随上下文引用")
    get.add_argument("id")
    get.add_argument("--section", choices=("brief", "experience"), default="brief")
    get.add_argument("--budget-bytes", type=int, default=12000)
    sub.add_parser("stats", help="索引条数、大小、同步时间和版本")
    sub.add_parser("doctor", help="SQLite + FTS5 外部内容一致性检查")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        kb = KnowledgeBase(args.root, args.db)
        if args.command == "init":
            kb.root.mkdir(parents=True, exist_ok=True)
            output = kb.sync()
        elif args.command == "sync":
            output = kb.sync(args.ids, full=args.full)
        elif args.command == "rebuild":
            output = kb.sync(full=True, rebuild=True)
        elif args.command == "search":
            output = kb.search(args.query, args.k, args.mode, args.all_status, args.budget_bytes, args.explain)
        elif args.command == "get":
            output = kb.get(args.id, args.section, args.budget_bytes)
        elif args.command == "stats":
            output = kb.stats()
        else:
            output = kb.doctor()
        sys.stdout.buffer.write(json_bytes(output))
        return 0
    except (KBError, OSError, sqlite3.Error, ValueError) as exc:
        sys.stderr.buffer.write(json_bytes({"error": str(exc)}))
        return 2
    except KeyboardInterrupt:
        sys.stderr.buffer.write(json_bytes({"error": "已中断；未提交的数据库事务已回滚"}))
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
