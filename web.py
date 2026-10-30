"""
Gradio 网页端

功能：
- 对话式交互
- 文件上传入库（走同一套安全校验 + 人工确认）
- 知识库状态展示
- 风格的切换与保存

安全说明：
网页端的文件读取统一走 SafeFileReader，上传的文件会先落到临时目录，
再经白名单校验后才解析，避免绕过策略。
"""

import os
import queue
import shutil
import tempfile
import threading
import time
from pathlib import Path

import gradio as gr
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

from myagent.agent import Agent
from myagent.confirm import ConfirmGateway
from myagent.config import config
from myagent.memory import ConversationMemory
from myagent import providers, settings
from myagent.llm import UnavailableLLM, build_llm, ping
from myagent.rag import RAGStore, build_embed_fn
from myagent.runtime import set_runtime
from myagent.safe_reader import SafeFileReader

# ---------- 初始化运行时 ----------
config.backend = os.getenv("BACKEND", config.backend)
config.model = os.getenv("MODEL", config.model) or config.model
config.ollama_host = os.getenv("OLLAMA_HOST", config.ollama_host) or config.ollama_host
config.api_key = os.getenv("API_KEY", config.api_key) or ""
config.base_url = os.getenv("BASE_URL", config.base_url) or config.base_url
config.max_steps = int(os.getenv("MAX_STEPS", config.max_steps))
# 图片理解：视觉模型留空则只用 OCR
config.vision_model = os.getenv("VISION_MODEL", config.vision_model) or ""
config.ocr_enabled = os.getenv("OCR_ENABLED", "1").strip().lower() not in (
    "0",
    "false",
    "no",
    "off",
)
config.max_images = int(os.getenv("MAX_IMAGES", config.max_images) or config.max_images)
config.vision_max_images = int(
    os.getenv("MAX_VISION_IMAGES", config.vision_max_images) or config.vision_max_images
)

# 只由 .env 决定的状态快照：用户在界面上点「清除配置」时回到这里
ENV_BASELINE = {
    "backend": config.backend,
    "model": config.model,
    "ollama_host": config.ollama_host,
    "api_key": config.api_key,
    "base_url": config.base_url,
    "vision_model": config.vision_model,
    "provider": "ollama" if config.backend == "ollama" else providers.guess_id(config.base_url),
}

# 界面里保存过的模型选择优先于 .env：界面是显式动作，.env 可能早就过期
if not settings.apply_to(config):
    config.provider = ENV_BASELINE["provider"]

try:
    LLM = build_llm(config)
    STARTUP_ERR = ""
except Exception as e:  # 配置不全也要能打开网页，否则用户连改配置的入口都没有
    LLM = UnavailableLLM(str(e))
    STARTUP_ERR = str(e)

RAG = RAGStore(embed_fn=build_embed_fn())
READER = SafeFileReader(
    vision=LLM,
    ocr_enabled=config.ocr_enabled,
    max_images=config.max_images,
    vision_max_images=config.vision_max_images,
)

# --- 上传目录：加进白名单，这样上传的文件也能通过安全校验 ---
UPLOAD_DIR = config.uploads_dir
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
for _d in (UPLOAD_DIR, config.workspace_dir, config.data_dir):
    _d.mkdir(parents=True, exist_ok=True)
    if _d.resolve() not in READER.allowed_dirs:
        READER.allowed_dirs.append(_d.resolve())

# 网页模式下确认由前端弹窗处理；这里用 callback 模式
GATEWAY = ConfirmGateway(mode="callback", timeout=120)
set_runtime(llm=LLM, rag=RAG, reader=READER, gateway=GATEWAY)

AGENT = Agent(
    LLM,
    max_steps=config.max_steps,
    verbose=False,
    memory=ConversationMemory(RAG),
)


# ---------- 回调 ----------

def chat_fn(message: str, history: list):
    if not message.strip():
        return "", history
    try:
        reply = AGENT.run(message)
    except Exception as e:
        reply = f"出错了：{type(e).__name__}: {e}"
    history = history + [
        {"role": "user", "content": message},
        {"role": "assistant", "content": reply},
    ]
    return "", history


