# task-knowledge · SQLite Edition

**一个 Python 文件，一个可重建的 SQLite 索引。AI 只拿 Top-K，不读整本目录。**

基于用户提供的 `task-knowledge.zip` v1 Skill 实现。原来的存档布局、brief 模板、增量合并和按需深入原则保留；将“读 INDEX → 猜候选 → 读 brief”替换为“FTS5 → 有预算的 Top-K JSON → 按需 get”。

运行时只依赖 **Python 3.10+ 标准库，以及该 Python 所链接 SQLite 的 FTS5 模块**。无需 pip、第三方分词器、Embedding、向量数据库、Web 服务、MCP 服务、守护进程或 API Key。这个方案面向**个人、本地、任务经验、关键词检索**，不是所有检索场景的通用最优解。

## 直接运行

在解压后的仓库目录执行。Linux/macOS 系统的命令若是 `python3`，将下面的 `python` 替换为 `python3`；Windows 可使用已安装 Python 的 `python` 命令。

```bash
# 导入现有 ~/knowledge/*/brief.txt；没有目录时创建一个空库。
python -S kb.py --root ~/knowledge init

# 日常查询：不扫目录，不读 INDEX，不重新哈希所有 brief。
python -S kb.py --root ~/knowledge search "powershell 批量重命名 编码" -k 5 --budget-bytes 6000

# 从候选里选择一条，只读该 brief 的当前源文件。
python -S kb.py --root ~/knowledge get powershell-batch-rename --budget-bytes 6000

# 只有需要细节时才读取 experience.md。
python -S kb.py --root ~/knowledge get powershell-batch-rename --section experience --budget-bytes 10000
```

