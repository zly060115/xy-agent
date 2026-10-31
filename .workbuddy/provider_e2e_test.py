"""端到端验证：界面选模型 + 填密钥 → 真实发出 HTTP 请求 → 拿到回答。

做法：本地起一个模拟厂商，完全按 OpenAI 协议应答（含工具调用两轮往返），
把 settings 指向它，然后跑真实的 Agent。这样能验证的是：
  选模型 → 存配置 → build_llm → 请求路径/鉴权头/消息格式 → 解析 → 工具往返 → 最终回答
唯一无法本地验证的是"厂商真的认你的密钥"，那需要真密钥。
"""
import json
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

sys.path.insert(0, r"C:/Users/zly/WorkBuddy/2026-10-27-18-26-17")

from myagent import settings
from myagent.agent import Agent
from myagent.llm import build_llm
from myagent.runtime import set_runtime

PORT = 7999
CALLS: list[dict] = []
GOOD_KEY = "sk-good-123456"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # 静音
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) or b"{}"
        try:
            body = json.loads(raw)
        except Exception:
            body = {}
        auth = self.headers.get("Authorization") or ""
        CALLS.append({"path": self.path, "auth": auth, "body": body})

        if GOOD_KEY not in auth:
            self._reply(401, {"error": {"message": "Incorrect API key provided", "type": "invalid_request_error"}})
            return

        msgs = body.get("messages") or []
        saw_tool_result = any(m.get("role") == "tool" for m in msgs)
        if not saw_tool_result:
            self._reply(200, {
                "id": "chatcmpl-mock-1", "object": "chat.completion", "created": 1,
                "model": body.get("model", "mock"),
                "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                    "role": "assistant", "content": None,
                    "tool_calls": [{"id": "call_1", "type": "function", "function": {
                        "name": "calculator", "arguments": json.dumps({"expression": "1280*0.123"})}}],
                }}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            })
        else:
            self._reply(200, {
                "id": "chatcmpl-mock-2", "object": "chat.completion", "created": 2,
                "model": body.get("model", "mock"),
                "choices": [{"index": 0, "finish_reason": "stop", "message": {
                    "role": "assistant", "content": "1280 × 0.123 = 157.44"}}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 8, "total_tokens": 28},
            })

    def _reply(self, code, payload):
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def make_cfg(data_dir):
    return SimpleNamespace(
        data_dir=data_dir, backend="api", provider="custom", model="mock-model",
        api_key="", base_url="", ollama_host="", vision_model="", num_ctx=8192,
    )


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"模拟厂商已启动 127.0.0.1:{PORT}")

    ok = True

    def check(name, cond, detail=""):
        nonlocal ok
        print(("PASS  " if cond else "FAIL  ") + name + (f"  [{detail}]" if detail else ""))
        ok = ok and bool(cond)

    # --- 1. 界面点选 → 存配置 → 应用 ---
    tmp = tempfile.mkdtemp()
    p = settings.save(tmp, "custom", "mock-model", GOOD_KEY, f"http://127.0.0.1:{PORT}/v1")
    check("配置已落盘", p.exists(), str(p.name))
    cfg = make_cfg(tmp)
    applied = settings.apply_to(cfg)
    check("配置被应用", applied)
    check("backend 切成 api", cfg.backend == "api", cfg.backend)
    check("model 正确", cfg.model == "mock-model", cfg.model)
    check("base_url 正确", cfg.base_url.endswith("/v1"), cfg.base_url)
    check("密钥正确写入", cfg.api_key == GOOD_KEY)

    # --- 2. build_llm 造出云端后端 ---
    llm = build_llm(cfg)
    check("选的是 OpenAI 兼容后端", type(llm).__name__ == "OpenAICompatLLM", type(llm).__name__)

    # --- 3. 真跑一轮 Agent：工具调用 + 结果回传 + 最终回答 ---
    set_runtime(llm=llm, rag=None)
    ag = Agent(llm=llm, verbose=True)
    out = ag.run("1280*0.123 是多少")
    check("拿到最终回答", "157.44" in out, out[:60].replace("\n", " "))

    check("请求数=2（工具往返两轮）", len(CALLS) == 2, str(len(CALLS)))
    first = CALLS[0]
    check("请求路径正确", first["path"] == "/v1/chat/completions", first["path"])
    check("鉴权头正确", first["auth"] == f"Bearer {GOOD_KEY}")
    check("model 传给厂商", first["body"].get("model") == "mock-model", str(first["body"].get("model")))
    check("tools 已按 OpenAI 协议下发", isinstance(first["body"].get("tools"), list) and len(first["body"]["tools"]) > 0)

    second = CALLS[1]
    roles = [m.get("role") for m in second["body"]["messages"]]
    check("第二轮带上了 tool 结果", "tool" in roles, str(roles))
    tool_msg = [m for m in second["body"]["messages"] if m.get("role") == "tool"][0]
    check("tool 消息只保留 tool_call_id", set(tool_msg.keys()) <= {"role", "content", "tool_call_id"}, str(sorted(tool_msg.keys())))
    asst = [m for m in second["body"]["messages"] if m.get("role") == "assistant" and m.get("tool_calls")]
    has_wrap = bool(asst) and asst[0]["tool_calls"][0].get("type") == "function" and "function" in asst[0]["tool_calls"][0]
    check("assistant 工具调用是标准包装", has_wrap)

    # --- 4. 密钥错误：走完整对话链路，用户看到的应是中文 ---
    CALLS.clear()
    settings.save(tmp, "custom", "mock-model", "sk-wrong", f"http://127.0.0.1:{PORT}/v1")
    cfg2 = make_cfg(tmp)
    settings.apply_to(cfg2)
    llm2 = build_llm(cfg2)
    set_runtime(llm=llm2, rag=None)
    out2 = Agent(llm=llm2).run("1280*0.123 是多少")
    check("错误密钥时给出中文提示", "密钥被拒绝" in out2, out2[:70].replace("\n", " "))
    check("错误密钥时不抛异常给界面", isinstance(out2, str) and len(out2) > 0)

    # --- 5. 自定义地址漏写 /v1 ---
    CALLS.clear()
    settings.save(tmp, "custom", "mock-model", GOOD_KEY, f"http://127.0.0.1:{PORT}")
    cfg3 = make_cfg(tmp)
    settings.apply_to(cfg3)
    try:
        build_llm(cfg3).chat([{"role": "user", "content": "hi"}])
        path3 = CALLS[0]["path"] if CALLS else "(无请求)"
        check("漏写 /v1 时能自动补全", path3 == "/v1/chat/completions", path3)
    except Exception as e:
        check("漏写 /v1 时能自动补全", False, str(e)[:70].replace("\n", " "))

    srv.shutdown()
    print("\n" + ("全部通过" if ok else "存在失败项"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