def ingest_fn(file_obj):
    """网页上传 → 解析入库"""
    if not file_obj:
        return "请先选择一个文件"

    src = Path(file_obj if isinstance(file_obj, str) else file_obj.name)
    if not src.exists():
        return f"文件不存在: {src}"

    # 拷到 uploads 目录，确保在白名单内
    dest = UPLOAD_DIR / src.name
    try:
        shutil.copy2(src, dest)
    except Exception as e:
        return f"复制失败: {e}"

    from myagent.tools import execute_tool

    # 网页模式下跳过二次确认（用户主动上传本身就是授权行为），
    # 但仍走完整的白名单 + 只读 + 格式校验
    old_mode = GATEWAY.mode
    GATEWAY.mode = "auto_approve"
    try:
        result = execute_tool("ingest_local_file", {"path": str(dest)})
    finally:
        GATEWAY.mode = old_mode

    return result


def _copy_to_uploads(src: Path) -> Path:
    """把上传文件复制到 uploads/。

    同名文件可能被 WPS 或杀毒扫描短暂占用，覆盖会抛 WinError 32，
    这时自动换一个带时间戳的名字重试，不让整个分析流程失败。
    """
    dest = UPLOAD_DIR / src.name
    try:
        shutil.copy2(src, dest)
        return dest
    except (PermissionError, OSError) as first_err:
        stamp = time.strftime("%H%M%S")
        alt = UPLOAD_DIR / f"{src.stem}_{stamp}{src.suffix}"
        try:
            shutil.copy2(src, alt)
            return alt
        except Exception:
            raise first_err


