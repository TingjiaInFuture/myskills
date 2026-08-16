# myskills

个人自创 Agent Skills 仓库。每个子目录是一个独立技能，遵循 [Agent Skills 规范](https://agentskills.io/)：技能目录必含 `SKILL.md`（YAML frontmatter + Markdown 指令），另可按需携带 `assets/`（模板/静态资源）、`scripts/`（可执行脚本）、`references/`（参考文档）。

## Skills 一览

| Skill | 简介 |
|---|---|
| [task-knowledge](./task-knowledge/) | 任务经验沉淀复用知识库：存档 / 检索 / 更新 / 复用 |

---

## task-knowledge — 任务经验沉淀复用

### 简介

把每次成功任务的**可复用经验 + 最少完整上下文**沉淀为 `~/knowledge/` 下相互独立的存档（每个存档一个文件夹，`brief.txt` 为总览入口），并在后续任务中快速检索与复用。存档文字尽量简要，上下文以绝对 path 引用原文件而不复制。

**四个功能**：

| 功能 | 触发时机 | 核心动作 |
|---|---|---|
| 1 存档 | 任务成功执行完毕 | 查重 → 新建 `~/knowledge/<凝练标题>/brief.txt`（摘要/关键词/结论级经验/上下文 path）→ 登记索引 |
| 2 检索 | 给出一段任务描述 | 扫 `INDEX.md` 初筛 → 只读候选 `brief.txt` 复核 → 输出最相关的几个 |
| 3 更新 | 存档时发现已有相关存档 | 增量合并（而非新建）→ 追加更新记录 → 同步索引 |
| 4 复用 | 新任务开始时 | 检索相关存档 → 应用已验证做法、规避已记录的坑 |

**目录结构**：

```
task-knowledge/
├── SKILL.md                  # 主文件：四大功能的完整执行指令
└── assets/
    ├── brief-template.txt    # 单个存档入口文件（brief.txt）模板
    └── index-template.md     # 知识库总索引（INDEX.md）模板
```

**数据位置**：经验库存于 `~/knowledge/`（Windows 下即 `C:\Users\<用户名>\knowledge\`），结构为 `INDEX.md` + 各存档文件夹，首次使用自动初始化。

### 使用方法

**一**：把 `task-knowledge/` 复制（或符号链接）到智能体的 skills 目录。

```powershell
# Claude Code（复制）
Copy-Item -Recurse "<本仓库路径>\task-knowledge" "$HOME\.claude\skills\"

# Claude Code（符号链接，仓库更新后无需再复制；需管理员或开发者模式）
New-Item -ItemType SymbolicLink -Path "$HOME\.claude\skills\task-knowledge" -Target "<本仓库路径>\task-knowledge"
```

之后智能体会按任务自动触发，也可显式调用：`/task-knowledge 存档`、`/task-knowledge 检索 <任务描述>`、`/task-knowledge 复用 <任务描述>`。

**二**：在你的智能体的全局提示文件中（`~/.claude/CLAUDE.md`、`~/.codex/AGENTS.md`）添加：

> 在任务开始前和结束时使用 skills/task-knowledge

含义：任务开始前 → 执行"复用"（检索并应用相关经验）；任务成功结束后 → 执行"存档/更新"（沉淀本次经验）。若智能体不支持 skills 机制，它会把 `SKILL.md` 当普通指令文件读取执行，同样有效；路径请按本仓库实际存放位置调整。

---

## 新增 skill

- 一个技能一个文件夹，文件夹名与 frontmatter 的 `name` 一致（小写字母、数字、连字符，≤64 字符）。
- 必含 `SKILL.md`：frontmatter 写 `name`（必填）、`description`（必填，说明"做什么 + 何时用"，含触发关键词），正文写执行指令（建议 <500 行）。
- 模板/静态资源放 `assets/`，可执行脚本放 `scripts/`，参考文档放 `references/`。
- 新增后在上面"Skills 一览"表和正文各补一段简介与使用方法。
