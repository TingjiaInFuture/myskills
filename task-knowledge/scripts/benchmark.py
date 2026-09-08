#!/usr/bin/env python3
"""Reproducible synthetic benchmark; no external packages and no API calls.

Example: python scripts/benchmark.py --docs 10000 --runs 40 --cli-runs 12
All input files and the database live in a temporary directory and are removed.
Engine timings reuse a connection; CLI timings include a new interpreter each run.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import platform
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import kb

TOPICS = [
    ("sqlite", "中文检索", "使用双字片段建立全文索引并按关键词查询"),
    ("powershell", "批量重命名", "先预览改名结果并避免编码错误"),
    ("docker", "容器网络", "检查端口映射并验证服务连通性"),
    ("git", "仓库拆分", "保留提交记录并核验分支引用"),
    ("python", "文件编码", "显式指定编码并使用原子替换写入文件"),
    ("nginx", "反向代理", "检查转发配置并验证请求头"),
    ("postgres", "数据库备份", "校验备份文件并演练恢复流程"),
    ("linux", "磁盘诊断", "检查磁盘使用量并保留必要日志"),
    ("typescript", "类型检查", "将接口约束与运行时校验分开"),
    ("redis", "缓存失效", "设置过期策略并验证一致性"),
]


def timed(fn: Callable):
    start = time.perf_counter()
    result = fn()
    return time.perf_counter() - start, result


def latency_ms(values):
    ordered = sorted(v * 1000 for v in values)
    return {"median": round(statistics.median(ordered), 3),
            "p95": round(ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)], 3),
            "samples": len(ordered)}


def run(n: int, runs: int, cli_runs: int):
    with tempfile.TemporaryDirectory(prefix="task-kb-bench-") as temporary:
        root = Path(temporary) / "knowledge"
        root.mkdir()
        legacy_index_bytes = 0
        source_bytes = 0
        generated_at = time.perf_counter()
        for i in range(n):
            topic, title, solution = TOPICS[i % len(TOPICS)]
            slug = f"archive-{i:06d}"
            directory = root / slug
            directory.mkdir()
            data = (f"标题: {topic}-{title}-{i}\n摘要: {solution}\n"
                    f"关键词: {topic}, {title}, needle{i}, shared\n"
                    "日期: 2026-01-01\n状态: 有效\n\n## 可复用经验\n"
                    f"- {solution}。先在测试目录验证，再应用到生产环境。\n"
                    "- shared 通用经验：记录实际输入、验证输出、保留回滚方法。\n"
                    "- 遇到失败先确认根因，避免重复执行破坏性命令。\n\n"
                    "## 上下文引用\n/example/local/path — 合成示例，不读取此路径\n"
                    "## 更新记录\n2026-01-01 创建合成样本\n").encode("utf-8")
            (directory / "brief.txt").write_bytes(data)
            source_bytes += len(data)
            legacy_index_bytes += len((f"- {slug} — {solution} 【关键词: {topic}, {title}, needle{i}, shared】\n").encode("utf-8"))
        generation_s = time.perf_counter() - generated_at
        print(f"Generated {n} briefs in {generation_s:.3f}s", file=sys.stderr, flush=True)
        database = kb.KnowledgeBase(root)
        build_s, build_result = timed(database.sync)
        print(f"Initial sync: {build_s:.3f}s", file=sys.stderr, flush=True)
        unchanged_s, unchanged_result = timed(database.sync)
        target = root / "archive-000000" / "brief.txt"
        original = target.read_text(encoding="utf-8")
        target.write_text(original.replace("sqlite-", "sqlite-updated-", 1), encoding="utf-8")
        update_s, update_result = timed(lambda: database.sync(["archive-000000"]))
        queries = {"selective": f"needle{n - 1}", "topic": "sqlite 中文检索", "broad": "shared"}
        measurements = {}
        query_plan = []
        sql = """SELECT d.slug,docs_fts.rank FROM docs_fts JOIN docs d ON d.id=docs_fts.rowid
                 WHERE docs_fts MATCH ? AND d.status='active' ORDER BY docs_fts.rank LIMIT 5"""
        with database.connect() as conn:
            for name, query in queries.items():
                print(f"Measuring {name}", file=sys.stderr, flush=True)
                expression, _ = kb.compile_query(query)
                for _ in range(5):
                    conn.execute(sql, (expression,)).fetchall()
                engine_times = [timed(lambda: conn.execute(sql, (expression,)).fetchall())[0]
                                for _ in range(runs)]
                # Full Python API opens/closes a connection and creates snippets/JSON data.
                api_times = [timed(lambda: database.search(query))[0] for _ in range(runs)]
                cli_times = []
                no_site_times = []
                output_bytes = 0
                for _ in range(cli_runs):
                    elapsed, result = timed(lambda: subprocess.run(
                        [sys.executable, str(Path(kb.__file__).resolve()), "--root", str(root),
                         "search", query, "-k", "5", "--budget-bytes", "6000"],
                        capture_output=True, check=True, timeout=30))
                    cli_times.append(elapsed)
                    output_bytes = len(result.stdout)
                    assert output_bytes <= 6000
                    assert json.loads(result.stdout)["results"]
                    elapsed, fast_result = timed(lambda: subprocess.run(
                        [sys.executable, "-S", str(Path(kb.__file__).resolve()), "--root", str(root),
                         "search", query, "-k", "5", "--budget-bytes", "6000"],
                        capture_output=True, check=True, timeout=30))
                    no_site_times.append(elapsed)
                    assert json.loads(fast_result.stdout) == json.loads(result.stdout)
                count = conn.execute("SELECT count(*) FROM docs_fts WHERE docs_fts MATCH ?",
                                     (expression,)).fetchone()[0]
                measurements[name] = {"query": query, "matching_documents": count,
                    "sql_warm_ms": latency_ms(engine_times), "python_api_ms": latency_ms(api_times),
                    "fresh_cli_default_ms": latency_ms(cli_times),
                    "fresh_cli_no_site_ms": latency_ms(no_site_times), "output_bytes": output_bytes}
            expression, _ = kb.compile_query(queries["topic"])
            query_plan = [list(row) for row in conn.execute("EXPLAIN QUERY PLAN " + sql, (expression,))]
        stats = database.stats()
        print("Checking FTS integrity", file=sys.stderr, flush=True)
        assert database.doctor()["ok"]
        print("Benchmark complete; cleaning temporary files", file=sys.stderr, flush=True)
        return {
            "created_at_utc": kb.now(), "dataset": "synthetic 10-topic Chinese/English briefs, not a relevance benchmark",
            "environment": {"python": sys.version.split()[0], "sqlite": sqlite3.sqlite_version,
                            "platform": platform.platform(), "machine": platform.machine(),
                            "logical_cpu_count": os.cpu_count(), "storage": "container temporary directory; OS cache not flushed"},
            "documents": n, "generation_seconds": round(generation_s, 4),
            "initial_sync_seconds": round(build_s, 4), "initial_sync": build_result,
            "unchanged_scan_seconds": round(unchanged_s, 4), "unchanged_scan": unchanged_result,
            "single_update_seconds": round(update_s, 4), "single_update": update_result,
            "source_brief_bytes": source_bytes, "database_bytes": stats["db_bytes"],
            "legacy_index_bytes": legacy_index_bytes, "queries": measurements,
            "query_plan": query_plan,
            "notes": ["SQL timers reuse one connection and warm caches.",
                      "API timers include connection setup and result snippets.",
                      "CLI timers include a fresh Python process, connection, query, and UTF-8 JSON output.",
                      "-S disables site initialization; the application has only standard-library dependencies.",
                      "Byte counts are not model-specific token counts.",
                      "A ubiquitous term can require scoring most/all matching documents; LIMIT is not O(1).",
                      "No cold-cache or retrieval-quality claim is made."],
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docs", type=int, default=10000)
    parser.add_argument("--runs", type=int, default=40)
    parser.add_argument("--cli-runs", type=int, default=12)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.docs < 10 or args.runs < 1 or args.cli_runs < 1:
        parser.error("docs >= 10, runs >= 1, cli-runs >= 1")
    result = run(args.docs, args.runs, args.cli_runs)
    text = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    sys.stdout.buffer.write(text.encode("utf-8"))


if __name__ == "__main__":
    main()
