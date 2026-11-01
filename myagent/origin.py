"""
调用来源标记

同一套 Agent 代码，被三种入口驱动：
- web   ：浏览器/桌面窗口，有人坐在前面，可以弹确认框
- api   ：HTTP 接口，调用方是另一个程序，没人点确认
- mcp   ：被 MCP 主机（Claude Desktop / Cursor）当工具用，同样没人

权限不能靠"全局改 CONFIRM_MODE"来区分：那是进程级状态，
网页用户正在用的时候，一个外部请求把模式改掉，会连带改掉网页的行为。
用 contextvars 记录当前请求的来源，工具执行的最深处读它，各自算各自的。
"""

from __future__ import annotations

import contextvars

_origin: contextvars.ContextVar[str] = contextvars.ContextVar("xy_origin", default="web")


def set_origin(value: str) -> contextvars.Token:
    """设置来源，返回 token（务必在 finally 里 reset，避免线程复用时串味）。"""
    return _origin.set((value or "web").strip().lower())


def reset_origin(token: contextvars.Token) -> None:
    try:
        _origin.reset(token)
    except Exception:
        # token 来自别的上下文时会报错；不影响业务，忽略
        pass


def current_origin() -> str:
    return _origin.get()


def is_unattended() -> bool:
    """没人值守：确认框弹给谁看都不合适。"""
    return current_origin() in ("api", "mcp")
