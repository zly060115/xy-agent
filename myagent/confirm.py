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

from .origin import is_unattended

# 需要人工确认的工具
SENSITIVE_TOOLS = {
    "read_local_file",
    "ingest_local_file",
    "list_local_files",
    "analyze_document",
}

# 上述敏感工具里，真正只读的部分。
# 给 API / MCP 这类无人值守场景用：读放行、写拒绝。
# ingest_local_file 不在此列 —— 它会往向量库里写东西，属于写操作。
READONLY_TOOLS = {
    "read_local_file",
    "list_local_files",
    "analyze_document",  # analyst 明确"读完整篇，不入库"，不落盘
}

# 会改变机器状态、但不走人工确认的工具。
# 它们不在 SENSITIVE_TOOLS 里（网页端一直是直接放行的），
# 所以 require() 原本对它们无脑返回 True —— allow_readonly 必须单独挡住，
# 否则"只读"名不副实。
WRITE_TOOLS = {
    "write_file",
    "run_command",
    "ingest_local_file",
}


def _external_allowlist() -> set[str]:
    """
    对外（API / MCP）允许的工具集合。

    懒加载 + 兜底：tools 模块导入较重，且它在导入期就会读环境变量；
    真拿不到时返回空集，按最保守处理（全拒），不会因为异常而放开权限。
    """
    try:
        from .tools import external_tool_names

        return set(external_tool_names())
    except Exception:
        return set()

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
    确认网关。四种模式：
    - terminal：命令行交互（供 main.py 使用）
    - callback：外部回调（供 web.py 使用）
    - allow_readonly：无人值守，只读放行、写入拒绝（供 API / MCP 使用）
    - auto_approve：全部放行（仅供用户自己主动触发的上传等场景）
    其余取值一律拒绝（auto_deny 的语义）。
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
        # 无人值守（API / MCP）：不看 mode，看对外白名单。
        # 单一口径：白名单里有就放行，没有就拒绝。
        # 不这么切的话，网页用户正在弹确认框时，外部请求会去等一个
        # 永远不会有人点的确认，白挂 120 秒。
        if is_unattended():
            return tool in _external_allowlist()

        if tool not in SENSITIVE_TOOLS:
            # 非敏感工具里也混着写操作（write_file / run_command）。
            # 无人值守的只读模式下必须挡掉，否则「只读」是假的。
            if self.mode == "allow_readonly" and tool in WRITE_TOOLS:
                return False
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
            elif self.mode == "allow_readonly":
                # 无人值守（API / MCP）：只读放行、写拒绝。
                # 别的 Agent 调过来最常用的就是读文档、分析文档，
                # 一刀切拒绝等于把能力废掉；但往知识库里写不能自动放行。
                ok = tool in READONLY_TOOLS
            elif self.mode == "auto_approve":
                # 仅用于用户已主动发起的上传等场景，
                # 白名单和只读校验依然完整生效
                ok = True
            else:
                ok = False

            # 只缓存"人明确拒绝过"的决定。策略性拒绝（allow_readonly 挡写入）
            # 不缓存：策略会变，缓存住会让切换模式后同一请求永远失败。
            if not ok and self.mode in ("terminal", "callback"):
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
        if not self.on_request:
            # 没有接确认弹窗（网页会话里没做确认界面）时立刻拒绝。
            # 以前这里会死等 timeout（120 秒），用户只看到转圈，最后才发现被拒。
            return False
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
