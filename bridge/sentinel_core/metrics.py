"""Token 消耗审计 —— 统计 Agent 烧掉多少，以及拦下多少。

为什么值得单独做
================

Agent 的成本是**事后才可见**的：一个跑偏的子任务能在一小时里烧掉正常一天的量，
而你往往是在账单出来时才知道。这一层把两件事记在账上：

* **挽回**：被拦下的任务本来会消耗多少（用被拦文本的体量估一个下限）。**这一项在
  生产链路上真的在跑** —— 每次拦截都调 :meth:`TokenLedger.record_blocked`。
* **消耗**：这一路会话大致用了多少输入/输出 Token。**只有调用方回传时才有**：
  :meth:`TokenLedger.record` 是唯一写 ``input_tokens`` / ``output_tokens`` 的入口，
  而当前生产链路一次都没调它（只给了 ``saved_tokens``）。所以实测 ``total_tokens``
  恒为 0，:func:`render_usage` 把它渲染成「未统计」而不是「约 0」——
  **把「没测」说成「没花」是错的**，后者会让人以为这套审计覆盖了成本。

关于精度，说清楚
================

这里的数字是**估算**，不是账单。原因：

* 宿主网关不一定回传 usage（流式响应常常没有）；
* Agent CLI 的中间轮次无法逐个统计，只能按输出体量倒推；
* 中英混排的分词比例不稳定。

所以对外一律标 ``estimated=True``。宁可被当成粗略指标，也不假装它是计量表。
估算规则：CJK 字符按 1 字 ≈ 1 token，其余按 4 字符 ≈ 1 token —— 这是社区经验值，
对中英混排的误差通常在 20% 以内。

**推论**：真正需要「消耗准」的时候，得先在宿主侧拿到 usage 再调 ``record``；
在那之前，``total_tokens`` 的正确读法是「未统计」，不是「0」。
"""

from __future__ import annotations

import time
from typing import Any

__all__ = [
    "TokenLedger",
    "estimate_tokens",
    "render_usage",
]

#: 非 CJK 字符每 token 的字符数。
_CHARS_PER_TOKEN = 4.0

#: 估算时单条文本的上限（再长的文本按这个截断估算，避免一次几 MB 的扫描）。
_MAX_SCAN_CHARS = 200_000


def estimate_tokens(text: str) -> int:
    """粗估一段文本的 Token 数。

    :param text: 文本。
    :return: 估算 Token 数（空文本为 0）。
    """
    if not text:
        return 0
    sample = text[:_MAX_SCAN_CHARS]
    cjk = 0
    other = 0
    for char in sample:
        # CJK 统一表意文字 + 全角标点：按字计
        if "\u4e00" <= char <= "\u9fff" or "\u3000" <= char <= "\u303f" or "\uff00" <= char <= "\uffef":
            cjk += 1
        elif char.strip():
            other += 1
    return int(cjk + other / _CHARS_PER_TOKEN)


def _fmt_tokens(value: int) -> str:
    """把 Token 数格式化成易读形式。

    :param value: Token 数。
    :return: 形如 ``12.3k`` 的字符串。
    """
    number = int(value)
    if number >= 1_000_000:
        return f"{number / 1_000_000:.2f}M"
    if number >= 1_000:
        return f"{number / 1_000:.1f}k"
    return str(number)


