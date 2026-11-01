"""
运行时装配

网页端（web.py）和 MCP 服务（mcp_server.py）是两个进程，但需要的
东西完全一样：读配置 → 建模型 → 建向量库 → 建安全读取器 → 装进 runtime。

这段原本写在 web.py 顶层的 50 行里。MCP 如果照着复制一份，两处迟早
会漂移 —— 比如 settings.json 与 .env 的优先级、视觉模型的开关，
这种不一致排查起来非常费劲（"网页能用的模型，MCP 里报错"）。
所以只抽装配，不含任何界面逻辑。
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# MCP 服务由外部宿主（Claude Desktop / Cursor）拉起，工作目录不一定
# 是项目根，所以这里按文件位置定位 .env
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from . import providers, settings
from .config import config
from .llm import UnavailableLLM, build_llm
from .rag import RAGStore, build_embed_fn
from .safe_reader import SafeFileReader


def load_env_config() -> dict:
    """
    把环境变量覆盖到 config，返回"只由 .env 决定"的快照。

    这个快照是界面上「清除配置」的回归点：删掉用户选的模型后，
    应该回到 .env 描述的状态，而不是回到默认值。
    """
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
    return {
        "backend": config.backend,
        "model": config.model,
        "ollama_host": config.ollama_host,
        "api_key": config.api_key,
        "base_url": config.base_url,
        "vision_model": config.vision_model,
        "provider": "ollama" if config.backend == "ollama" else providers.guess_id(config.base_url),
    }


def apply_saved_or_env(baseline: dict) -> None:
    """界面里存过的模型选择优先于 .env：界面是显式动作，.env 可能早就过期。"""
    if not settings.apply_to(config):
        config.provider = baseline["provider"]


def build_llm_or_unavailable() -> tuple[object, str]:
    """
    模型建不起来也要能继续跑。

    网页端：否则用户连改配置的入口都没有。
    MCP 端：否则宿主拉起的进程会直接退出，只留下一条看不懂的报错。
    两边都用 UnavailableLLM 顶上，把原因带在对话里。
    """
    try:
        return build_llm(config), ""
    except Exception as e:
        return UnavailableLLM(str(e)), str(e)


def build_rag() -> RAGStore:
    return RAGStore(embed_fn=build_embed_fn())


def build_reader(llm) -> SafeFileReader:
    return SafeFileReader(
        vision=llm,
        ocr_enabled=config.ocr_enabled,
        max_images=config.max_images,
        vision_max_images=config.vision_max_images,
    )


def allow_dirs(reader: SafeFileReader, *dirs: Path) -> None:
    """把目录加进读取白名单（不存在就建），上传目录/工作区要用。"""
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)
        if d.resolve() not in reader.allowed_dirs:
            reader.allowed_dirs.append(d.resolve())
