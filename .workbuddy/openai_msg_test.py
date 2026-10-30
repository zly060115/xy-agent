# -*- coding: utf-8 -*-
"""验证 OpenAICompatLLM 的消息格式转换：
1. 单元断言：转换后的消息符合 OpenAI 协议 schema
2. 实测：未转换的扁平格式发给 OpenAI 兼容端点会 422，转换后能通过
"""
import json
import sys

sys.path.insert(0, ".")
from myagent.llm import OpenAICompatLLM

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


# ---------- 1. 单元断言 ----------
print("[1] _to_openai_msg 单元断言")
conv = OpenAICompatLLM._to_openai_msg

m = conv(
    {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": "call_abc", "name": "calculator", "arguments": {"expr": "1+1"}}],
    }
)
check("assistant.tool_calls 有 type=function", m["tool_calls"][0].get("type") == "function")
check("function.name 在 function 里", m["tool_calls"][0]["function"]["name"] == "calculator")
check("arguments 是字符串", isinstance(m["tool_calls"][0]["function"]["arguments"], str))
check("arguments 内容正确", json.loads(m["tool_calls"][0]["function"]["arguments"])["expr"] == "1+1")
check("保留 tool_call id", m["tool_calls"][0]["id"] == "call_abc")
check("缺 id 时自动补", conv({"role": "assistant", "content": "", "tool_calls": [{"name": "x", "arguments": {}}]})["tool_calls"][0]["id"] == "call_0")

m = conv({"role": "tool", "content": "结果42", "tool_call_id": "call_abc", "name": "calculator"})
check("tool 消息保留 tool_call_id", m.get("tool_call_id") == "call_abc")
check("tool 消息去掉 name 字段", "name" not in m)
check("tool content 保留", m["content"] == "结果42")

m = conv({"role": "user", "content": None})
check("user content None 补空串", m["content"] == "")

m = conv({"role": "assistant", "content": "普通回复"})
check("普通 assistant 不加 tool_calls", "tool_calls" not in m)

# ---------- 2. 实测：扁平格式 vs 标准格式 ----------
print("[2] Ollama OpenAI 兼容端点实测（localhost:11434/v1）")
history_bad = [
    {"role": "system", "content": "你是助手"},
    {"role": "user", "content": "1+1等于几"},
    {"role": "assistant", "content": "",
     "tool_calls": [{"id": "call_1", "name": "calculator", "arguments": {"expr": "1+1"}}]},
    {"role": "tool", "content": "2", "tool_call_id": "call_1", "name": "calculator"},
    {"role": "assistant", "content": "",
     "tool_calls": [{"id": "call_2", "name": "web_search", "arguments": {"query": "1+1"}}]},
    {"role": "tool", "content": "2", "tool_call_id": "call_2", "name": "web_search"},
]

from openai import OpenAI

cli = OpenAI(api_key="ollama", base_url="http://localhost:11434/v1", timeout=60)

bad_ok, std_ok = None, None
try:
    cli.chat.completions.create(model="qwen2.5:7b", messages=history_bad, max_tokens=8)
    bad_ok = True
except Exception as e:
    bad_ok = False
    print(f"  扁平格式(修前) → {type(e).__name__}: {str(e)[:80]}")

history_good = [OpenAICompatLLM._to_openai_msg(m) for m in history_bad]
try:
    r = cli.chat.completions.create(model="qwen2.5:7b", messages=history_good, max_tokens=8)
    std_ok = bool(r.choices and r.choices[0].message)
    print(f"  标准格式(修后) → 正常返回: {(r.choices[0].message.content or '')[:40]!r}")
except Exception as e:
    std_ok = False
    print(f"  标准格式(修后) → {type(e).__name__}: {str(e)[:120]}")

check("扁平格式被服务端拒绝(复现422)", bad_ok is False)
check("标准格式被服务端接受", std_ok is True)

# ---------- 3. 端到端：走 OpenAICompatLLM.chat 两轮 ----------
print("[3] OpenAICompatLLM.chat 两轮对话")
llm = OpenAICompatLLM(model="qwen2.5:7b", api_key="ollama", base_url="http://localhost:11434/v1", provider_name="ollama")
msgs = [{"role": "system", "content": "你是助手，需要时调用工具。"},
        {"role": "user", "content": "用 calculator 算 23*7，然后告诉我结果"}]
try:
    r1 = llm.chat(msgs, tools=[{"type": "function", "function": {
        "name": "calculator", "description": "计算",
        "parameters": {"type": "object", "properties": {"expr": {"type": "string"}}, "required": ["expr"]}}}])
    if r1.get("tool_calls"):
        tc = r1["tool_calls"][0]
        msgs.append({"role": "assistant", "content": r1.get("content") or "", "tool_calls": r1["tool_calls"]})
        msgs.append({"role": "tool", "content": "161", "tool_call_id": tc["id"], "name": tc["name"]})
        r2 = llm.chat(msgs, tools=[])
        e2e = "161" in (r2.get("content") or "")
        check("第二轮不报422且答对161", e2e, f"second={r2.get('content')!r:.80}")
    else:
        check("第一轮发出工具调用", False, f"r1={r1}")
except Exception as e:
    check("两轮对话无异常", False, f"{type(e).__name__}: {str(e)[:120]}")

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
