# 配置：改这里就能切换模型后端
# 本地试用阶段 → backend = "ollama"
# 以后接 API ↘ backend = "api"，再填下面三项

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# 无论从哪个目录启动，都加载项目根的 .env
load_dotenv(Path(__file__).resolve().parent.parent / ".env")


def _default_data_dir() -> Path:
    """数据目录：记忆库/知识库/上传文件/工作区的家。

    优先取 .env 里的 DATA_DIR；没配则放用户主目录 ~/.xy。
    默认在代码目录之外，代码怎么搬、开源、重装都不影响积累的数据。
    """
    env = (os.getenv("DATA_DIR") or "").strip()
    if env:
        return Path(env).expanduser()
    return Path.home() / ".xy"


@dataclass
class Config:
    # ---- 后端选择：ollama（本地） 或 api（云端） ----
    backend: str = "ollama"

    # ---- 厂商（对应 myagent/providers.py 里的 id，界面选择后写入） ----
    provider: str = "ollama"

    # ---- 本地 Ollama ----
    model: str = "qwen2.5:7b"          # 推荐 7b：8G 显存刚好，工具调用稳
    ollama_host: str = "http://localhost:11434"

    # ---- 云端 API（等你以后要用时再填） ----
    api_key: str = ""                   # 例：sk-xxxx
    base_url: str = "https://api.deepseek.com/v1"
    # 常用 base_url：
    #   DeepSeek   https://api.deepseek.com/v1     model="deepseek-chat"
    #   通义千问    https://dashscope.aliyuncs.com/compatible-mode/v1   model="qwen-plus"
    #   智谱GLM    https://open.bigmodel.cn/api/paas/v4   model="glm-4-plus"
    #   OpenAI     https://api.openai.com/v1       model="gpt-4o-mini"

    # ---- 图片理解 ----
    # 文档里嵌的图：先用 OCR 抽文字，抽不出字才交给多模态模型看内容
    vision_model: str = "qwen2.5vl:3b"  # 留空 = 只用 OCR，不调视觉模型
    ocr_enabled: bool = True
    max_images: int = 12                # 单份文档最多处理多少张图
    vision_max_images: int = 6          # 其中最多几张送视觉模型（防跑太久）

    # ---- 运行参数 ----
    max_steps: int = 15
    # 上下文窗口。默认 4096 会截断长文档，这里给到 8192；
    # 8G 显存跑 7B 是安全线，显存更大可以调到 16384
    num_ctx: int = 8192

    # ---- 数据目录（记忆库/知识库/上传文件/工作区） ----
    data_dir: Path = field(default_factory=_default_data_dir)

    @property
    def chroma_dir(self) -> Path:
        """向量库：知识库 + 对话长期记忆"""
        return self.data_dir / "chroma_db"

    @property
    def uploads_dir(self) -> Path:
        """网页上传的原始文档"""
        return self.data_dir / "uploads"

    @property
    def workspace_dir(self) -> Path:
        """Agent 的读写工作区"""
        return self.data_dir / "workspace"

    @property
    def skills_dir(self) -> Path:
        """按需加载的技能目录：{DATA_DIR}/skills/<名字>/SKILL.md"""
        return self.data_dir / "skills"

    @property
    def write_dirs(self) -> list[Path]:
        """除工作区之外，Agent 还能写的目录。

        用户说「把文档放我桌面」时，就该真的落到桌面，而不是塞进工作区
        再让他自己去翻。默认给桌面 / 文档 / 下载三个用户自己的目录，
        系统目录和程序目录永远不在此列（写入前还会再拦一道）。
        要改就设 ALLOWED_WRITE_DIRS，多个用 ; 分隔。
        """
        env = (os.getenv("ALLOWED_WRITE_DIRS") or "").strip()
        if env:
            raw = [Path(x.strip()) for x in re.split(r"[;]", env) if x.strip()]
        else:
            home = Path.home()
            raw = [home / "Desktop", home / "Documents", home / "Downloads"]
        out: list[Path] = []
        for p in raw:
            try:
                p = p.expanduser()
                if p.exists() and p.is_dir():
                    r = p.resolve()
                    if r not in out:
                        out.append(r)
            except OSError:
                continue
        return out

    @property
    def settings_file(self) -> Path:
        """用户选择的模型与密钥（界面保存到这里，不进代码库）"""
        return self.data_dir / "settings.json"


config = Config()

# 界面里保存过的模型选择优先于 .env。
# 放在最后执行：任何入口（main.py / web.py）import 配置时都自动生效。
try:
    from .settings import apply_to as _apply_settings

    _apply_settings(config)
except Exception:
    # 设置文件损坏或首次运行，用默认值启动，不能让程序起不来
    pass

