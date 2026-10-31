"""本机 Agent 桥接 —— N.E.K.O 原生插件（包入口）。

实现全部在 ``_plugin.py``。这里只做一件事：**在包上下文里**把插件类转发出去，
让宿主那条 ``plugin.plugins.agent_bridge:AgentBridgePlugin`` 仍然成立。

为什么需要这层壳
----------------
仓库根目录本身就是 Python 包（宿主把整个仓库当作 ``agent_bridge`` 挂载）。
但 pytest 推导测试模块名时，会把根目录的 ``__init__.py`` **当作独立文件**导入
（``__package__ == ""``）—— 那种状态下相对导入与宿主 SDK 都不可用，
于是没有宿主环境的机器（CI）直接报 ``ModuleNotFoundError: plugin``。

所以 ``__package__`` 为空时这里什么都不做，让"导入这个文件"变成一次无害操作。
插件定义一行都不在这儿，宿主行为不受影响。
"""

from __future__ import annotations

if __package__:
    from ._plugin import AgentBridgePlugin

    __all__ = ["AgentBridgePlugin"]
else:  # pragma: no cover —— 只在 pytest 推导模块名的那一瞬间发生
    __all__: list[str] = []
