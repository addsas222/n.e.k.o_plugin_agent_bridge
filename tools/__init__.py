"""tools 包标记 —— 让脚本可以用 `python -m tools.<模块>` 运行。

为什么需要：脚本里注入 sys.path 会让 import 落到代码之后（E402），
而 CI 的 ruff 用 `--ignore-noqa --isolated`，行内 noqa 与 ruff.toml 都不生效。
变成包以后 `python -m` 会把仓库根放进 sys.path，源码里一个 hack 都不用。
"""
