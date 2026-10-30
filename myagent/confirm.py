"""
人工确认网关（Human-in-the-loop）

核心设计：
Agent 调用任何敏感工具时，不直接执行，而是先产生一个待确认请求。
只有拿到明确的 "yes" 才真正执行；超时或拒绝 → 返回拒绝结果给模型。

在终端里：打印请求，等用户输入 y/n
在 Gradio 里：前端弹确认框，用队列阻塞等待
"""

import json
import os
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

# 需要人工确认的工具
SENSITIVE_TOOLS = {
    "read_local_file",
    "ingest_local_file",
    "list_local_files",
    "analyze_document",
}

# 确认超时（秒），超时视为拒绝
DEFAULT_TIMEOUT = 120


@dataclass
class PendingRequest:
    id: str
    tool: str
    args: dict
    reason: str
    created_at: float = field(default_factory=time.time)
    _event: threading.Event = field(default_factory=threading.Event)
    _result: bool = False

    def approve(self):
        self._result = True
        self._event.set()

    def deny(self):
        self._result = False
        self._event.set()

    def wait(self, timeout: float = DEFAULT_TIMEOUT) -> bool:
        return self._event.wait(timeout) and self._result


class ConfirmGateway:
    """
    确认网关。两种模式：
    - terminal：命令行交互（供 main.py 使用）
    - callback：外部回调（供 web.py 使用）
    - auto_deny：无交互环境，一律拒绝
    """

    def __init__(self, mode: str = "terminal", timeout: float = DEFAULT_TIMEOUT):
        self.mode = mode
        self.timeout = timeout
        self._pending: dict[str, PendingRequest] = {}
        self._lock = threading.Lock()
        # 外部可注册回调，用于在 UI 上展示待确认请求
        self.on_request: Callable[[PendingRequest], None] | None = None
        # 本次会话已被拒绝的工具+参数，避免反复打扰
        self._denied_cache: set[str] = set()

    def _key(self, tool: str, args: dict) -> str:
        return f"{tool}:{json.dumps(args, sort_keys=True, ensure_ascii=False)}"

    def require(self, tool: str, args: dict, reason: str = "") -> bool:
        """
        申请执行许可。返回 True 才允许执行。
        """
        if tool not in SENSITIVE_TOOLS:
            return True

        key = self._key(tool, args)
        if key in self._denied_cache:
            return False

        req = PendingRequest(id=uuid.uuid4().hex[:8], tool=tool, args=args, reason=reason)

        with self._lock:
            self._pending[req.id] = req

        try:
            if self.mode == "terminal":
                ok = self._ask_terminal(req)
            elif self.mode == "callback":
                ok = self._ask_callback(req)
            elif self.mode == "auto_approve":
                # 仅用于用户已主动发起的上传等场景，
                # 白名单和只读校验依然完整生效
                ok = True
            else:
                ok = False

            if not ok:
                self._denied_cache.add(key)
            return ok
        finally:
            with self._lock:
                self._pending.pop(req.id, None)

    # ---------- 终端交互 ----------

    def _ask_terminal(self, req: PendingRequest) -> bool:
        print("\n" + "=" * 56)
        print("  ⚠️  Agent 申请读取本地文件")
        print("=" * 56)
        print(f"  工具: {req.tool}")
        if req.reason:
            print(f"  原因: {req.reason}")
        print("  参数:")
        for k, v in req.args.items():
            print(f"    {k} = {v}")
        print("-" * 56)
        print("  只读操作，不会修改任何文件。")
        try:
            ans = input("  允许？(y/N) > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        print("=" * 56 + "\n")
        return ans in ("y", "yes", "是", "允许")

    # ---------- 回调交互（Web 用） ----------

    def _ask_callback(self, req: PendingRequest) -> bool:
        if self.on_request:
            try:
                self.on_request(req)
            except Exception:
                return False
        # 等待前端 approve/deny，超时视为拒绝
        return req.wait(self.timeout)

    # ---------- 供 Web 层调用 ----------

    def approve(self, req_id: str) -> bool:
        with self._lock:
            req = self._pending.get(req_id)
        if req:
            req.approve()
            return True
        return False

    def deny(self, req_id: str) -> bool:
        with self._lock:
            req = self._pending.get(req_id)
        if req:
            req.deny()
            return True
        return False

    def list_pending(self) -> list[dict]:
        with self._lock:
            return [
                {"id": r.id, "tool": r.tool, "args": r.args, "reason": r.reason}
                for r in self._pending.values()
            ]


def build_gateway() -> ConfirmGateway:
    """按运行环境造一个网关"""
    mode = os.getenv("CONFIRM_MODE", "terminal")
    timeout = float(os.getenv("CONFIRM_TIMEOUT", DEFAULT_TIMEOUT))
    return ConfirmGateway(mode=mode, timeout=timeout)
