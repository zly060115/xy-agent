"""
模型厂商预设表

目的：开源用户不必翻文档、改代码，在下拉框里选一家、填上自己的密钥就能用。

设计取舍：
1. 只收录 OpenAI 兼容协议的厂商（本地 Ollama 除外）。这样一套 OpenAICompatLLM
   就能覆盖全部云端厂商，不必为每家写适配器。
2. 模型名一律当"常用值"而非"唯一值"处理：厂商换型号很快（DeepSeek 的
   deepseek-chat 已于 2026-07 弃用、Kimi 的 moonshot-v1 系列已下线），
   所以界面上的模型框允许直接输入任意名字，预设只是省打字。
3. 不联网拉模型列表。智谱、豆包等厂商的 /models 接口不开放，
   统一改成静态预设 + 可手填，行为一致、不会出现"有的能拉有的不能"。
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Provider:
    id: str
    name: str
    kind: str  # local = 本地推理，无需密钥；api = 云端，需要密钥
    base_url: str
    models: tuple[str, ...]
    vision_models: tuple[str, ...] = ()
    key_url: str = ""
    note: str = ""

    @property
    def need_key(self) -> bool:
        return self.kind == "api"


_NOTE_TOOLS = "支持函数调用（本项目的工具调用依赖这一项）"

PROVIDERS: tuple[Provider, ...] = (
    Provider(
        id="ollama",
        name="本地 Ollama（免费，无需密钥）",
        kind="local",
        base_url="http://localhost:11434",
        models=(
            "qwen2.5:7b",
            "qwen2.5:14b",
            "qwen2.5:32b",
            "qwen2.5-coder:7b",
            "deepseek-r1:7b",
            "llama3.1:8b",
        ),
        vision_models=("qwen2.5vl:3b", "qwen2.5vl:7b"),
        note="跑在自己显卡上，数据不出本机。模型名必须已经 ollama pull 过。"
        "8G 显存建议 7b；14b 以上会明显变慢。",
    ),
    Provider(
        id="deepseek",
        name="DeepSeek 深度求索",
        kind="api",
        base_url="https://api.deepseek.com",
        models=("deepseek-flash", "deepseek-v4-pro"),
        vision_models=("deepseek-flash",),
        key_url="https://platform.deepseek.com/api_keys",
        note="便宜、中文强、工具调用稳，最推荐作为本地模型的第一替代。"
        "deepseek-flash（V4.1-Flash）日常够用且能直接读图，v4-pro 适合复杂推理。"
        "模型名以 /v1/models 返回为准，别凭印象填。" + _NOTE_TOOLS,
    ),
    Provider(
        id="dashscope",
        name="阿里云百炼（通义千问）",
        kind="api",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        models=("qwen3.8-max", "qwen3.8-flash", "qwen3.7-plus", "qwen-long"),
        key_url="https://bailian.console.aliyun.com/",
        note="长文档好用（qwen-long 支持超长上下文）。"
        "图片理解需要另填视觉模型名（百炼控制台里的 qwen-vl 系列）。",
    ),
    Provider(
        id="zhipu",
        name="智谱 GLM",
        kind="api",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        models=("glm-5.3", "glm-5.3-flash", "glm-5.2", "glm-4.6"),
        vision_models=("glm-5.3-flash",),
        key_url="https://open.bigmodel.cn/usercenter/apikeys",
        note="glm-5.3-flash 原生多模态，可以直接兼职看图和读文档。",
    ),
    Provider(
        id="moonshot",
        name="月之暗面 Kimi",
        kind="api",
        base_url="https://api.moonshot.cn/v1",
        models=("kimi-k3.1-pro", "kimi-k3", "kimi-k2.7-code", "kimi-k2.6"),
        vision_models=("kimi-k3.1-pro",),
        key_url="https://platform.moonshot.cn/console/api-keys",
        note="超长上下文、原生支持视觉，长合同和长报告一次读完。"
        "旧的 moonshot-v1 系列已下线，别再填。",
    ),
    Provider(
        id="ark",
        name="字节豆包（火山方舟）",
        kind="api",
        base_url="https://ark.cn-beijing.volces.com/api/v3",
        models=("doubao-seed-2-1-pro-260915", "doubao-seed-2-0-lite-260428"),
        key_url="https://console.volcengine.com/ark",
        note="模型名以方舟控制台的「接入点」为准，可能带日期后缀，"
        "建议从控制台复制后粘贴到这里。",
    ),
    Provider(
        id="hunyuan",
        name="腾讯混元（TokenHub）",
        kind="api",
        base_url="https://tokenhub.tencentcloudmaas.com/v1",
        models=("hy3", "hy4-preview"),
        key_url="https://console.cloud.tencent.com/",
        note="hy3 是便宜的长文本档，hy4-preview 支持更长上下文。",
    ),
    Provider(
        id="minimax",
        name="MiniMax",
        kind="api",
        base_url="https://api.minimaxi.com/v1",
        models=("MiniMax-M3",),
        key_url="https://platform.minimaxi.com/",
        note="",
    ),
    Provider(
        id="stepfun",
        name="阶跃星辰 StepFun",
        kind="api",
        base_url="https://api.stepfun.com/v1",
        models=("step-3.7-flash", "step-3.5-flash"),
        key_url="https://platform.stepfun.com/",
        note="",
    ),
    Provider(
        id="siliconflow",
        name="硅基流动 SiliconFlow",
        kind="api",
        base_url="https://api.siliconflow.cn/v1",
        models=(
            "deepseek-ai/DeepSeek-V4-Flash",
            "Qwen/Qwen3-8B",
            "Qwen/Qwen2.5-72B-Instruct",
        ),
        key_url="https://cloud.siliconflow.cn/account/ak",
        note="一个密钥可调很多家开源模型，模型名带前缀（机构/型号），"
        "以「模型广场」里复制的名字为准。",
    ),
    Provider(
        id="openai",
        name="OpenAI",
        kind="api",
        base_url="https://api.openai.com/v1",
        models=("gpt-4o-mini", "gpt-4o"),
        vision_models=("gpt-4o", "gpt-4o-mini"),
        key_url="https://platform.openai.com/api-keys",
        note="国内网络通常需要代理才能访问。模型名迭代快，以官方文档为准，可直接手填。",
    ),
    Provider(
        id="openrouter",
        name="OpenRouter（聚合多家）",
        kind="api",
        base_url="https://openrouter.ai/api/v1",
        models=("openai/gpt-4o-mini", "anthropic/claude-sonnet-4.5"),
        key_url="https://openrouter.ai/keys",
        note="一个密钥调多家模型，模型名形如「厂商/型号」，以官网列表为准。",
    ),
    Provider(
        id="custom",
        name="自定义（任意 OpenAI 兼容接口）",
        kind="api",
        base_url="",
        models=(),
        key_url="",
        note="自建 vLLM、LM Studio、公司内部网关都填这里："
        "接口地址必须写到 /v1 这一层，模型名照服务端实际名字填。",
    ),
)

_BY_ID = {p.id: p for p in PROVIDERS}


# 精选推荐卡片：像应用商店那样，点一张卡片就把厂商/模型/接口地址全配好，
# 用户不需要理解"厂商"这一层。结构：(厂商 id, 模型名, 卡片上的一行话)。
# Gradio 的单选组件不支持富文本徽章，所以用 emoji + 破折号模拟
# 「标签 + 模型名 · 厂商 —— 理由」的卡片样式。收录标准：
# 1) 有免费额度或本地可跑；2) 工具调用稳（本项目的核心依赖）；3) 有明确长处。
RECOMMENDED: tuple[tuple[str, str, str], ...] = (
    (
        "ollama",
        "qwen2.5:7b",
        "🆓 qwen2.5:7b · 本地 Ollama —— 免费，数据不出本机（8G 显存首选）",
    ),
    (
        "deepseek",
        "deepseek-flash",
        "💰 deepseek-flash · DeepSeek —— 大厂里最便宜，中文强、工具调用稳",
    ),
    (
        "zhipu",
        "glm-5.3-flash",
        "🎁 glm-5.3-flash · 智谱 GLM —— 有免费额度，原生会看图，读文档截图免配视觉模型",
    ),
    (
        "moonshot",
        "kimi-k3",
        "📄 kimi-k3 · Kimi —— 超长上下文，长合同长报告一次读完",
    ),
    (
        "dashscope",
        "qwen3.8-max",
        "⭐ qwen3.8-max · 通义千问 —— 综合能力最强档，复杂分析任务首选",
    ),
    (
        "siliconflow",
        "deepseek-ai/DeepSeek-V4-Flash",
        "🧩 deepseek-ai/DeepSeek-V4-Flash · 硅基流动 —— 一个 key 调多家开源模型",
    ),
)


def get(provider_id: str) -> Provider | None:
    return _BY_ID.get((provider_id or "").strip())


def all_providers() -> tuple[Provider, ...]:
    return PROVIDERS


def choices() -> list[tuple[str, str]]:
    """给下拉框用的 (显示名, 内部 id) 列表"""
    return [(p.name, p.id) for p in PROVIDERS]


def default_model(provider_id: str) -> str:
    p = get(provider_id)
    return p.models[0] if p and p.models else ""


def label(provider_id: str) -> str:
    p = get(provider_id)
    return p.name if p else (provider_id or "未选择")


def guess_id(base_url: str) -> str:
    """从接口地址反推厂商 id。

    给只用 .env 的老用户看的：他填了 BASE_URL，界面上要能显示出是哪一家。
    匹配不到就归到 custom。
    """
    url = (base_url or "").strip().rstrip("/").lower()
    if not url:
        return "custom"
    for p in PROVIDERS:
        if not p.base_url:
            continue
        base = p.base_url.rstrip("/").lower()
        if url == base or url.startswith(base + "/") or base.startswith(url + "/"):
            return p.id
    if "localhost" in url or "127.0.0.1" in url:
        return "ollama"
    return "custom"
