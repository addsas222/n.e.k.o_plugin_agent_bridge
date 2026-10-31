"""宿主 SDK（``plugin.sdk.plugin``）的最小替身 —— **只给测试用**。

为什么需要：仓库根目录本身是 Python 包，pytest 推导测试模块名时会把
``__init__.py`` 当**独立模块**导入（``__package__ == ""``），那里必然要 import
宿主 SDK，而没有宿主的机器（CI）就会 ``ModuleNotFoundError: plugin``。

这个替身只解决"导入不炸"，不模拟任何行为 —— 测试从不实例化插件类。
生产/宿主环境永远走真实 SDK：``__init__.py`` 只在独立导入时才启用它。
"""

from __future__ import annotations


class SdkError(Exception):
    """替身：只用于 ``except SdkError``。"""


class NekoPluginBase:
    """替身基类。"""

    def __init__(self, *args, **kwargs) -> None:
        self._stub_args = (args, kwargs)


class Ok(dict):
    """替身：真实实现是结果包装类型。"""


class Err(dict):
    """替身：同上。"""


def _passthrough(*args, **kwargs):
    def wrap(obj):
        return obj

    return wrap


def neko_plugin(cls=None, **kwargs):
    if cls is None:
        return _passthrough()
    return cls


lifecycle = _passthrough
llm_tool = _passthrough
plugin_entry = _passthrough


class ui:
    context = staticmethod(_passthrough)
    action = staticmethod(_passthrough)
