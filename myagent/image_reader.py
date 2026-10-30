"""
文档内嵌图片的理解

两层策略，按成本从低到高：
1. OCR（CPU，约 0.5 秒/张）—— 把图里的文字、数字、表格原样抽出来。
   文档里的"数据截图""Excel 截图""扫描页"靠这一层就够用了。
2. 多模态模型（GPU，约 3-8 秒/张）—— OCR 一个字都抽不出来时，
   说明这是照片、示意图、柱状图之类的纯图形，交给视觉模型讲一句它在表达什么。

为什么要分两层而不是全部丢给视觉模型：
3b 的视觉模型读密集小字很容易看错、看漏，而数字恰恰是分析报告里最不能错的东西。
OCR 对印刷体数字几乎零错误，所以优先 OCR，视觉模型只做兜底。

全程只处理内存里的字节，不落盘、不改原文件。
"""

import base64
import io
import re
import threading

# 小于这个体积的图基本是空文件或极简图标
MIN_BYTES = 800
# 短边小于这个像素的，是图标、项目符号、分隔线，没有分析价值
MIN_SIDE = 48

# OCR 抽出少于这么多字，判定为"图里没有实质文字"，才动用视觉模型
OCR_MIN_CHARS = 10

# 送 OCR 前把长边限制在这个范围：太大慢，太小糊
OCR_MAX_SIDE = 2200
OCR_UPSCALE_BELOW = 700

# 送视觉模型前压到长边 1280 的 JPEG，省显存也省时间
VISION_MAX_SIDE = 1280
VISION_QUALITY = 85

VISION_PROMPT = """这是一份文档里的插图。请用中文简明说明它展示了什么，2-4 句话：

- 图表：说明图表类型、横纵轴含义、整体趋势、关键数值或峰值
- 流程图/架构图：说明流程步骤或组成部分之间的关系
- 截图/照片：说明画面主体和可见的关键信息
- 图上有文字或数字的，尽量原样抄出来

只输出描述本身，不要"这张图片展示了"这类开场白。看不清就说看不清，不要猜。"""

_OCR_LOCK = threading.Lock()
_OCR = None
_OCR_ERROR = ""

# 视觉模型是否就绪的探测结果缓存（model 名 → 是否可用）
_VISION_OK: dict[str, bool] = {}


def _engine():
    """RapidOCR 加载一次约 1-2 秒，做成单例复用；加载失败也只记一次"""
    global _OCR, _OCR_ERROR
    if _OCR is not None or _OCR_ERROR:
        return _OCR
    with _OCR_LOCK:
        if _OCR is not None or _OCR_ERROR:
            return _OCR
        try:
            import logging

            # rapidocr 初始化时会刷一堆 [INFO] 模型路径日志，控制台看着很乱
            for name in ("RapidOCR", "rapidocr", "RapidOCR.rapidocr"):
                logging.getLogger(name).setLevel(logging.WARNING)

            from rapidocr import RapidOCR

            _OCR = RapidOCR()
            for name in ("RapidOCR", "rapidocr", "RapidOCR.rapidocr"):
                logging.getLogger(name).setLevel(logging.WARNING)
        except Exception as e:  # 依赖缺失 / 模型文件损坏
            _OCR_ERROR = f"{type(e).__name__}: {e}"
    return _OCR


def ocr_status() -> str:
    """给界面用：OCR 是否可用"""
    eng = _engine()
    if eng is not None:
        return "ok"
    return f"unavailable: {_OCR_ERROR}"


def _decode(data: bytes):
    """字节 → RGB 图像，顺便把透明底压成白底（否则视觉模型看到一片黑）"""
    from PIL import Image, ImageOps

    img = Image.open(io.BytesIO(data))
    img = ImageOps.exif_transpose(img)  # 手机拍的图会自动带旋转标记
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[-1])
        img = bg
    else:
        img = img.convert("RGB")
    return img


def _resize(img, max_side: int, upscale_below: int = 0):
    from PIL import Image

    w, h = img.size
    long_side = max(w, h)
    if long_side > max_side:
        scale = max_side / long_side
        return img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
    if upscale_below and long_side < upscale_below:
        scale = upscale_below / long_side
        return img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    return img


