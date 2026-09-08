# 详细说明（演示）

此文件用于演示按需读取 experience.md，不进入默认全文索引。

```bash
python -S kb.py --root ~/knowledge search "sqlite 中文 检索" -k 5
```

查询无命中时先确认关键词与索引同步状态，再考虑换别名。
这里的唯一测试词 deep-only-example 不应该被默认搜索命中。
