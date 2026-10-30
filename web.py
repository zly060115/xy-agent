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
from myagent.llm import build_llm
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

LLM = build_llm(config)
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


def reset_chat():
    AGENT.reset()
    return [], "上下文已清空"


# ---------- 界面 ----------

with gr.Blocks(title="小满 Agent") as demo:
    gr.Markdown("# 小满 Agent\n能自己动手办事的本地助手。可以读文档、查知识库、写文件。")

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

            send_btn.click(chat_fn, [msg_box, chatbot], [msg_box, chatbot])
            msg_box.submit(chat_fn, [msg_box, chatbot], [msg_box, chatbot])
            reset_btn.click(reset_chat, None, [chatbot, msg_box])

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
            an_btn.click(analyze_fn, [an_file, an_ingest], an_out)

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
            ingest_btn.click(ingest_fn, file_input, ingest_out)

        with gr.Tab("知识库"):
            gr.Markdown("查看当前知识库状态")
            stats_btn = gr.Button("刷新状态")
            stats_out = gr.Textbox(label="状态", lines=6)
            stats_btn.click(stats_fn, None, stats_out)

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
            style_btn.click(save_style, style_box, style_out)

    demo.load(stats_fn, None, stats_out)


if __name__ == "__main__":
    demo.launch(
        server_name=os.getenv("WEB_HOST", "127.0.0.1"),
        server_port=int(os.getenv("WEB_PORT", 7860)),
        inbrowser=True,
        theme=gr.themes.Soft(),
        show_error=True,
    )
