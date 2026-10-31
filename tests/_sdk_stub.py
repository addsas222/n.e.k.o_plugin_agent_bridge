"""宿主 SDK（``plugin.sdk.plugin``）的最小替身 —— **只给测试用**。

为什么需要
----------
仓库根目录本身是一个 Python 包（有 ``__init__.py``），而 pytest 在推导测试模块名时
会把这个包导入一次。它顶部就 ``from plugin.sdk.plugin import ...``，于是**没有宿主
环境**的机器上（CI）直接 ``ModuleNotFoundError``。

本地一直没暴露，是因为检出目录名恰好是 ``agent_bridge``，被 ``conftest.py`` 里那个
命名空间桩挡住了；CI 的目录名是 ``n.e.k.o_plugin_agent_bridge``，桩不命中。

这个替身只解决"导入不炸"，不模拟任何行为：测试从来不实例化这个插件类。
生产/宿主环境永远走真实 SDK（见 ``__init__.py`` 里的 env 守卫），
缺 SDK 时仍然照常抛错，不会被这里静默吞掉。
"""

from __future__ import annotations


class SdkError(Exception):
    """替身：只用于 ``except SdkError``。"""


class NekoPluginBase:
    """替身基类：吸收 ``__init__(ctx)`` 与类级注册调用。"""

    def __init__(self, *args, **kwargs) -> None:
        self._stub_args = (args, kwargs)


class Ok(dict):
    """替身：真实实现是结果包装类型，这里只需可构造。"""


class Err(dict):
    """替身：同上。"""


def _passthrough(*args, **kwargs):
    """通用装饰器：原样返回被装饰对象。"""

    def wrap(obj):
        return obj

    return wrap


def neko_plugin(cls=None, **kwargs):
    """``@neko_plugin`` 既可裸用也可带参。"""
    if cls is None:
        return _passthrough()
    return cls


lifecycle = _passthrough
llm_tool = _passthrough
plugin_entry = _passthrough


class ui:
    """``ui`` 只用到 ``ui.context`` 与 ``ui.action`` 两个装饰器。"""

    context = staticmethod(_passthrough)
    action = staticmethod(_passthrough)