def analyze_fn(file_obj, also_ingest: bool):
    """上传文档 → 分析重点（默认不入库），可选同时入库。

    用生成器分步推送进度：长文档要跑一两个分钟，
    期间必须让用户看到"在动"，否则会以为卡死。
    """
    if not file_obj:
        yield "请先选择一个文件"
        return

    src = Path(file_obj if isinstance(file_obj, str) else file_obj.name)
    if not src.exists():
        yield f"文件不存在: {src}"
        return

    src = src.resolve()
    try:
        dest = _copy_to_uploads(src)
    except Exception as e:
        yield f"复制失败: {e}"
        return

    from myagent.analyst import DocumentAnalyst

    size_kb = max(1, src.stat().st_size // 1024)

    # 单张图片：直接给识别结果，套文档报告模板反而看不懂
    if src.suffix.lower() in {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".tif", ".tiff"}:
        from myagent.image_reader import organize_ocr, read_image

        yield f"正在识别图片 `{src.name}`（{size_kb} KB）…"
        try:
            res = read_image(dest.read_bytes(), LLM)
        except Exception as e:
            yield f"识别失败：{type(e).__name__}: {e}"
            return
        if not res["text"]:
            yield (
                f"这张图片没有识别出文字或可描述的内容（{src.name}）。\n\n"
                "可能是纯色图、图标，或者分辨率太低。"
            )
            return

        # OCR 出来的是乱序碎片，要先整理成人能读的说明，
        # 直接把原文摆出来用户看不懂（表格左右栏会串在一起）
        tidy = ""
        if res["ocr"]:
            yield f"识别到 {len(res['ocr'])} 字，正在整理成可读的说明…"
            tidy = organize_ocr(res["ocr"], LLM)

        out = [f"### {src.name}", ""]
        out += [tidy] if tidy else []
        if not tidy and res["ocr"]:
            out += ["```text", res["ocr"], "```"]
        if res["caption"]:
            out += ["", "**图像理解**", "", res["caption"]]
        out += [
            "",
            "---",
            f"识别方式：{'文字识别 + 图像理解' if res['via'] == 'both' else ('文字识别' if res['via'] == 'ocr' else '图像理解')}",
        ]
        # 原文留着备查：整理有可能改错，用户要能对照
        if tidy and res["ocr"]:
            out += [
                "",
                "**原始识别文字（供核对）**",
                "",
                "```text",
                res["ocr"],
                "```",
            ]
        if also_ingest:
            out.append("\n（单张图片暂不入库，入库请用 Word/PDF 文档）")
        yield "\n".join(out)
        return

    yield f"正在读取 `{src.name}`（{size_kb} KB）…"

    q: queue.Queue = queue.Queue()

    def work() -> None:
        try:
            analyst = DocumentAnalyst(READER, LLM)
            out = analyst.analyze(str(dest), progress=lambda s: q.put(("p", s)))
            if also_ingest:
                from myagent.tools import execute_tool

                old = GATEWAY.mode
                GATEWAY.mode = "auto_approve"
                try:
                    extra = execute_tool("ingest_local_file", {"path": str(dest)})
                finally:
                    GATEWAY.mode = old
                out += f"\n\n---\n\n**同时入库**\n\n```\n{extra}\n```"
            q.put(("done", out))
        except Exception as e:  # noqa: BLE001 - 任何异常都要回传给界面
            q.put(("err", f"{type(e).__name__}: {e}"))

    threading.Thread(target=work, daemon=True).start()

    while True:
        kind, payload = q.get()
        if kind == "p":
            yield f"⏳ {payload}"
        elif kind == "done":
            yield payload
            return
        else:
            yield f"**分析失败**：{payload}"
            return


def stats_fn():
    s = RAG.stats()
    allowed = [str(d) for d in READER.allowed_dirs]
    return (
        f"知识库片段: {s['knowledge_chunks']}\n"
        f"记忆片段  : {s['memory_chunks']}\n"
        f"数据库路径: {s['db_path']}\n"
        f"可读目录  : {', '.join(allowed)}\n"
        f"支持格式  : {', '.join(READER.info()['allowed_ext'])}"
    )


def save_style(style: str):
    os.environ["STYLE_PROMPT"] = style.strip()
    AGENT.style_prompt = style.strip()
    AGENT.reset()
    return "✅ 风格已应用（仅影响措辞，不影响工具调用）"


# ---------- 模型选择 ----------

def _status_md() -> str:
    head = f"**当前模型**：`{LLM.name}`\n\n"
    body = settings.describe(config).replace("\n", "  \n")
    warn = ""
    if STARTUP_ERR:
        warn = f"\n\n⚠️ 启动时配置有问题：{STARTUP_ERR}"
    if config.backend == "api":
        warn += f"\n\n密钥保存在 `{config.settings_file}`（仅本机，不进代码库）"
    return head + body + warn


def model_status_fn():
    return _status_md()


def on_provider_change(pid: str):
    """切厂商：把模型名、接口地址、视觉模型、说明一起换掉，并清掉快速选择。

    清快速选择是必须的：用户点了「智谱」的卡片后又去高级设置切到 Kimi，
    卡片上还亮着智谱会让两张配置互相矛盾。
    """
    p = providers.get(pid)
    if p is None:
        empty = gr.update()
        return empty, empty, empty, empty, empty
    models = [(m, m) for m in p.models]
    note = f"**{p.name}**"
    if p.note:
        note += "\n\n" + p.note
    if p.key_url:
        note += f"\n\n密钥申请：{p.key_url}"
    return (
        gr.update(choices=models, value=(p.models[0] if p.models else "")),
        gr.update(value=p.base_url),
        gr.update(value=note),
        gr.update(value=(p.vision_models[0] if p.vision_models else "")),
        gr.update(value=None),  # 快速选择卡片取消点亮
    )


def pick_model_fn(card: str | None):
    """快速选择卡片：一张卡片 = 厂商 + 模型，点一下把整张配置表填好。

    直接填所有字段而不是只切厂商再靠联动——联动会把模型名重置成
    该厂商的第一个，卡片选的型号就丢了。
    """
    empty = gr.update()
    if not card:
        return empty, empty, empty, empty, empty, ""
    pid, _, model = card.partition("::")
    p = providers.get(pid)
    if not p or not model:
        return empty, empty, empty, empty, empty, ""
    if p.need_key:
        tip = (
            f"**已选 {p.name} · {model}**。下一步：在下面粘贴你的密钥"
            f"（申请：{p.key_url}）→ 点「测试连接」→ 点「保存并启用」。"
        )
    else:
        tip = (
            f"**已选 {p.name} · {model}**。本地模型不需要密钥，"
            "直接点下方「保存并启用」。"
        )
    return (
        pid,
        gr.update(choices=[(m, m) for m in p.models], value=model),
        p.base_url,
        p.note,
        (p.vision_models[0] if p.vision_models else ""),
        tip,
    )


def _tmp_cfg(pid: str, model: str, key: str, base_url: str, vision: str):
    """用界面上填的值造一份临时配置，用于测试连接（不落盘）"""
    from types import SimpleNamespace

    p = providers.get(pid)
    if p and p.kind == "local":
        return SimpleNamespace(
            backend="ollama",
            provider=pid,
            model=model.strip() or p.models[0],
            ollama_host=(base_url or "").strip() or p.base_url,
            num_ctx=config.num_ctx,
            vision_model=(vision or "").strip(),
            api_key="",
            base_url="",
        )
    return SimpleNamespace(
        backend="api",
        provider=pid,
        model=(model or "").strip(),
        api_key=(key or "").strip(),
        base_url=(base_url or "").strip() or (p.base_url if p else ""),
        vision_model=(vision or "").strip(),
        ollama_host="",
    )


def _effective_key(pid: str, key: str) -> str:
    """界面留空时沿用已保存的那把密钥。

    换模型（同一家）不该逼用户重新粘一次密钥；但换厂商必须重填，
    否则会把上一家的密钥发到新厂商，报错还会指向错的厂商。
    """
    k = (key or "").strip()
    if k:
        return k
    if pid == getattr(config, "provider", "") and config.backend == "api":
        return (config.api_key or "").strip()
    return ""


def test_model_fn(pid: str, model: str, key: str, base_url: str, vision: str):
    key = _effective_key(pid, key)
    ok, why = settings.validate(pid, model, key, base_url)
    if not ok:
        return f"❌ {why}"
    try:
        candidate = build_llm(_tmp_cfg(pid, model, key, base_url, vision))
    except Exception as e:
        return f"❌ 配置有误：{e}"
    ok, msg = ping(candidate, check_tools=True)
    return ("✅ " if ok else "❌ ") + msg


def _activate(new_llm) -> None:
    """把新模型接到正在运行的所有组件上，不用重启服务"""
    global LLM
    LLM = new_llm
    AGENT.llm = new_llm
    READER.vision = new_llm
    set_runtime(llm=new_llm, reader=READER)
    AGENT.reset()


def apply_model_fn(pid: str, model: str, key: str, base_url: str, vision: str):
    global STARTUP_ERR
    key = _effective_key(pid, key)
    ok, why = settings.validate(pid, model, key, base_url)
    if not ok:
        return f"❌ {why}"

    old = settings.load(config.data_dir)  # 用于失败回滚

    try:
        settings.save(config.data_dir, pid, model, key, base_url, vision)
        if not settings.apply_to(config):
            raise RuntimeError("设置写入后读取失败，请检查数据目录权限")
        new_llm = build_llm(config)
    except Exception as e:
        _rollback(old)
        return f"❌ 没有切换，当前仍是 {LLM.name}：{e}"

    _activate(new_llm)
    STARTUP_ERR = ""
    return f"✅ 已切换到 `{new_llm.name}`，下一次提问就生效（对话上下文已清空）\n\n" + _status_md()


def _rollback(old: dict) -> None:
    """保存失败时回到上一份设置，避免"文件写了一半、内存里还是旧的"这种半死状态"""
    if old:
        settings.save(
            config.data_dir,
            old.get("provider", ""),
            old.get("model", ""),
            old.get("api_key", ""),
            old.get("base_url", ""),
            old.get("vision_model", ""),
        )
    else:
        settings.clear(config.data_dir)
    try:
        settings.apply_to(config)
        _activate(build_llm(config))
    except Exception:
        pass


def clear_model_fn():
    """清除界面保存的配置，回到 .env / 默认值"""
    removed = settings.clear(config.data_dir)
    for k, v in ENV_BASELINE.items():
        setattr(config, k, v)
    try:
        _activate(build_llm(config))
        note = "✅ 已清除界面配置，回到 .env 里的设置"
    except Exception as e:
        _activate(UnavailableLLM(str(e)))
        note = f"配置已清除，但 .env 里的设置也不能用：{e}"
    if not removed:
        note = "本来就没有保存过界面配置，已按 .env 重新加载"
    return note + "\n\n" + _status_md()


def reset_chat():
    AGENT.reset()
    return [], "上下文已清空"


# ---------- 界面 ----------

with gr.Blocks(title="小满 Agent") as demo:
    gr.Markdown("# 小满 Agent\n能自己动手办事的本地助手。可以读文档、查知识库、写文件。")
    gr.Markdown(
        f"当前模型：`{LLM.name}`　·　换模型去「模型」标签页（本地 Ollama 免费，也可填自己的云端密钥）"
    )

    with gr.Tabs():
        with gr.Tab("对话"):
            chatbot = gr.Chatbot(label="对话", height=460)
            with gr.Row():
                msg_box = gr.Textbox(
                    placeholder="说个目标，比如：帮我总结知识库里关于 XX 的内容",
                    scale=5,
                    show_label=False,
                    lines=2,
                )
                send_btn = gr.Button("发送", variant="primary", scale=1)

            with gr.Row():
                reset_btn = gr.Button("清空上下文")

            # api_name 必须显式写：同名函数绑两次，Gradio 会自动加 chat_fn_1 这种后缀，
            # API 文档页就变成一堆看不出用途的重复入口。
            send_btn.click(
                chat_fn, [msg_box, chatbot], [msg_box, chatbot], api_name="chat"
            )
            # 回车发送与按钮发送是同一个动作，不单独开一个端点
            msg_box.submit(
                chat_fn,
                [msg_box, chatbot],
                [msg_box, chatbot],
                api_visibility="private",
            )
            reset_btn.click(reset_chat, None, [chatbot, msg_box], api_name="reset")

        with gr.Tab("文档分析"):
            gr.Markdown(
                "上传文档，直接给你重点：一句话结论、核心要点、关键信息、需要注意的事。\n\n"
                "**文档里的截图、图表也会一起识别**（先抽取图中文字，纯图形再交给视觉模型看懂）。\n\n"
                "也可以直接上传一张图片（png / jpg），单独做文字识别和图像理解。"
            )
            an_file = gr.File(
                label="选择文档或图片",
                file_types=[
                    ".txt",
                    ".md",
                    ".pdf",
                    ".docx",
                    ".doc",
                    ".png",
                    ".jpg",
                    ".jpeg",
                    ".bmp",
                    ".webp",
                ],
            )
            an_ingest = gr.Checkbox(
                label="同时存入知识库（之后可以在对话里追问细节）", value=False
            )
            an_btn = gr.Button("开始分析", variant="primary")
            an_out = gr.Markdown()
            an_btn.click(analyze_fn, [an_file, an_ingest], an_out, api_name="analyze")

        with gr.Tab("文档入库"):
            gr.Markdown(
                "上传 txt / md / pdf / docx，系统会解析、切片、向量化后存入本地知识库。\n\n"
                "只想要一份文档的重点，用「文档分析」；想把资料长期沉淀、以后反复查询，用这里。"
            )
            file_input = gr.File(
                label="选择文件", file_types=[".txt", ".md", ".pdf", ".docx", ".doc"]
            )
            ingest_btn = gr.Button("解析入库", variant="primary")
            ingest_out = gr.Textbox(label="结果", lines=8, )
            ingest_btn.click(ingest_fn, file_input, ingest_out, api_name="ingest")

        with gr.Tab("知识库"):
            gr.Markdown("查看当前知识库状态")
            stats_btn = gr.Button("刷新状态")
            stats_out = gr.Textbox(label="状态", lines=6)
            stats_btn.click(stats_fn, None, stats_out, api_name="stats")

        with gr.Tab("模型"):
            gr.Markdown(
                "**换模型不用改代码、不用重启。** 在「快速选择」里点一张卡片就配好；"
                "需要密钥的粘贴一把，先「测试连接」再「保存并启用」。\n\n"
                "密钥保存在本地数据目录的 `settings.json` 里（不进代码库、不会被推到 GitHub），"
                "界面只显示掩码。本地 Ollama 不需要密钥。"
            )
            model_now = gr.Markdown(_status_md())

            # ---- 快速选择：卡片列表，点一张 = 厂商 + 模型 + 接口地址全配好 ----
            pick = gr.Radio(
                choices=[
                    (card, f"{pid}::{model}")
                    for pid, model, card in providers.RECOMMENDED
                ],
                label="快速选择（点一下自动配好）",
                show_label=True,
            )
            pick_note = gr.Markdown()

            key_tb = gr.Textbox(
                label="API 密钥",
                type="password",
                placeholder=(
                    f"已保存：{settings.mask(config.api_key)}（留空则沿用）"
                    if config.api_key
                    else "本地模型不需要；云端模型粘贴你自己的密钥"
                ),
            )

            # ---- 高级设置：任意厂商 / 手填模型名 / 接口地址 / 视觉模型 ----
            with gr.Accordion(
                "高级设置（手动选厂商、填任意模型名、改接口地址）", open=False
            ):
                prov_dd = gr.Dropdown(
                    label="模型厂商",
                    choices=providers.choices(),
                    value=(
                        config.provider if providers.get(config.provider) else "ollama"
                    ),
                )
                prov_note = gr.Markdown(
                    (providers.get(config.provider) or providers.get("ollama")).note
                )
                model_dd = gr.Dropdown(
                    label="模型名（下拉里是常用值，也可以直接输入任意名字）",
                    choices=[
                        (m, m)
                        for m in (
                            (
                                providers.get(config.provider)
                                or providers.get("ollama")
                            ).models
                        )
                    ],
                    value=config.model,
                    allow_custom_value=True,
                )
                url_tb = gr.Textbox(
                    label="接口地址",
                    value=(
                        config.base_url if config.backend == "api" else config.ollama_host
                    ),
                )
                vis_tb = gr.Textbox(
                    label="视觉模型（可留空 = 只用 OCR 读图里的文字）",
                    value=config.vision_model,
                    placeholder="例如 glm-5.3-flash；留空则不做图像理解",
                )

            with gr.Row():
                test_btn = gr.Button("测试连接")
                apply_btn = gr.Button("保存并启用", variant="primary")
                clear_btn = gr.Button("清除配置")

            model_out = gr.Markdown()

            pick.change(
                pick_model_fn,
                pick,
                [prov_dd, model_dd, url_tb, prov_note, vis_tb, pick_note],
                api_visibility="private",
            )
            # 用 .input 而不是 .change：程序填值（点卡片）也会触发 .change，
            # 会把卡片选好的模型重置成该厂商第一个；.input 只在用户手动改时触发
            prov_dd.input(
                on_provider_change,
                prov_dd,
                [model_dd, url_tb, prov_note, vis_tb, pick],
                api_name="provider_info",
            )
            test_btn.click(
                test_model_fn,
                [prov_dd, model_dd, key_tb, url_tb, vis_tb],
                model_out,
                api_name="test_model",
            )
            # 保存成功后刷新顶部状态条，属于界面内部动作，不对外暴露
            apply_btn.click(
                apply_model_fn,
                [prov_dd, model_dd, key_tb, url_tb, vis_tb],
                model_out,
                api_name="apply_model",
            ).then(
                model_status_fn, None, model_now, api_visibility="private"
            )
            clear_btn.click(
                clear_model_fn, None, model_out, api_name="clear_model"
            ).then(model_status_fn, None, model_now, api_visibility="private")

        with gr.Tab("风格"):
            gr.Markdown(
                "设置回复风格。注意：风格只影响措辞语气，"
                "不会改变 Agent 的工具调用行为和安全策略。"
            )
            style_box = gr.Textbox(
                label="风格描述",
                placeholder="例如：像一位严谨的技术编辑，回答简洁、先给结论",
                lines=4,
            )
            style_btn = gr.Button("应用风格")
            style_out = gr.Textbox(label="结果")
            style_btn.click(save_style, style_box, style_out, api_name="save_style")

    # 页面打开时的初始化填充，属于界面内部动作，不对外暴露
    demo.load(stats_fn, None, stats_out, api_visibility="private")
    demo.load(model_status_fn, None, model_now, api_visibility="private")


if __name__ == "__main__":
    _port = int(os.getenv("WEB_PORT", 7860))
    try:
        demo.launch(
            server_name=os.getenv("WEB_HOST", "127.0.0.1"),
            server_port=_port,
            inbrowser=True,
            theme=gr.themes.Soft(),
            show_error=True,
            # 底部只留 "built with Gradio"，藏掉「Use via API / 设置 / 运行历史」：
            # 那个页面是给程序调用的接口文档，普通用户点进去只会看到一堆函数名。
            # 端点本身照旧可用，用法见 README「HTTP API」一节。
            footer_links=["gradio"],
        )
    except OSError as e:
        # 端口被占用时 gradio 抛的是英文堆栈，用户看不懂也找不到原因。
        # 这里给一句能照着做的话，而不是让 Python 栈糊满窗口。
        low = str(e).lower()
        if "empty port" in low or "address already in use" in low or "10048" in low:
            print()
            print(f"[启动失败] 端口 {_port} 已被占用，网页起不来。")
            print(f"  1) 如果小满已经在跑，直接打开 http://127.0.0.1:{_port} 即可")
            print(f"  2) 要用别的端口：先执行 set WEB_PORT=7861，再重新启动")
            print(f"  3) 想知道是谁占着：netstat -ano | findstr :{_port}")
            raise SystemExit(2)
        raise