class TokenLedger:
    """Token 账本。

    :ivar store: 底层 ``EventStore``（可为 ``None``，此时只在内存里累计）。
    """

    def __init__(self, store: Any = None) -> None:
        self.store = store
        self._input = 0
        self._output = 0
        self._saved = 0
        self._calls = 0

    def record(
        self,
        *,
        agent: str = "",
        model: str = "",
        fingerprint: str = "",
        prompt: str = "",
        output: str = "",
        input_tokens: int = 0,
        output_tokens: int = 0,
        saved_tokens: int = 0,
    ) -> dict[str, int]:
        """记一次用量。

        可以直接给 ``prompt`` / ``output`` 让本层估算，也可以给精确的 token 数
        （给了就不估算那一项）。

        :param agent: Agent 标识。
        :param model: 模型名。
        :param fingerprint: 会话指纹。
        :param prompt: 输入文本（用于估算）。
        :param output: 输出文本（用于估算）。
        :param input_tokens: 精确输入 Token（优先于估算）。
        :param output_tokens: 精确输出 Token。
        :param saved_tokens: 本次挽回的估算值。
        :return: 本次记录的 ``{"input", "output", "saved"}``。
        """
        got_input = int(input_tokens) if input_tokens else estimate_tokens(prompt)
        got_output = int(output_tokens) if output_tokens else estimate_tokens(output)
        got_saved = int(saved_tokens)

        self._input += got_input
        self._output += got_output
        self._saved += got_saved
        self._calls += 1

        if self.store is not None:
            self.store.add_tokens(
                agent=agent,
                model=model,
                fingerprint=fingerprint,
                input_tokens=got_input,
                output_tokens=got_output,
                saved_tokens=got_saved,
            )
        return {"input": got_input, "output": got_output, "saved": got_saved}

    def record_blocked(self, *, agent: str = "", fingerprint: str = "", task: str = "") -> int:
        """记一次「被拦下而省下」的估算。

        拦在派发前时，Agent 完全没启动，省下的是整轮成本。用一个保守下限：
        任务文本本身的 Token 数 × 输出系数。

        :param agent: Agent 标识。
        :param fingerprint: 会话指纹。
        :param task: 被拦下的任务文本。
        :return: 估算省下的 Token 数。
        """
        base = estimate_tokens(task)
        saved = int(base * 3)
        self.record(agent=agent, fingerprint=fingerprint, saved_tokens=saved)
        return saved

    def summary(self, *, since: float | None = None) -> dict[str, Any]:
        """取汇总。

        :param since: 起始时间戳；给了就只汇总这之后（需要 ``store``）。
        :return: 汇总字典，``estimated`` 恒为 ``True``。
        """
        if self.store is not None and since is not None:
            stats = self.store.stats(since=since)
            tin = int(stats.get("input_tokens") or 0)
            tout = int(stats.get("output_tokens") or 0)
            saved = int(stats.get("saved_tokens") or 0)
        else:
            tin, tout, saved = self._input, self._output, self._saved
        return {
            "estimated": True,
            "input_tokens": tin,
            "output_tokens": tout,
            "total_tokens": tin + tout,
            "saved_tokens": saved,
            "calls": self._calls,
            "since": since,
        }


def render_usage(summary: dict[str, Any]) -> str:
    """把用量汇总渲染成一行可读文本。

    ``total_tokens`` 为 0 时**不渲染成「约 0」**：这个版本没有任何地方回传真实用量
    （见模块 docstring），0 表示「没测」而不是「没花」。同理，没有分母时不编百分比。

    :param summary: ``TokenLedger.summary`` 的返回值。
    :return: 文本。
    """
    if not summary:
        return "Token：无记录"
    total = int(summary.get("total_tokens") or 0)
    saved = int(summary.get("saved_tokens") or 0)
    if total:
        head = f"Token 约 {_fmt_tokens(total)}（输入 {_fmt_tokens(int(summary.get('input_tokens') or 0))}"
        head += f" / 输出 {_fmt_tokens(int(summary.get('output_tokens') or 0))}，估算值）"
    else:
        head = "Token 消耗：未统计（本版本只跟踪「拦下挽回」，不跟踪实际消耗）"
    if saved:
        if total:
            ratio = (saved / (total + saved)) * 100
            head += f"，拦下挽回约 {_fmt_tokens(saved)}（{ratio:.0f}%）"
        else:
            # 没有实测消耗就没有分母，硬算比例会得出「100%」这种无意义的数。
            head += f"，拦下挽回约 {_fmt_tokens(saved)}"
    since = summary.get("since")
    if since:
        try:
            stamp = time.strftime("%m-%d %H:%M", time.localtime(float(since)))
            head += f"，自 {stamp} 起"
        except (ValueError, OSError):
            pass
    return head
