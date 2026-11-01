"""
MCP 服务（stdio）

把 XY 的能力暴露给 MCP 主机：Claude Desktop、Cursor，或任何支持 MCP 的
Agent 框架。两条用法都给：

1. ask_xy(question) —— 整个 Agent 当一个工具。
   对端说一句"帮我看看知识库里关于 XX 的内容"，XY 自己检索、读文档、
   跑完 ReAct 循环，回一段话。对端不需要知道内部有 13 个工具。

2. 原子工具（默认 10 个）—— 对端当大脑，自己决定读哪个文件、检索什么。
   适合宿主已经有编排逻辑，只想借用你的读取和检索能力。

为什么用 stdio 而不是 HTTP：
宿主是本地进程，stdio 不用端口、不用鉴权、不会被防火墙和杀软拦，
配置里写一行 command 就能用。要跨机器再上 streamable-http。

安全边界：
- 默认不暴露 write_file / run_command / ingest_local_file。
  其中 run_command 用的是 shell=True + 5 个词的黑名单，实质上接近
  任意命令执行，不该出现在默认工具集里。
- 要开：set XY_EXTERNAL_ALLOW=write,shell,ingest（三个可分开给）。
- 所有工具调用都走 execute_tool()，仍然经过 confirm 的白名单校验，
  即使宿主绕过工具列表直接请求，也会被拒。

stdio 协议注意：stdout 是协议通道，任何普通 print 都会让宿主解析失败。
本文件的日志一律走 stderr；Agent 以 verbose=False 构造。
"""

from __future__ import annotations

import logging
import threading

from mcp.server.mcpserver import MCPServer

from myagent import bootstrap
from myagent.agent import Agent
from myagent.confirm import ConfirmGateway
from myagent.config import config
from myagent.memory import ConversationMemory
from myagent.origin import reset_origin, set_origin
from myagent.runtime import set_runtime
from myagent.tools import execute_tool, external_tool_names

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    stream=__import__("sys").stderr,
)
log = logging.getLogger("xy.mcp")

# ---------- 装配（与网页端同一份 bootstrap） ----------

_ENV = bootstrap.load_env_config()
bootstrap.apply_saved_or_env(_ENV)
LLM, _STARTUP_ERR = bootstrap.build_llm_or_unavailable()
RAG = bootstrap.build_rag()
READER = bootstrap.build_reader(LLM)
GATEWAY = ConfirmGateway(mode="allow_readonly")
bootstrap.allow_dirs(READER, config.workspace_dir, config.data_dir, config.uploads_dir)
set_runtime(llm=LLM, rag=RAG, reader=READER, gateway=GATEWAY)

if _STARTUP_ERR:
    log.warning("模型不可用：%s（工具仍可调用，ask_xy 会返回原因）", _STARTUP_ERR)

_ALLOWED = set(external_tool_names())
log.info("对外工具：%s", ", ".join(sorted(_ALLOWED)))

INSTRUCTIONS = """XY 是一个本地文档分析助手。

- 它有自己的知识库（用户上传并入库的文档），以及本机文件读取权限。
- ask_xy 适合"问一句话、要一段结论"：它自己决定检索什么、读哪个文件。
- 原子工具适合你已经知道要做什么、只需要它去执行一步。
- 默认只读：不会写文件、不执行命令、不会往知识库里加东西。
"""

server = MCPServer(name="xy-agent", instructions=INSTRUCTIONS)


def _run(name: str, args: dict) -> str:
    """统一入口：标来源 + 走 execute_tool（保留确认与白名单校验）。"""
    token = set_origin("mcp")
    try:
        return execute_tool(name, args)
    except Exception as e:  # 工具异常不该让整个 MCP 连接断掉
        return f"工具执行出错: {type(e).__name__}: {e}"
    finally:
        reset_origin(token)


# ---------- 复合：整个 Agent 当一个工具 ----------

_agent: Agent | None = None
_agent_lock = threading.Lock()


@server.tool(
    name="ask_xy",
    description="把你的问题交给 XY 处理：它会检索本地知识库、按需读取文档，"
    "跑完多轮工具调用后给出结论。适合开放式问题。",
)
def ask_xy(question: str) -> str:
    """问 XY 一个问题，返回它的完整回答。

    Args:
        question: 你的问题。可以引用"知识库里那份文档"，它会自己去检索。
    """
    if _STARTUP_ERR:
        return f"XY 的模型当前不可用（{_STARTUP_ERR}），请在网页端重新配置模型后再试。"

    global _agent
    token = set_origin("mcp")
    try:
        # 串行：MCP 宿主可能并发调用，但对话历史是共享的，
        # 并发进来会互相污染上下文（这也是 HTTP 那边用会话池的原因）
        with _agent_lock:
            if _agent is None:
                _agent = Agent(
                    LLM,
                    max_steps=config.max_steps,
                    verbose=False,
                    memory=ConversationMemory(RAG),
                )
            return _agent.run(question)
    except Exception as e:
        return f"XY 执行出错: {type(e).__name__}: {e}"
    finally:
        reset_origin(token)


