# -*- coding: utf-8 -*-
"""通过 HTTP API 向运行中的 XY 提问，打印最终回答。
用法: python .workbuddy/ask.py "问题"
"""
import json
import sys
import urllib.request

BASE = "http://127.0.0.1:7860"


def ask(q: str, timeout: int = 600) -> str:
    body = json.dumps({"data": [q, []]}).encode()
    req = urllib.request.Request(
        f"{BASE}/gradio_api/call/chat",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    # 本机请求必须绕过系统代理
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=30) as r:
        eid = json.load(r)["event_id"]

    with opener.open(f"{BASE}/gradio_api/call/chat/{eid}", timeout=timeout) as r:
        raw = r.read().decode("utf-8", "ignore")

    last_data = None
    for line in raw.splitlines():
        if line.startswith("data:"):
            last_data = line[5:].strip()
    if not last_data:
        return f"<无返回> {raw[:200]}"
    payload = json.loads(last_data)
    hist = payload[1] if isinstance(payload, list) and len(payload) > 1 else []
    for msg in reversed(hist):
        if msg.get("role") == "assistant":
            c = msg.get("content")
            if isinstance(c, list):
                return "".join(b.get("text", "") for b in c if isinstance(b, dict))
            return str(c)
    return "<没有 assistant 消息>"


if __name__ == "__main__":
    q = sys.argv[1] if len(sys.argv) > 1 else "你好"
    print(ask(q))
