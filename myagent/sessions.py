"""
会话隔离（AgentPool）

为什么需要这层：
`Agent.messages`（agent.py:134）是实例属性，承载对话历史。
`web.py` 里的 `AGENT` 是模块级单例 —— 一个人用网页时没问题，
但一旦对外提供接口，两个调用方会共用同一份 messages：

  A：「帮我看下这份合同」
  B：「刚才说的那个条款再解释一下」   ← B 读到的是 A 的上下文

更糟的是 `self.messages.append` 没有加锁，两个请求同时进来会把
user/assistant 消息交错写进同一个列表，模型看到的就是一段乱序对话。

这里按 session_id 分配实例，划分边界：
- 共享 LLM / RAG / 长期记忆：知识库和记忆本来就该跨会话（那是产品能力）
- 隔离 messages：对话上下文属于会话边界，不该串
- 同一会话加锁串行：一轮没跑完不能被下一轮插进来
- 跨会话并行：并发能力来自这里
- TTL 与容量上限回收：避免长时间运行后内存无限涨

不引入任何框架，就是一把锁 + 一个有序字典。
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable

DEFAULT_SESSION = "default"


@dataclass
class _Slot:
    """一个会话槽位：一个 Agent 实例 + 它自己的锁"""

    agent: Any
    lock: threading.Lock = field(default_factory=threading.Lock)
    last_used: float = field(default_factory=time.time)
    turns: int = 0
    created_at: float = field(default_factory=time.time)


class AgentPool:
    """
    按 session_id 管理 Agent 实例。

    factory: 无参可调用对象，返回一个新的 Agent（每次调用都要给新实例，
             传共享单例会退化成改动前的行为）。
    max_sessions: 最多保留多少个会话，超出按最久未用淘汰。
    ttl: 空闲多久回收（秒），0 表示不按时间回收。
    """

    def __init__(
        self,
        factory: Callable[[], Any],
        max_sessions: int = 16,
        ttl: float = 1800.0,
    ):
        if not callable(factory):
            raise TypeError("factory 必须是可调用对象，例如 lambda: Agent(...)")
        self._factory = factory
        self._max = max(1, int(max_sessions))
        self._ttl = max(0.0, float(ttl))
        self._slots: OrderedDict[str, _Slot] = OrderedDict()
        self._lock = threading.Lock()
        self.created = 0
        self.evicted = 0

    # ---------- 内部 ----------

    @staticmethod
    def _norm(session_id: str | None) -> str:
        sid = (session_id or "").strip()
        return sid or DEFAULT_SESSION

    def _reap_locked(self) -> None:
        """回收空闲过久的会话。只回收当前没在跑的，正在执行的不动。"""
        if self._ttl <= 0:
            return
        deadline = time.time() - self._ttl
        for sid in [s for s, slot in self._slots.items() if slot.last_used < deadline]:
            slot = self._slots[sid]
            # 拿不到锁说明这一轮还在跑，跳过
            if slot.lock.acquire(blocking=False):
                try:
                    self._slots.pop(sid, None)
                    self.evicted += 1
                finally:
                    slot.lock.release()

    # ---------- 对外 ----------

    def get(self, session_id: str | None = None) -> _Slot:
        """取会话槽位，不存在则新建。"""
        sid = self._norm(session_id)
        with self._lock:
            slot = self._slots.get(sid)
            if slot is not None:
                self._slots.move_to_end(sid)
                return slot

            self._reap_locked()

            if len(self._slots) >= self._max:
                # 淘汰最久未使用的
                old_sid, old_slot = self._slots.popitem(last=False)
                self.evicted += 1
                _ = old_sid, old_slot

            slot = _Slot(agent=self._factory())
            self._slots[sid] = slot
            self.created += 1
            return slot

    def run(
        self,
        session_id: str | None,
        text: str,
        on_step: Callable[[str], None] | None = None,
    ) -> str:
        """在同一会话内串行执行一轮，跨会话并行。"""
        slot = self.get(session_id)
        with slot.lock:
            slot.last_used = time.time()
            slot.turns += 1
            try:
                return slot.agent.run(text, on_step=on_step)
            finally:
                slot.last_used = time.time()

    def each(self, fn: Callable[[Any], Any]) -> int:
        """
        对池中每个存活实例执行一次，返回成功个数。

        切换模型、改风格时用：这些设置是进程级的，
        但状态散落在每个 Agent 实例上，得逐个同步。
        """
        with self._lock:
            slots = list(self._slots.values())
        n = 0
        for slot in slots:
            try:
                fn(slot.agent)
                n += 1
            except Exception:
                pass
        return n

    def agents(self) -> list:
        with self._lock:
            return [s.agent for s in self._slots.values()]

    def history(self, session_id: str | None) -> list[dict]:
        """只读快照，便于排查串会话。"""
        sid = self._norm(session_id)
        with self._lock:
            slot = self._slots.get(sid)
        if not slot:
            return []
        return list(getattr(slot.agent, "messages", []))

    def drop(self, session_id: str | None) -> bool:
        """结束一个会话（失败时的显式清理入口）。"""
        sid = self._norm(session_id)
        with self._lock:
            return self._slots.pop(sid, None) is not None

    def reset(self) -> int:
        """清空全部会话，返回清掉的个数。重启服务前用。"""
        with self._lock:
            n = len(self._slots)
            self._slots.clear()
            return n

    def stats(self) -> dict:
        with self._lock:
            return {
                "sessions": len(self._slots),
                "max_sessions": self._max,
                "ttl_seconds": self._ttl,
                "created_total": self.created,
                "evicted_total": self.evicted,
                "ids": list(self._slots.keys())[:32],
            }