# ---------- 原子工具 ----------
#
# 签名要显式写出来：MCP 靠类型注解生成 JSON Schema，
# 用 **kwargs 包一层的话，宿主看到的参数表就是空的。


@server.tool(description="当前日期和时间（含星期）")
def now() -> str:
    """返回当前的日期与时间。"""
    return _run("now", {})


if "list_files" in _ALLOWED:

    @server.tool(description="列出 XY 工作区内的文件")
    def list_files(path: str = ".") -> str:
        """列出工作区某个目录下的文件。

        Args:
            path: 相对工作区的路径，默认当前目录
        """
        return _run("list_files", {"path": path})


if "read_file" in _ALLOWED:

    @server.tool(description="读取 XY 工作区内的文本文件")
    def read_file(path: str) -> str:
        """读取工作区内的一个文本文件。

        Args:
            path: 相对工作区的路径
        """
        return _run("read_file", {"path": path})


if "search_web" in _ALLOWED:

    @server.tool(description="联网搜索（DuckDuckGo，无需 API Key）")
    def search_web(query: str) -> str:
        """搜索网页，返回前几条结果。

        Args:
            query: 搜索词
        """
        return _run("search_web", {"query": query})


if "calculator" in _ALLOWED:

    @server.tool(description="安全计算器，只支持数字与基本运算")
    def calculator(expression: str) -> str:
        """计算一个数学表达式。

        Args:
            expression: 例如 (128*3+45)/2
        """
        return _run("calculator", {"expression": expression})


if "search_knowledge" in _ALLOWED:

    @server.tool(description="在 XY 的本地知识库里做语义检索")
    def search_knowledge(query: str, top_k: int = 4) -> str:
        """检索已入库的文档片段。

        Args:
            query: 检索词或问句
            top_k: 返回几条，默认 4
        """
        return _run("search_knowledge", {"query": query, "top_k": top_k})


if "knowledge_stats" in _ALLOWED:

    @server.tool(description="查看知识库里有多少内容")
    def knowledge_stats() -> str:
        """返回知识库与长期记忆的条目数。"""
        return _run("knowledge_stats", {})


if "list_local_files" in _ALLOWED:

    @server.tool(description="列出本机某个目录下的文件（只读，受白名单限制）")
    def list_local_files(directory: str = "") -> str:
        """列出本机目录下的文件。

        Args:
            directory: 绝对路径；留空则用默认的可读目录
        """
        return _run("list_local_files", {"directory": directory})


if "read_local_file" in _ALLOWED:

    @server.tool(description="读取本机的一个文本文件（只读，受白名单限制）")
    def read_local_file(path: str) -> str:
        """读取本机文件内容。

        Args:
            path: 文件的绝对路径
        """
        return _run("read_local_file", {"path": path})


if "analyze_document" in _ALLOWED:

    @server.tool(description="分析一篇文档的重点，返回结构化要点（不入库）")
    def analyze_document(path: str) -> str:
        """分析文档并返回要点。

        Args:
            path: 文档的绝对路径
        """
        return _run("analyze_document", {"path": path})


# ---------- 显式解锁才暴露的写操作 ----------

if "write_file" in _ALLOWED:

    @server.tool(description="在工作区内写文本文件（需 XY_EXTERNAL_ALLOW=write）")
    def write_file(path: str, content: str) -> str:
        """写文件。

        Args:
            path: 目标路径
            content: 文件内容
        """
        return _run("write_file", {"path": path, "content": content})


if "run_command" in _ALLOWED:

    @server.tool(
        description="在工作区内执行 shell 命令（需 XY_EXTERNAL_ALLOW=shell，"
        "等同于把本机命令执行权限交给调用方）"
    )
    def run_command(command: str) -> str:
        """执行命令。

        Args:
            command: 要执行的命令
        """
        return _run("run_command", {"command": command})


if "ingest_local_file" in _ALLOWED:

    @server.tool(description="把文件解析后写入知识库（需 XY_EXTERNAL_ALLOW=ingest）")
    def ingest_local_file(path: str) -> str:
        """入库一个文件。

        Args:
            path: 文件的绝对路径
        """
        return _run("ingest_local_file", {"path": path})


def main() -> None:
    log.info("XY MCP 服务启动（stdio）")
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