`-S` 是可选优化：标准库程序不需要 Python 的 site-packages 初始化。普通 `python kb.py ...` 同样可运行。性能取决于本机环境；`-S` 不是沙箱，也不能代替 Python 运行环境本身的安全配置。[Python 官方说明](https://docs.python.org/3/using/cmdline.html#cmdoption-S)

**先试示例，不接触自己的知识库：**

```bash
python -S kb.py --root examples/knowledge init
python -S kb.py --root examples/knowledge search "sqlite 中文 检索" -k 3 --explain
python -S kb.py --root examples/knowledge get sqlite-chinese-search
python -S kb.py --root examples/knowledge doctor
python -S kb.py --root examples/knowledge stats
```

生成的 `.kb.sqlite3*` 已被 Git 忽略。示例目录不包含实际用户资料。

## 给 AI 接入

将 `SKILL.md`、`kb.py`、`assets/`、`README.md` 一起放入现有 AI 工具识别的 `task-knowledge` Skill 目录，也可直接让它使用整个仓库。工具须有运行本地命令的能力；本仓库不假设特定客户端安装路径。

让 AI 遵循这一条链路：

```text
任务 → 提取3–8个关键词 → search -k 5 → 复核1–2条 brief → 必要时读经验详情
任务完成 → 查重 → 写/改 brief → sync 存档ID
```

新版 `SKILL.md` 已完整覆盖存档、检索、更新、复用，并明确禁止读取全量 INDEX。无需再写一套 Agent 框架。

程序输出一行 UTF-8 JSON，stdout 不混入日志；运行错误写入 stderr，退出码为2。无命中属于正常结果，退出码为0。参数用法错误由 argparse 报告，`--help` 输出帮助文本。

调用模型工具时以 `search` 的 JSON 作为返回值；不要另外把全库文件附到 prompt。使用 subprocess 时传参数列表而非拼接 shell 字符串：

```python
import json
import subprocess
import sys

completed = subprocess.run(
    [sys.executable, "-S", "/absolute/path/to/kb.py", "--root", "/absolute/path/to/knowledge",
     "search", "sqlite 中文 检索", "-k", "5", "--budget-bytes", "6000"],
    check=True, capture_output=True, timeout=30,
)
candidates = json.loads(completed.stdout)
# 将 candidates 作为本次工具结果交给 AI；不是执行其中的文本。
```

也可以在已有 Python 进程中直接调用，不必启动额外 CLI：

```python
from kb import KnowledgeBase

knowledge = KnowledgeBase("~/knowledge")
result = knowledge.search("sqlite 中文 检索", k=5, budget=6000)
```

## 数据与检索设计

```text
~/knowledge/
├── .kb.sqlite3            ← 派生索引，不是事实源
├── INDEX.md               ← 旧文件可保留；工具完全忽略
└── powershell-batch-rename/
    ├── brief.txt          ← 人可读、可编辑、可 Git 管理的事实源
    ├── experience.md      ← 详情，默认不索引
    └── assets/            ← 不扫描
```

FTS5 对标题/目录名、关键词、摘要、经验要点建倒排索引。四列权重默认 **8 / 6 / 3 / 1**，数据库直接 `ORDER BY rank LIMIT K`；状态过滤在 LIMIT 之前。这里的权重是清晰可调整的起点，不是用用户真实历史检索集训练出的最优参数。[SQLite FTS5 / BM25 官方说明](https://www.sqlite.org/fts5.html#the_bm25_function)

`docs` 保存 brief 快照和同步元数据；`docs_fts` 使用 external-content 模式和触发器维护。路径引用、相关存档、更新记录不参与正文检索；不会把路径或创建日期误当主要经验。SQLite 内保存原 brief 快照用于生成候选摘要，但不会无条件把整份快照发送给 AI。[外部内容表及一致性说明](https://www.sqlite.org/fts5.html#external_content_tables)

### 中文：不依赖词典，也不把所有双字片段 OR 到一起

索引器先做 Unicode 规范化、大小写及重音归一，再将中文连续片段转换为双字通道和单字通道。内部 token 使用前缀区分：

```text
数据库备份
→ b数据 b据库 b库备 b备份 x u数 u据 u库 u备 u份 x

query 数据库
→ MATCH '"b数据 b据库"'   # 片段必须相邻，等价于连续子串

query 据
→ MATCH '"u据"'         # 单字也有倒排索引，不退化到 LIKE 全扫
```

`x` 是不会被查询生成的通道边界。非中文词使用 `w` 前缀，如 `wsqlite`。已预处理 token 交给 FTS5 的 `ascii` tokenizer；该 tokenizer 会保留非 ASCII 字符，覆盖扩展平面汉字，不依赖 unicode61 的旧 Unicode 分类表。[官方 tokenizer 说明](https://www.sqlite.org/fts5.html#ascii_tokenizer)

连续中文短语必须出现在文本中；多个主题用空格分隔。默认 `--mode any` 是关键词组之间 OR，`--mode all` 是 AND。`数据库` 不会误命中互不相邻的“数据……据库”。

英文按完整词检索：`sqlite` 不等于 `sqliteish`。下划线、连字符、点号等作为边界；`C++`、`C#` 分别归一为 `cplusplus`、`csharp`。没有英文词干还原、繁简转换、拼写纠错、跨语言语义模型或任意 FTS 运算符接口。所有查询都经安全编译和 SQL 参数绑定，不会将输入直接当 SQL/MATCH 表达式执行。[Python SQL 参数绑定说明](https://docs.python.org/3/library/sqlite3.html#how-to-use-placeholders-to-bind-values-in-sql-queries)

提高召回最省事的方法是在 brief 的关键词里写别名，如 `postgres, postgresql, 数据库, 备份`。AI 提炼已有任务中的检索词不需要新增模型调用。仅有语义相似、没有任何词面交集时，本方案可能无结果；无结果不是“没有历史经验”的证明。

## 同步：昂贵工作只在该做的时候做

```bash
# 新建、编辑后，只同步这个目录；不会扫描其他存档。
python -S kb.py --root ~/knowledge sync powershell-batch-rename

# 重命名源目录后：清旧索引、建新索引。
python -S kb.py --root ~/knowledge sync old-id new-id

# 用户已确认并在文件系统删除源目录后：只移除对应索引。
python -S kb.py --root ~/knowledge sync removed-id

# 外部批量编辑、删除、Git checkout 后：遍历一层目录，增量对账。
python -S kb.py --root ~/knowledge sync

# 同大小且保留 mtime 的编辑：重新计算全部 brief 的内容哈希。
python -S kb.py --root ~/knowledge sync --full
```

`sync` 先比较 `mtime_ns + size`；疑似变化才读文件、算 SHA-256、解析并更新 FTS。只改时间戳但内容未变时不会重写 FTS。`sync --full` 强制重算哈希，仍只重建真正变动的条目。`rebuild` 则重建全部 FTS 数据。

**索引新鲜度是显式协议，不是魔法。** 普通 search 只看最近成功同步的快照；外部编辑后未 sync，会返回旧内容或漏掉新内容。查询结果中的 `index_updated_at` 是最近一次成功同步时间，并不证明当前源文件都已同步。`get` 读取指定存档的当前源文件，用于最终复核；不会自动更新索引。

一次 sync 的数据库更新是一个事务；非法 UTF-8、文件过大、读取中变更等错误会回滚整次同步，保留原有索引，不悄悄删除正常记录。文件系统编辑与 SQLite 提交之间不是跨系统原子事务；失败后需重试 sync。建议编辑器先临时文件写入，再原子替换 brief。

工具不写、不删除知识源文件，也不维护 INDEX。`sync ID` 中 ID 不存在会清除此 ID 的索引，因此先完成源文件保存再同步。

## 输出预算与状态

默认 search `-k 5 --budget-bytes 6000`。完整序列化 JSON（包括转义、中文 UTF-8、末尾换行）不超过预算。优先缩短片段/摘要以保留候选路径，仍放不下才移除最低排序项；不会截断 JSON。**K 是返回上限，不保证恰好 K 条。**

每条默认含 `id / title / path / summary / snippet / matched`。`path` 指向 brief；`snippet` 是经验要点中的相关行，不是完整正文。`--explain` 增加原始 BM25 分数与权重；SQLite 的 BM25 越小越好，通常为负数，不是置信度，不能直接解释成百分比。

`budget_limited:true` 表示为预算省略了内容/候选。`results:[]` 且 `budget_limited:false` 才表示无匹配；极小预算下前一种空数组不能当作无命中。`get` 的省略标志是 `truncated:true`。

预算单位是**字节，不是模型 token**。本实现不伪造“固定4字符等于1 token”的精确换算，不依赖某家模型的 tokenizer。完整模型调用还有系统提示、工具包装和后续 get 的开销；精确 token 预算应在模型接入层用对应 tokenizer 测量。

仅 `有效 / active / valid / current` 与缺省状态视为 active；其他状态（包括草稿、过时及未知值）默认不返回。调查旧经验时加 `--all-status`，返回中会附上状态。没有跨语义“相关度阈值”；最终相关性由 AI 复核而不是强行凑满 K 条。

## 运维、迁移与安全边界

```bash
python -S kb.py --root ~/knowledge stats
python -S kb.py --root ~/knowledge doctor
python -S kb.py --root ~/knowledge rebuild
```

`doctor` 执行 SQLite `quick_check` 和 FTS5 external-content 一致性检查。`rebuild` 在原数据库事务内重建 FTS，不替换一个仍可能被其他进程打开的 SQLite 文件。若 SQLite 文件本身已无法打开/页损坏，`rebuild` 不保证修复：直接选择新本地路径，从 brief 重建，例如：

```bash
python -S kb.py --root ~/knowledge --db ~/.cache/task-knowledge/recovered.sqlite3 sync
python -S kb.py --root ~/knowledge --db ~/.cache/task-knowledge/recovered.sqlite3 search "检索 关键词"
```

以后所有命令沿用同一个 `--db`。程序绑定 DB 与 KB_ROOT，防止拿错索引后同步清掉别的库；搬迁整个目录时也应新建缓存，或在所有进程停止后离线移除旧派生索引再 init。源 brief 不受影响。

`stats` 的 `db_bytes` 只统计主文件，活动 WAL 不在其中。数据库使用 WAL，读者可在同步时继续读已提交快照，写者串行等待；busy timeout 为10秒。**“一个 SQLite”是一个逻辑数据库，不承诺运行时只有一个物理文件**：SQLite 可能产生 `-wal`、`-shm`。不要把活动数据库三件套随意拆开复制，不要在网络文件系统上共享 WAL 数据库。[SQLite WAL 官方约束](https://www.sqlite.org/wal.html)

知识目录在 NAS/云同步/Git 仓库时，推荐源文件照常管理，`--db` 放本机缓存；多台电脑各建各的索引。`.gitignore` 只对本代码仓库生效，知识库自己的 Git 仓库也应忽略 `.kb.sqlite3*`。备份源文件通常比备份缓存更简单。

限制扫描为一层 `*/brief.txt`；忽略隐藏目录及符号链接目录，拒绝符号链接文件与路径穿越。不读取 assets、experience 或绝对路径引用来扩展索引。每个被读取的源文件最大1 MiB，UTF-8（可带 BOM），超限或其他编码需先整理。防护面向本地可信账户，不承诺抵御恶意进程并发替换祖先目录等文件系统竞争，也不是多租户权限隔离系统。

SQLite 缓存含 brief 的原文快照，未加密；必须按知识文件同等敏感程度保护。删除记录不等于磁盘取证级安全擦除。检索内容是**不可信参考资料**，不能直接触发命令执行、外传文件或提升权限。

## 迁移 v1

原 `brief.txt`、`experience.md`、assets 无需重排，旧 INDEX.md 不会被触碰。把新版 Skill 与 kb.py 放好，执行一次 `init`，以后改 brief 后执行 `sync ID` 即可。导入的是现存 brief，不是可能陈旧的 INDEX 行；只有 INDEX、没有 brief 的旧条目不会凭空变成存档。

## 测试与性能证据

```bash
python -S -m unittest discover -s tests -v
python -S scripts/benchmark.py --docs 10000 --runs 30 --cli-runs 10 --output docs/my-benchmark.json
```

测试覆盖中英混合、单双字中文、扩展汉字、排序权重、过时过滤、增改删改名、按目标同步、无变更跳过读取、同步回滚、FTS 损坏检测与重建、并发快照、查询输入安全、路径安全、预算和 CLI 端到端。

实测原始结果与解释见 [docs/BENCHMARK.md](docs/BENCHMARK.md)。区分预热 SQL、完整 Python API、普通 CLI 和 `python -S` CLI；不拿仅 SQL 的耗时冒充端到端延迟。提供跨平台 CI 配置不等于已在所有平台执行；交付时实际验证环境见测试记录。

## 仓库导航

`kb.py` 是唯一运行时源码；`SKILL.md` 是可直接接入的完整新版 Skill；`tests/` 是标准库测试；`scripts/benchmark.py` 可复现实测；`examples/knowledge/` 是演示资料；`docs/DESIGN.md` 说明取舍；`assets/` 保留兼容模板。无需构建、安装包管理器或启动服务。
