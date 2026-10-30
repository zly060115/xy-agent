# 配置：改这里就能切换模型后端
# 本地试用阶段 → backend = "ollama"
# 以后接 API ↘ backend = "api"，再填下面三项

from dataclasses import dataclass


@dataclass
class Config:
    # ---- 后端选择：ollama（本地） 或 api（云端） ----
    backend: str = "ollama"

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


config = Config()
