# 旧版索引说明

v2 不再使用 INDEX.md 作为 AI 检索入口，也不要求维护此文件。

旧 INDEX.md 可以保留作人工笔记；工具不会读取、改写或删除它。
迁移依据是 KB_ROOT/*/brief.txt，而不是这里的条目。

```bash
python -S /实际路径/kb.py --root ~/knowledge init
python -S /实际路径/kb.py --root ~/knowledge search "关键词 短语" -k 5
```
