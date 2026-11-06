"""输出方言适配层 —— 让上层看见「Agent 正在做什么动作」。

为什么有这个包
==============

``worker.py`` 尾随日志只拿到字节流，``store.py`` 存的是事件，中间缺一层翻译：
不同 Agent CLI 的输出协议完全不一样（Anthropic 消息块 / Codex rollout 事件 /
MiniMax headless 事件包 / 纯文本）。本包把每种方言归一成同一个 ``LineEvent``，
上层就只面对「工具、命令、文件、错误、文本、目标」六种东西。

用法::

    from bridge.sentinel_core.adapters import detect_adapter, get_adapter

    # 已知是谁家的
    adapter = get_adapter("claude")
    # 不知道，按日志开头几个字段猜
    adapter = detect_adapter(open(log_path).read(4096))

    for line in stream.splitlines():
        event = adapter.parse(line)   # 永不抛异常，认不出就是 None
        if event is not None:
            store.add_event(kind=event.kind, summary=event.text, raw=event.raw)

两条约束
========

* **每次返回新实例**：``text`` 方言带状态（目标只认第一行），复用实例会让第二路
  会话永远等不到 goal。宁可多建一个对象，也不让状态跨会话泄漏。
* **永不抛异常**：这层跑在读取线程上（见 ``runner._PipeReader``），抛一次就可能
  带走整个输出收集。
"""

from __future__ import annotations

from collections.abc import Callable

from .base import Adapter, LineEvent
from .claude import ClaudeAdapter
from .codex import CodexAdapter
from .mcode import McodeAdapter
from .text import TextAdapter

__all__ = [
    "LIST_ADAPTERS",
    "Adapter",
    "LineEvent",
    "detect_adapter",
    "get_adapter",
]

#: 支持的方言名，按「结构越强越靠前」排列，最后一项是兜底。
LIST_ADAPTERS: tuple[str, ...] = ("claude", "codex", "mcode", "text")

_FACTORIES: dict[str, Callable[[], Adapter]] = {
    "claude": ClaudeAdapter,
    "codex": CodexAdapter,
    "mcode": McodeAdapter,
    "text": TextAdapter,
}

#: 检测时最多看多少行样本 —— 日志开头几十行就够认出方言了。
_DETECT_LINES = 50


def get_adapter(name: str) -> Adapter:
    """按名字取适配器，**未知名字退到 text**。

    :param name: 方言名（大小写与空白不敏感）。
    :return: 适配器新实例。
    """
    key = str(name or "").strip().lower()
    factory = _FACTORIES.get(key) or _FACTORIES["text"]
    return factory()


def detect_adapter(sample: str) -> Adapter:
    """按样本猜方言：先过 ``sniff``，再用「能解析出的行占比」打分取最高。

    只靠布尔 ``sniff`` 会在混合日志上犯错（三种结构化方言的字段偶尔互相出现），
    所以多一层打分：谁真的能把这批行翻译成事件，谁就最像。分数为 0 的一律不选 ——
    那说明它只是碰巧看见了某个字段名。

    唯一例外（``_structured_stream``）：样本**每一行都是 JSON 对象**、且只有一家
    说过「像」时，就用那一家。这是会话刚开头的真实形态 —— claude 的前几十行全是
    ``ai-title`` / ``agent-name`` / ``mode`` 这类元数据，一条消息都没有，这时按
    「命中率 0」退回纯文本，会把紧接着到来的 ``tool_use`` 全糊成文本。反过来，
    「文档里提到过 ``*** Begin Patch``」这种纯文本样本不会被这条救回来：它没有
    整篇 JSON 行，仍然走 text。

    :param sample: 日志样本（多行）。
    :return: 最像的适配器；都不像则为 text。
    """
    text = sample or ""
    best: Adapter | None = None
    best_score = 0.0
    sniffed: list[Adapter] = []
    for name in LIST_ADAPTERS:
        if name == "text":
            continue
        adapter = _FACTORIES[name]()
        try:
            if not adapter.sniff(text):
                continue
            score = _hit_rate(adapter, text)
        except Exception:  # noqa: BLE001 —— 检测失败等同于「不像」
            continue
        sniffed.append(adapter)
        if score > best_score:
            best, best_score = adapter, score
    if best is None and len(sniffed) == 1 and _structured_stream(text):
        best = sniffed[0]
    return best if best is not None else _FACTORIES["text"]()


def _structured_stream(sample: str) -> bool:
    """样本是不是「一行一个 JSON 对象」的流。

    :param sample: 样本。
    :return: 非空且不少于一半的行以 ``{`` 开头。
    """
    lines = [line.strip() for line in sample.splitlines() if line.strip()][:_DETECT_LINES]
    if not lines:
        return False
    objects = sum(1 for line in lines if line.startswith("{"))
    return objects * 2 >= len(lines)



def _hit_rate(adapter: Adapter, sample: str) -> float:
    """算这份样本里有多少行能被该方言翻译成事件。

    :param adapter: 候选适配器。
    :param sample: 样本。
    :return: 0~1 的命中率。
    """
    lines = [line for line in sample.splitlines() if line.strip()][:_DETECT_LINES]
    if not lines:
        return 0.0
    hits = 0
    for line in lines:
        try:
            if adapter.parse(line) is not None:
                hits += 1
        except Exception:  # noqa: BLE001 —— 单行解析失败只影响这一行
            continue
    return hits / len(lines)
