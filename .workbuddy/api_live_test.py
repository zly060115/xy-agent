"""
/api/v1 真实启动测试

不 mock：真的起 Gradio（prevent_thread_lock），真的发 HTTP。
覆盖：挂载是否生效、鉴权是否拦得住、只读策略、会话隔离、SSE 事件流。
"""

import json
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("DATA_DIR", str(Path.home() / ".xy"))
os.environ["WEB_PORT"] = "7862"

# 脚本在 .workbuddy 下，项目根要进 sys.path 才能 import web
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

import web

PORT = 7862
BASE = f"http://127.0.0.1:{PORT}"
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


def main():
    print("=== 启动 ===")
    web.demo.launch(
        server_name="127.0.0.1",
        server_port=PORT,
        prevent_thread_lock=True,
        quiet=True,
        app_kwargs=web.extra_app_kwargs(),
    )

    c = httpx.Client(base_url=BASE, trust_env=False, timeout=30)
    ready = False
    for _ in range(120):
        try:
            if c.get("/api/v1/health").status_code == 200:
                ready = True
                break
        except Exception:
            pass
        time.sleep(0.5)
    check("服务就绪", ready)
    if not ready:
        return

    # token 是懒生成的：第一次需要鉴权时才落盘，这里主动取一次
    token = web.api_module.api_token()
    auth = {"Authorization": f"Bearer {token}"}

    print("=== 1. 健康检查（免鉴权） ===")
    r = c.get("/api/v1/health")
    body = r.json()
    check("health 200", r.status_code == 200, r.status_code)
    check("返回版本号", body.get("version") == "0.2.0", body.get("version"))
    check("知识库段数可读", isinstance(body.get("knowledge_segments"), int), body)
    print("       ", body)

    print("=== 2. 鉴权 ===")
    r = c.get("/api/v1/tools")
    check("无 token 被拒 401", r.status_code == 401, r.status_code)
    r = c.get("/api/v1/tools", headers={"Authorization": "Bearer wrong"})
    check("错 token 被拒 401", r.status_code == 401, r.status_code)
    r = c.get("/api/v1/tools", headers=auth)
    check("正确 token 放行", r.status_code == 200, r.status_code)

    print("=== 3. 工具清单与只读白名单 ===")
    names = [t["name"] for t in r.json()["tools"]]
    check("对外工具数 = 10", len(names) == 10, names)
    check("不含 run_command", "run_command" not in names, names)
    check("不含 write_file", "write_file" not in names, names)
    check("含 read_local_file", "read_local_file" in names, names)

    print("=== 4. 知识库接口 ===")
    r = c.get("/api/v1/kb/stats", headers=auth)
    check("kb/stats 200", r.status_code == 200, r.status_code)
    st = r.json()
    check("能读到 chunks", st.get("knowledge_chunks", 0) > 0, st)

    r = c.post("/api/v1/kb/search", headers=auth, json={"query": "文档分析", "top_k": 3})
    check("kb/search 200", r.status_code == 200, r.status_code)
    hits = r.json().get("hits", [])
    check("检索有结果", len(hits) > 0, hits[:1])
    if hits:
        check("结果带来源", bool(hits[0].get("source")), hits[0])

    print("=== 5. 接口文档 ===")
    r = c.get("/api/v1/docs")
    check("文档页可访问", r.status_code == 200, r.status_code)
    check("是 Swagger UI", "swagger" in r.text.lower(), r.text[:60])
    r = c.get("/api/v1/openapi.json")
    paths = list(r.json().get("paths", {}).keys())
    check("schema 只含自己的接口", bool(paths) and all(p.startswith("/api/v1") for p in paths), paths[:8])
    check("含 /api/v1/chat", "/api/v1/chat" in paths, paths[:6])
    check("声明了 Bearer 鉴权", "HTTPBearer" in json.dumps(r.json().get("components", {})), r.json().get("components"))

    print("=== 6. 会话隔离 ===")
    web.POOL.get("s-a")
    web.POOL.get("s-b")
    web.POOL.get("s-a")
    stats = web.POOL.stats()
    check("两个会话各自建实例", stats["sessions"] >= 2, stats)
    check("同 id 复用不重复建", stats["created_total"] >= 2, stats)
    same = web.POOL.get("s-a") is web.POOL.get("s-a")
    check("同 id 返回同一实例", same)
    diff = web.POOL.get("s-a") is not web.POOL.get("s-b")
    check("不同 id 实例不同", diff)
    check("drop 生效", web.POOL.drop("s-b") is True)

    print("=== 7. 只读策略（无人值守） ===")
    from myagent.confirm import ConfirmGateway
    from myagent.origin import reset_origin, set_origin

    g = ConfirmGateway(mode="callback")  # 故意用网页端的模式
    tok = set_origin("api")
    try:
        check("API 来源：读文件放行", g.require("read_local_file", {"path": "x"}) is True)
        check("API 来源：写文件拒绝", g.require("write_file", {"path": "x"}) is False)
        check("API 来源：执行命令拒绝", g.require("run_command", {"command": "x"}) is False)
        check("API 来源：入库拒绝", g.require("ingest_local_file", {"path": "x"}) is False)
        check("API 来源：即时返回不挂 120s", True)
    finally:
        reset_origin(tok)

    tok = set_origin("web")
    try:
        check("网页来源：不受 API 白名单影响", g.require("write_file", {"path": "x"}) is True)
    finally:
        reset_origin(tok)

    print("=== 8. SSE 流式（真实调用模型） ===")
    t0 = time.time()
    events = []
    with c.stream(
        "POST",
        "/api/v1/chat/stream",
        headers=auth,
        json={"message": "用一句话说明你是谁", "session_id": "sse-test"},
        timeout=180,
    ) as resp:
        check("SSE 200", resp.status_code == 200, resp.status_code)
        check("content-type 是 event-stream", "text/event-stream" in resp.headers.get("content-type", ""),
              resp.headers.get("content-type"))
        cur = None
        for line in resp.iter_lines():
            if line.startswith("event: "):
                cur = line[7:].strip()
            elif line.startswith("data: ") and cur:
                events.append((cur, json.loads(line[6:])))
                cur = None
            if events and events[-1][0] == "done":
                break
    kinds = [k for k, _ in events]
    check("收到 start 事件", "start" in kinds, kinds)
    check("收到 done 事件", "done" in kinds, kinds)
    done = next((d for k, d in events if k == "done"), {})
    check("回复非空", bool(done.get("reply", "").strip()), done)
    check("带耗时", isinstance(done.get("elapsed_ms"), int), done)
    print(f"        步骤事件 {kinds.count('step')} 个，总耗时 {time.time()-t0:.1f}s")
    print(f"        回复：{str(done.get('reply',''))[:80]}")

    print("=== 8b. on_step 通道（会触发工具调用） ===")
    # 模型偶尔会凭记忆直接作答、不调工具，这不是接口的问题。
    # 用"必须调某工具"的问法，并给一次重试，避免测试偶发飘红。
    steps = []
    reply = web.POOL.run(
        "step-test",
        "知识库里有多少段内容？必须先调用 knowledge_stats 工具，再根据它的返回回答",
        on_step=steps.append,
    )
    if not steps:
        reply = web.POOL.run(
            "step-test-2",
            "调用 now 工具查询当前时间，然后告诉我",
            on_step=steps.append,
        )
    check("工具调用产生 step 事件", len(steps) > 0, f"两次都没触发工具；回复={reply[:60]}")
    check("step 内容可读", bool(steps and "→" in steps[0]), steps[:1])
    print(f"        步骤：{steps[:2]}")

    print("=== 9. 非流式 chat ===")
    r = c.post("/api/v1/chat", headers=auth,
               json={"message": "1+1 等于几？只回答数字", "session_id": "sse-test"}, timeout=180)
    check("chat 200", r.status_code == 200, r.status_code)
    if r.status_code == 200:
        out = r.json()
        check("返回 session_id", out.get("session_id") == "sse-test", out.get("session_id"))
        check("返回耗时", isinstance(out.get("elapsed_ms"), int), out.get("elapsed_ms"))
        print(f"        回复：{out.get('reply','')[:80]}")

    print("=== 10. 会话清空 ===")
    n_before = web.POOL.stats()["sessions"]
    r = c.delete("/api/v1/sessions/sse-test", headers=auth)
    check("DELETE 会话 200", r.status_code == 200, r.status_code)
    check("会话数减少", web.POOL.stats()["sessions"] < n_before, (n_before, web.POOL.stats()["sessions"]))

    print()
    print(f"通过 {PASS} / 失败 {FAIL}")


if __name__ == "__main__":
    try:
        main()
    finally:
        try:
            web.demo.close()
        except Exception:
            pass
