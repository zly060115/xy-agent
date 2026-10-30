# -*- coding: utf-8 -*-
"""验证 ping(check_tools=True) 的双阶段自检：
本地原生 Ollama 与 OpenAI 兼容通道都要报「工具调用往返正常」。
"""
import sys

sys.path.insert(0, ".")
from myagent.llm import OllamaLLM, OpenAICompatLLM, ping, _probe_tool_roundtrip

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


print("[1] 本地原生 Ollama")
native = OllamaLLM(model="qwen2.5:7b", host="http://localhost:11434")
print(f"  supports_tools={native.supports_tools}")
ok, note = _probe_tool_roundtrip(native)
print(f"  roundtrip → {ok} | {note}")
check("原生通道工具往返通过", ok, note)

ok, msg = ping(native, check_tools=True)
print(f"  ping → {ok} | {msg}")
check("ping 包含工具往返结论", ok and "工具" in msg, msg)

print("[2] OpenAI 兼容通道（localhost:11434/v1）")
compat = OpenAICompatLLM(model="qwen2.5:7b", api_key="ollama", base_url="http://localhost:11434/v1", provider_name="ollama")
ok, note = _probe_tool_roundtrip(compat)
print(f"  roundtrip → {ok} | {note}")
check("兼容通道工具往返通过", ok, note)

ok, msg = ping(compat, check_tools=True)
print(f"  ping → {ok} | {msg}")
check("ping 判定为可用", ok, msg)

print("[3] 坏配置必须 ❌ 而不是假通过")
bad = OpenAICompatLLM(model="qwen2.5:7b", api_key="x", base_url="http://127.0.0.1:9/v1", provider_name="x")
ok, msg = ping(bad, check_tools=True)
print(f"  bad → {ok} | {msg}")
check("连不上时返回 False", ok is False, msg)
check("报错是中文可读", any("\u4e00" <= c <= "\u9fff" for c in msg), msg)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
