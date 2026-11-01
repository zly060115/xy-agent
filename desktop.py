"""
XY 桌面版（pywebview 外壳）

网页端（web.py）打开的是浏览器标签页，这个文件把它装进一个原生窗口：
有自己的标题栏、任务栏图标，没有地址栏，关掉窗口就是退出程序。

它只做三件必须做对的事：

1. 窗口先出来，界面后加载。
   Gradio 启动要好几秒（加载 chromadb、onnxruntime、模型配置）。
   如果先启动服务再开窗口，用户双击后会盯着桌面发呆十几秒，以为没反应。
   所以这里先渲染一个本地 splash 页面，再在后台线程里把服务拉起来。

2. 端口不抢别人的，也不重复起一份。
   7860 被占用时先问一句"是不是 XY 自己在跑"：是就直接接上去，
   不是才换端口。既不误杀别的程序，也不会出现两份服务抢同一个知识库。

3. 关窗 = 进程真的退出。
   Gradio 的服务线程不是 daemon，不管它，窗口关了 python.exe 还挂在
   任务管理器里，下次启动就撞端口。这是桌面版最常见的"关不干净"。

用法：
    .venv\\Scripts\\pythonw.exe desktop.py     # 正常使用（无黑窗口）
    .venv\\Scripts\\python.exe  desktop.py --debug   # 调试（开 F12 开发者工具）
"""

from __future__ import annotations

import inspect
import json
import os
import socket
import sys
import threading
import time
import traceback
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from myagent.config import config  # noqa: E402

APP_NAME = "XY"
WIN_W, WIN_H = 1280, 860
MIN_W, MIN_H = 980, 640
ICON = ROOT / "assets" / "icon.ico"
DATA_DIR = Path(config.data_dir)
LOG_PATH = DATA_DIR / "desktop.log"
WV_CACHE = DATA_DIR / "webview_cache"

# ---------- 日志 ----------
# pythonw.exe 没有控制台，sys.stdout 是 None。此时任何一次 print 都会抛
# AttributeError，程序会静默死掉 —— 所以必须在最开头就把两个流接到文件上。
DATA_DIR.mkdir(parents=True, exist_ok=True)
_LOG_FP = open(LOG_PATH, "a", encoding="utf-8", buffering=1)
if sys.stdout is None:
    sys.stdout = _LOG_FP
if sys.stderr is None:
    sys.stderr = _LOG_FP


def log(msg: str) -> None:
    try:
        _LOG_FP.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
    except Exception:
        pass


def log_exc(prefix: str = "") -> None:
    log(prefix + traceback.format_exc())


