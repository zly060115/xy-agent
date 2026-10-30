"""
全局运行时容器

把模型、知识库、文件读取器、确认网关挂在同一个地方，
避免 tools.py 和 agent.py 互相 import 造成循环依赖。
"""

from dataclasses import dataclass
from typing import Any


@dataclass
class Runtime:
    llm: Any = None
    rag: Any = None
    reader: Any = None
    gateway: Any = None


_runtime = Runtime()


def set_runtime(**kwargs):
    for k, v in kwargs.items():
        if hasattr(_runtime, k):
            setattr(_runtime, k, v)


def get_runtime() -> Runtime:
    return _runtime
