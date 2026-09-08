# 交付验证

日期：2026-09-08。

实际环境：Python 3.13.5；SQLite 3.46.1；Linux-6.18.35-x86_64-with-glibc2.41。

已执行 `python -S -m unittest discover -s tests -v`：44项全部通过。原始输出见 [tests-linux-python313.txt](tests-linux-python313.txt)。

已执行示例库 init、search、get/相关自动化测试、stats、doctor；已执行10,000条合成基准，详见 [BENCHMARK.md](BENCHMARK.md)。未访问或修改用户真实的 ~/knowledge。

FTS5 缺失分支使用 mock 验证错误提示；实际交付环境自身支持 FTS5。符号链接测试在本次 Linux 环境实际运行，未被跳过。其余平台的运行情况未知，不以 CI 配置替代实测。

上传原包 SHA-256：`ec63bef6f0d7853bb82f83b8736de28fd525cd57869ea1106b66d6f533b8b0ed`。

数据库、WAL、SHM 与 Python 编译缓存不包含在交付包中。示例库在使用处执行 init 即可重建，因此不存在绑定交付环境路径的缓存被错误复用问题。