def _is_blank(img) -> bool:
    """近乎纯色（背景图、空白页）直接跳过，不然要白跑一次 OCR 加一次视觉模型"""
    try:
        from PIL import ImageStat

        return ImageStat.Stat(img.convert("L")).stddev[0] < 3.0
    except Exception:
        return False


def ocr_image(img) -> str:
    """对一张 PIL 图做 OCR，返回按行拼接的文本"""
    eng = _engine()
    if eng is None:
        return ""
    try:
        import numpy as np

        arr = np.array(_resize(img, OCR_MAX_SIDE, OCR_UPSCALE_BELOW))
        with _OCR_LOCK:  # onnxruntime session 复用，串行更稳
            res = eng(arr)
    except Exception:
        return ""

    # rapidocr 3.x 返回对象（.txts），2.x 返回 (list, elapse)
    txts = getattr(res, "txts", None)
    if txts is None and isinstance(res, (list, tuple)):
        try:
            txts = [item[1] for item in res[0]] if res and res[0] else []
        except Exception:
            txts = []

    items = [t.strip() for t in (txts or []) if t and t.strip()]
    boxes = getattr(res, "boxes", None)

    # 重排为阅读顺序：y 相近的框归为同一行，行内按 x 从左到右。
    # 直接按 y 排不行——同一行左右两栏常有几像素的高低差，会把一行拆散
    if boxes is not None and items and len(boxes) == len(items):
        placed = []
        for i, t in enumerate(items):
            try:
                b = boxes[i]
                y = min(float(p[1]) for p in b)
                x = min(float(p[0]) for p in b)
                h = max(float(p[1]) for p in b) - y
            except Exception:
                y, x, h = float(i), 0.0, 0.0
            placed.append([y, x, h, t])

        placed.sort(key=lambda r: (r[0], r[1]))
        heights = sorted(r[2] for r in placed if r[2] > 0)
        ref_h = heights[len(heights) // 2] if heights else 20.0

        rows: list[list] = []
        for r in placed:
            if rows and r[0] - rows[-1][0][0] < ref_h * 0.6:
                rows[-1].append(r)
            else:
                rows.append([r])

        items = []
        for row in rows:
            row.sort(key=lambda r: r[1])
            items.append("  ".join(t for _, _, _, t in row))

    lines = items
    return _clean_ocr("\n".join(lines))


_CORE_CHAR = re.compile(r"[\u4e00-\u9fffA-Za-z0-9]")


def _clean_ocr(text: str) -> str:
    """去掉 OCR 噪声。

    线条、边框、坐标轴常被识别成 "__" "—" "|" 这类纯符号，
    它们没有信息量，但会让"这张图有文字"的判断成立，
    从而挡住后面本该做的图像理解。
    """
    out: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        # 必须有实际的汉字 / 字母 / 数字，且至少两个
        if len(_CORE_CHAR.findall(s)) < 2:
            continue
        out.append(s)
    return "\n".join(out)


def vision_available(llm=None) -> bool:
    """视觉模型是否真的在本地就绪。

    不做这个探测的话，模型还没下载完时每张图都要等一次 60 秒超时，
    一份 12 张图的文档会白等十几分钟。
    """
    name = getattr(llm, "vision_name", "") or ""
    if not name:
        return False
    if name in _VISION_OK:
        return _VISION_OK[name]

    ok = True
    host = getattr(llm, "_host", "") or ""
    if host:
        try:
            import json
            import urllib.request

            with urllib.request.urlopen(host.rstrip("/") + "/api/tags", timeout=5) as r:
                data = json.load(r)
            have = {m.get("name", "") for m in data.get("models", [])}
            model = name.split("/", 1)[-1] if "/" in name else name
            ok = model in have
        except Exception:
            ok = True  # 探测本身失败（比如换了后端），照常尝试

    _VISION_OK[name] = ok
    return ok


def describe_image(img, llm=None) -> str:
    """让多模态模型看图，返回一句话描述；后端不支持则返回空"""
    if llm is None or not getattr(llm, "vision_name", ""):
        return ""
    if not vision_available(llm):
        return ""
    try:
        small = _resize(img, VISION_MAX_SIDE)
        buf = io.BytesIO()
        small.save(buf, format="JPEG", quality=VISION_QUALITY)
        b64 = base64.b64encode(buf.getvalue()).decode()
        out = llm.vision_chat(b64, VISION_PROMPT, mime="image/jpeg")
        return (out or "").strip()
    except Exception:
        return ""


def read_image(data: bytes, llm=None, allow_vision: bool = True) -> dict:
    """
    一张图 → 可读文本。

    返回 {"text": 正文, "ocr": OCR原文, "caption": 视觉描述, "via": 走的哪条路}
    via 取值：ocr / vision / both / none / skipped
    """
    empty = {"text": "", "ocr": "", "caption": "", "via": "none"}

    if not data or len(data) < MIN_BYTES:
        return {**empty, "via": "skipped"}
    try:
        img = _decode(data)
    except Exception:
        return empty

    if min(img.size) < MIN_SIDE:
        return {**empty, "via": "skipped"}

    if _is_blank(img):
        return {**empty, "via": "skipped"}

    text = ocr_image(img)
    caption = ""

    # 文字太少才看图意：既省钱，也避免视觉模型把 OCR 已经读准的数字改错
    if allow_vision and len(text) < OCR_MIN_CHARS:
        caption = describe_image(img, llm)

    if text and caption:
        merged = f"{text}\n（图像内容：{caption}）"
        via = "both"
    elif text:
        merged = text
        via = "ocr"
    elif caption:
        merged = f"（图像内容：{caption}）"
        via = "vision"
    else:
        merged = ""
        via = "none"

    return {"text": merged, "ocr": text, "caption": caption, "via": via}


ORGANIZE_PROMPT = """下面是从一张图片里 OCR 出来的文字，顺序和排版都是乱的：表格的左右栏被串在一起，还可能混进浏览器、网页、软件的界面文字。

请把它整理成能看懂的中文：

- 第一句说清这张图是什么、在讲什么
- 如果是表格或步骤清单，还原成有条理的列表，每条写成「项目：说明」
- 保留原文的命令、文件名、数字，不要改写术语
- 内容有缺失或对不上就如实说，不要补原文没有的东西

一定要剔除下面这些界面噪声，它们不属于正文：
- 浏览器地址栏、标签页标题、最小化/关闭按钮、favicon
- 网页上的标题、说明段落、上传框、按钮文字（如"开始分析""选择文件""同时存入知识库"）
- 文件名和文件大小（如 xxx.jpg 812.8 KB）
- 时间显示、页码、状态栏、底部工具栏文字（如"通过API使用""使用Gradio构建""设置"）

只输出整理结果，不要任何开场白。

OCR 原文：
---
{text}
---
"""

# 文字太少就不必整理了，原样返回反而更准
ORGANIZE_MIN_CHARS = 100


def organize_ocr(text: str, llm=None) -> str:
    """把乱序的 OCR 原文整理成人能读的结构化说明。

    这一步用的是文本模型，不需要视觉模型：
    OCR 只负责"把字抠出来"，至于这些字在讲什么、表格的哪一栏配哪一行，
    要交给模型重新组织，否则用户看到的是一堆碎片。
    """
    if not text or len(text.strip()) < ORGANIZE_MIN_CHARS or llm is None:
        return ""
    try:
        resp = llm.chat(
            [{"role": "user", "content": ORGANIZE_PROMPT.format(text=text.strip())}],
            options={"temperature": 0.1, "num_predict": 1200},
        )
        return (resp.get("content") or "").strip()
    except Exception:
        return ""


def release_vision(llm=None) -> None:
    """把视觉模型从显存里卸掉。

    8G 显卡放不下"7B 文本模型 + 3B 视觉模型"两份权重，
    图片读完后继续占着会把文本模型挤到 CPU，分析速度掉好几倍。
    Ollama 的 keep_alive=0 表示用完立即卸载。
    """
    name = getattr(llm, "vision_name", "") or ""
    host = getattr(llm, "_host", "") or ""
    if not name or not host:
        return
    model = name.split("/", 1)[-1] if "/" in name else name
    try:
        import json
        import urllib.request

        payload = json.dumps({"model": model, "keep_alive": 0}).encode()
        req = urllib.request.Request(
            host.rstrip("/") + "/api/generate",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=8).read()
    except Exception:
        pass  # 卸载失败不影响分析结果，Ollama 会按默认 5 分钟自行回收
