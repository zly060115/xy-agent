"""
MCP 端到端测试

不 mock：真的用 MCP 客户端通过 stdio 拉起 mcp_server.py，
真的 initialize、list_tools、call_tool。
"""

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

PASS = 0
FAIL = 0


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [OK]   {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  -> {detail}")


def text_of(result) -> str:
    parts = []
    for c in getattr(result, "content", []) or []:
        t = getattr(c, "text", None)
        if t:
            parts.append(t)
    return "\n".join(parts)


async def main():
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(ROOT / "mcp_server.py")],
        cwd=str(ROOT),
        env=dict(os.environ),
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            print("=== 1. 握手 ===")
            info = await session.initialize()
            check("initialize 成功", info is not None)
            srv = getattr(info, "server_info", None) or getattr(info, "serverInfo", None)
            print(f"        服务端：{getattr(srv, 'name', '?')} {getattr(srv, 'version', '')}")
            check("服务名是 xy-agent", getattr(srv, "name", "") == "xy-agent", srv)

            print("=== 2. 工具清单 ===")
            tl = await session.list_tools()
            names = sorted(t.name for t in tl.tools)
            check("工具数 = 11", len(names) == 11, names)
            check("有 ask_xy", "ask_xy" in names, names)
            check("没有 run_command", "run_command" not in names, names)
            check("没有 write_file", "write_file" not in names, names)
            check("没有 ingest_local_file", "ingest_local_file" not in names, names)
            print(f"        {', '.join(names)}")

            ask = next(t for t in tl.tools if t.name == "ask_xy")
            # mcp 2.x 的 Tool 用 snake_case（input_schema），1.x 是 inputSchema
            schema = getattr(ask, "input_schema", None) or getattr(ask, "inputSchema", {})
            check("ask_xy 有参数 question", "question" in (schema or {}).get("properties", {}), schema)
            check("ask_xy 有描述", bool(ask.description), ask.description)

            print("=== 3. 纯计算工具 ===")
            r = await session.call_tool("calculator", {"expression": "(128*3+45)/2"})
            out = text_of(r)
            # (384+45)/2 = 214.5
            check("calculator 返回 214.5", "214.5" in out, out[:80])

            r = await session.call_tool("now", {})
            out = text_of(r)
            check("now 返回时间", any(ch.isdigit() for ch in out) and ":" in out, out[:60])

            print("=== 4. 知识库工具 ===")
            r = await session.call_tool("knowledge_stats", {})
            out = text_of(r)
            check("knowledge_stats 有内容", "知" in out or "段" in out or any(c.isdigit() for c in out),
                  out[:100])
            print(f"        {out[:90].replace(chr(10), ' | ')}")

            r = await session.call_tool("search_knowledge", {"query": "文档分析", "top_k": 2})
            out = text_of(r)
            check("search_knowledge 有结果", len(out) > 40, out[:80])

            print("=== 5. 越权工具不可调用 ===")
            denied = False
            detail = ""
            try:
                r = await session.call_tool("run_command", {"command": "echo hi"})
                detail = text_of(r)[:80]
                # SDK 对不存在的工具可能返回普通结果（内容是 "Unknown tool"），
                # 也可能带 is_error；两种都算拒绝，只要没真执行
                is_err = bool(getattr(r, "is_error", False) or getattr(r, "isError", False))
                denied = is_err or "unknown tool" in detail.lower() or "不存在" in detail
            except Exception as e:
                denied = True
                detail = f"{type(e).__name__}: {e}"
            check("调用未暴露的工具被拒绝", denied, detail)

            print("=== 6. ask_xy（真实跑 Agent） ===")
            r = await session.call_tool(
                "ask_xy",
                {"question": "用一句话说明你是谁，并说出你知识库里有多少段内容"},
                read_timeout_seconds=180,
            )
            out = text_of(r)
            check("ask_xy 有回复", len(out) > 10, out[:100])
            check("自称 XY", "XY" in out, out[:100])
            print(f"        {out[:120]}")

    print()
    print(f"通过 {PASS} / 失败 {FAIL}")


if __name__ == "__main__":
    asyncio.run(main())
