# myskills

个人自创 Agent Skills 仓库。每个子目录是一个独立技能，遵循 [Agent Skills 规范](https://agentskills.io/)：技能目录必含 `SKILL.md`（YAML frontmatter + Markdown 指令），另可按需携带 `assets/`（模板/静态资源）、`scripts/`（可执行脚本）、`references/`（参考文档）。

## Skills 一览

| Skill | 简介 |
|---|---|
| [task-knowledge](./task-knowledge/) | 任务经验沉淀复用知识库 v2：brief.txt 为事实源 + 本地 SQLite FTS5 关键词 Top-K 检索，AI 不再读全量 INDEX |

---

## task-knowledge — 任务经验沉淀复用（SQLite Edition）

### 简介

把每次成功任务的**可复用经验 + 最少完整上下文**沉淀为 `~/knowledge/` 下相互独立的存档（每个存档一个文件夹，`brief.txt` 为总览入口），并在后续任务中检索复用。

v2 的核心变化：**文件保存知识，SQLite 负责检索**。`brief.txt`/`experience.md` 布局与模板兼容 v1；检索入口从"读全量 INDEX.md → 猜候选"替换为"提炼 3–8 个关键词 → 单文件 `kb.py` 走 SQLite FTS5 加权 BM25 → 预算内的 Top-K JSON"。AI 不读全量索引、不在查询前扫描整库，也不需要 pip 安装任何依赖（仅需 Python 3.10+ 及其内置 SQLite 的 FTS5 支持）。设计细节见 [task-knowledge/docs/DESIGN.md](./task-knowledge/docs/DESIGN.md)。

**四个功能**：

| 功能 | 触发时机 | 核心动作 |
|---|---|---|
| 1 存档 | 任务成功执行完毕 | search 查重 → 写 `~/knowledge/<凝练标题>/brief.txt` → `sync <存档ID>` 更新索引（不再登记 INDEX.md） |
| 2 检索 | 给出一段任务描述 | 提炼关键词 → `search` 取 Top-K 候选 → `get <ID>` 复核 brief |
| 3 更新 | 存档时发现已有相关存档 | 增量合并 brief（而非新建）→ 追加更新记录 → `sync <存档ID>` |
| 4 复用 | 新任务开始 | 检索相关存档 → 应用已验证做法、规避已记录的坑 |

**目录结构**（完整项目，运行时只需 `SKILL.md` + `kb.py` + `assets/` + `README.md`）：

```
task-knowledge/
├── SKILL.md                  # 主文件：四大功能的完整执行指令（v2.0.0）
├── kb.py                     # 单文件 CLI：init / search / get / sync / doctor / rebuild
├── assets/                   # brief 模板等静态资源
├── docs/                     # DESIGN / VALIDATION / BENCHMARK 与改造报告
├── examples/knowledge/       # 合成示例库，可安全试跑
├── tests/                    # 44 项单测（unittest，零第三方依赖）
├── scripts/                  # 基准脚本
└── .github/workflows/        # Linux/Windows/macOS × Python 3.10/3.13 CI
```

**数据位置**：经验库存于 `~/knowledge/`（Windows 下即 `C:\Users\<用户名>\knowledge\`）。索引是根目录下可重建的本地缓存 `.kb.sqlite3`（WAL 模式，可能伴随 `-wal`/`-shm`）；v1 的 `INDEX.md` 不再读写，保留在原处作历史参考。

### 使用方法

**一**：把 `task-knowledge/` 复制（或符号链接）到智能体的 skills 目录。

```powershell
# Claude Code（复制）
Copy-Item -Recurse "<本仓库路径>\task-knowledge" "$HOME\.claude\skills\"

# Claude Code（符号链接，仓库更新后无需再复制；需管理员或开发者模式）
New-Item -ItemType SymbolicLink -Path "$HOME\.claude\skills\task-knowledge" -Target "<本仓库路径>\task-knowledge"
```

技能目录必须连同 `kb.py` 与 `assets/` 一起部署；`tests/`、`examples/`、`docs/` 仅开发用，可不复制。之后智能体按任务自动触发，也可显式调用 `/task-knowledge 存档`、`/task-knowledge 检索 <关键词>`、`/task-knowledge 复用 <任务描述>`。

常用命令（进入 `task-knowledge/` 目录）：

```bash
python -S kb.py --root ~/knowledge init                              # 首次导入/重建索引
python -S kb.py --root ~/knowledge search "关键词1 关键词2" -k 5      # 日常检索
python -S kb.py --root ~/knowledge get <存档ID>                      # 读候选 brief
python -S kb.py --root ~/knowledge sync <存档ID>                     # 改动后定向同步
python -S kb.py --root ~/knowledge doctor                            # 一致性自检
```

**从 v1 升级**：对已有 `~/knowledge/` 执行一次 `init` 即完成迁移，存量 `brief.txt` 无需重排；此后只维护 brief + `sync`，不再登记 `INDEX.md`。

**二**：在你的智能体的全局提示文件中（`~/.claude/CLAUDE.md`、`~/.codex/AGENTS.md`）添加：

> 在任务开始前和结束后使用 skills/task-knowledge

含义：任务开始前 → 执行"复用"（检索并应用相关经验）；任务成功结束后 → 执行"存档/更新"（沉淀本次经验并 `sync`）。若智能体不支持 skills 机制，它会把 `SKILL.md` 当普通指令文件读取执行，同样有效；路径请按本仓库实际存放位置调整。

---

## 新增 skill

- 一个技能一个文件夹，文件夹名与 frontmatter 的 `name` 一致（小写字母、数字、连字符，≤64 字符）。
- 必含 `SKILL.md`：frontmatter 写 `name`（必填）、`description`（必填，说明"做什么 + 何时用"，含触发关键词），正文写执行指令（建议 <500 行）。
- 模板/静态资源放 `assets/`，可执行脚本放 `scripts/`，参考文档放 `references/`。
- 新增后在上面"Skills 一览"表和正文各补一段简介与使用方法。