# ---------- 启动画面 ----------
SPLASH_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<style>
  * { margin:0; padding:0; box-sizing:border-box; }
  html, body { height:100%; }
  body {
    display:flex; align-items:center; justify-content:center;
    background:#ffffff; color:#1f2937; overflow:hidden;
    font-family:"Microsoft YaHei UI","Microsoft YaHei","PingFang SC",sans-serif;
    -webkit-user-select:none; user-select:none;
  }
  .wrap { display:flex; flex-direction:column; align-items:center; width:340px; }
  .logo {
    width:88px; height:88px; border-radius:26px;
    background:linear-gradient(145deg,#fbbf24,#d97706);
    color:#fff; font-size:30px; font-weight:700;
    font-family:"Segoe UI","Microsoft YaHei UI",sans-serif;
    display:flex; align-items:center; justify-content:center;
    box-shadow:0 12px 28px rgba(217,119,6,.28);
    letter-spacing:1px;
    text-indent:1px;
  }
  .name { margin-top:22px; font-size:19px; font-weight:600; letter-spacing:.5px; }
  .sub  { margin-top:6px; font-size:12px; color:#9ca3af; letter-spacing:.3px; }
  .bar  {
    margin-top:26px; width:190px; height:3px; border-radius:3px;
    background:#f3f4f6; overflow:hidden;
  }
  .bar i {
    display:block; width:38%; height:100%; border-radius:3px;
    background:linear-gradient(90deg,#fbbf24,#d97706);
    animation:slide 1.5s ease-in-out infinite;
  }
  @keyframes slide {
    0%   { transform:translateX(-100%); }
    100% { transform:translateX(365%); }
  }
  .status {
    margin-top:14px; font-size:12.5px; color:#6b7280;
    min-height:20px; text-align:center; line-height:1.6;
  }
  .status.err { color:#dc2626; font-weight:600; }
  .hint { margin-top:8px; font-size:11.5px; color:#9ca3af; line-height:1.7;
          text-align:center; word-break:break-all; max-width:320px; }
  .hint code { color:#6b7280; font-family:Consolas,monospace; }
</style></head>
<body>
  <div class="wrap">
    <div class="logo">XY</div>
    <div class="name">XY</div>
    <div class="sub">本地文档分析助手</div>
    <div class="bar"><i></i></div>
    <div class="status" id="status">正在准备…</div>
    <div class="hint" id="hint"></div>
  </div>
<script>
  function setStatus(t) {
    var el = document.getElementById('status');
    el.className = 'status';
    el.textContent = t;
  }
  function setFail(t, hint) {
    var el = document.getElementById('status');
    el.className = 'status err';
    el.textContent = t;
    document.querySelector('.bar').style.display = 'none';
    if (hint) { document.getElementById('hint').innerHTML = hint; }
  }
</script>
</body></html>"""


def _js(window, script: str) -> None:
    """splash 页面还没 ready 时 evaluate_js 会抛错，静默重试两次就放弃。"""
    for _ in range(3):
        try:
            window.evaluate_js(script)
            return
        except Exception:
            time.sleep(0.15)


def say(window, text: str) -> None:
    log(f"状态：{text}")
    _js(window, f"setStatus({json.dumps(text, ensure_ascii=False)});")


def fail(window, text: str, detail: str = "") -> None:
    log(f"失败：{text} {detail}")
    hint = f"日志：<code>{LOG_PATH}</code>"
    if detail:
        hint = f"{detail}<br>{hint}"
    _js(
        window,
        f"setFail({json.dumps(text, ensure_ascii=False)},"
        f"{json.dumps(hint, ensure_ascii=False)});",
    )


# ---------- 端口 ----------
def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.25)
        return s.connect_ex(("127.0.0.1", port)) != 0


def is_self(port: int) -> bool:
    """占用这个端口的是不是 XY 自己。

    只看端口通不通没用：那可能是别的程序。真要判断就看 Gradio 的 config
    里的 title —— 是我们自己的 Blocks(title="XY") 才算数。
    判断不出来（比如 4.x 端点不同）就当作不是，换个端口起，稳妥。
    """
    for ep in (f"/gradio_api/config", f"/config"):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{ep}", timeout=1.5) as r:
                data = json.loads(r.read().decode("utf-8", "ignore"))
            if "XY" in str(data.get("title", "")):
                return True
        except Exception:
            continue
    return False


def pick_port(preferred: int) -> tuple[int, bool]:
    """返回 (端口, 是否已在运行的 XY)。"""
    if not port_free(preferred):
        if is_self(preferred):
            return preferred, True
        for p in range(preferred + 1, preferred + 20):
            if port_free(p):
                log(f"端口 {preferred} 被其他程序占用，改用 {p}")
                return p, False
        raise RuntimeError(f"{preferred}~{preferred + 19} 都不可用，请先关掉占用端口的程序")
    return preferred, False


def wait_http(port: int, timeout: float = 180.0) -> None:
    """等服务真正能响应 HTTP，而不只是端口 listen。

    Gradio 会先 bind 端口再装配路由，中间有几秒是"端口通但请求 404"。
    直接 load_url 会看到一个 Gradio 的错误页，所以这里等到首页 200。
    """
    deadline = time.time() + timeout
    url = f"http://127.0.0.1:{port}/"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status == 200:
                    return
        except Exception:
            pass
        time.sleep(0.4)
    raise TimeoutError(f"服务 {timeout:.0f} 秒内没有就绪")


# ---------- 启动 Gradio ----------
def _launch_kwargs() -> dict:
    """过滤掉当前 Gradio 版本不认识的参数。

    换版本时 launch() 的参数会增减（比如 quiet 是后来才有的），
    这里按签名过滤一遍，免得升级后因为一个多余的关键字直接起不来。
    """
    kw = {
        "server_name": "127.0.0.1",
        "server_port": None,  # 调用前填
        "prevent_thread_lock": True,
        "inbrowser": False,
        "show_error": True,
        "footer_links": ["gradio"],
        "quiet": True,
    }
    # 桌面版和网页版必须挂同一套 /api/v1 路由，否则同一份代码在
    # 两种启动方式下能力不一致（桌面版调不通接口，排查时很容易误判）
    try:
        import web as webui

        kw["app_kwargs"] = webui.extra_app_kwargs()
    except Exception as e:
        log(f"附加 API 路由失败（不影响界面）：{e}")
    return kw


def boot(window, preferred_port: int) -> None:
    """后台线程：加载界面服务，然后把 splash 换成真正的界面。"""
    try:
        say(window, "正在检查运行环境…")
        port, running = pick_port(preferred_port)

        if running:
            say(window, "检测到已在运行的 XY，正在接入…")
        else:
            say(window, "正在加载模型与知识库（首次启动稍慢）…")
            # 延迟导入：这一句要好几秒，放在窗口出来之后才不白屏
            import web as webui  # noqa: F401  模块级已完成 Agent/RAG 初始化

            say(window, "正在启动界面服务…")
            kw = _launch_kwargs()
            kw["server_port"] = port
            demo = webui.demo
            sig = inspect.signature(demo.launch)
            if not any(p.kind == p.VAR_KEYWORD for p in sig.parameters.values()):
                kw = {k: v for k, v in kw.items() if k in sig.parameters}
            demo.launch(**kw)
            log(f"Gradio 已启动：127.0.0.1:{port}")
            try:
                if webui.api_module.auth_enabled():
                    log(f"对外接口：http://127.0.0.1:{port}/api/v1  （文档 /api/v1/docs）")
            except Exception:
                pass

            say(window, "正在等待服务就绪…")
            wait_http(port)

        say(window, "即将进入…")
        window.load_url(f"http://127.0.0.1:{port}/")
        log("已切换到主界面")
    except Exception as exc:
        log_exc("启动失败：\n")
        fail(window, f"启动失败：{exc}", "请看日志里的完整堆栈。")


# ---------- 退出 ----------
def shutdown() -> None:
    """窗口关掉后把服务一起收干净，然后立刻退出进程。

    demo.close() 会尝试关 uvicorn；但即便它卡住，也不能等 ——
    用 os._exit 直接退，避免"窗口没了、python.exe 还在"。
    聊天记忆是逐条落盘的，这里不存在未落盘的数据。
    """
    try:
        webui = sys.modules.get("web")
        demo = getattr(webui, "demo", None)
        closer = getattr(demo, "close", None)
        if callable(closer):
            threading.Thread(target=closer, daemon=True).start()
            time.sleep(0.4)
    except Exception:
        pass
    log("窗口已关闭，进程退出")
    try:
        sys.stdout.flush()
        sys.stderr.flush()
        _LOG_FP.flush()
    except Exception:
        pass
    os._exit(0)


def main() -> int:
    debug = "--debug" in sys.argv
    preferred = int(os.getenv("WEB_PORT", "7860"))

    log("=" * 60)
    log(f"启动桌面版 pid={os.getpid()} port={preferred} debug={debug}")

    try:
        import webview
    except Exception:
        log_exc("pywebview 导入失败：\n")
        return 3

    try:
        WV_CACHE.mkdir(parents=True, exist_ok=True)
        window = webview.create_window(
            APP_NAME,
            html=SPLASH_HTML,
            width=WIN_W,
            height=WIN_H,
            min_size=(MIN_W, MIN_H),
            background_color="#FFFFFF",
            text_select=True,   # 允许选中复制回答里的文字
            confirm_close=False,
        )
    except Exception:
        log_exc("创建窗口失败：\n")
        return 4

    start_kw = {
        "func": boot,
        "args": (window, preferred),
        "debug": debug,
        "icon": str(ICON) if ICON.exists() else None,
        # 默认 private_mode=True 每次都用新的临时目录，冷启动要多花一两秒；
        # 给个固定缓存目录，第二次开明显更快，界面偏好也能留住。
        "private_mode": False,
        "storage_path": str(WV_CACHE),
    }
    sig = inspect.signature(webview.start)
    start_kw = {k: v for k, v in start_kw.items() if k in sig.parameters}

    try:
        webview.start(**start_kw)   # 阻塞到窗口关闭
    except Exception:
        log_exc("窗口运行异常：\n")
        return 5
    finally:
        shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
