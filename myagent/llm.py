# 模型后端抽象层
# 本地 Ollama 和云端 API 共用同一套接口，切换只改 config.py

from abc import ABC, abstractmethod
import re
from typing import Any

# 模型把工具调用写成文本时的标记
TOOL_TAG_RE = re.compile(r"</?tool_call[^>]*>", re.I)


def _iter_json_objects(text: str):
    """按大括号配对，逐个产出 (JSON 文本, 起点, 终点)

    比正则可靠：arguments 里常有嵌套对象，正则的贪婪/非贪婪都会切错。
    """
    i = 0
    while True:
        start = text.find("{", i)
        if start < 0:
            return
        depth = 0
        for j in range(start, len(text)):
            if text[j] == "{":
                depth += 1
            elif text[j] == "}":
                depth -= 1
                if depth == 0:
                    yield text[start : j + 1], start, j + 1
                    i = j + 1
                    break
        else:
            # 没闭合，往后找下一个 {
            i = start + 1


class BaseLLM(ABC):
    """所有模型后端都必须实现这三个成员"""

    @abstractmethod
    def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        options: dict | None = None,
    ) -> dict:
        """
        返回统一格式：
        {
            "content": str | None,        # 文本回复
            "tool_calls": [               # 要调用的工具，可能为空
                {"id": str, "name": str, "arguments": dict}
            ]
        }

        options 是可选采样参数（temperature / num_predict 等），
        后端不认识就忽略，保证调用方不用关心底层差异。
        """
        raise NotImplementedError

    @property
    @abstractmethod
    def supports_tools(self) -> bool:
        """该模型是否支持函数调用"""
        raise NotImplementedError

    def vision_chat(
        self,
        image_b64: str,
        prompt: str,
        mime: str = "image/png",
        options: dict | None = None,
    ) -> str | None:
        """把一张图交给多模态模型解读，返回一句中文描述。

        不是所有后端都能看图：不支持时返回 None，
        调用方据此降级成"只用 OCR 抽出来的文字"。
        """
        return None

    @property
    def vision_name(self) -> str:
        return ""

    @property
    @abstractmethod
    def name(self) -> str:
        raise NotImplementedError


class OllamaLLM(BaseLLM):
    """本地 Ollama 后端"""

    def __init__(
        self,
        model: str = "qwen2.5:7b",
        host: str = "http://localhost:11434",
        num_ctx: int = 8192,
        vision_model: str = "",
    ):
        import ollama

        self._client = ollama
        self._model = model
        self._host = host
        self._num_ctx = num_ctx
        self._vision_model = (vision_model or "").strip()
        self._cli = ollama.Client(host=host)
        # 视觉调用单独一个带超时的 client：模型正在下载或显存不足时，
        # 不加超时会把整个文档分析挂死
        self._vision_cli = (
            ollama.Client(host=host, timeout=60) if self._vision_model else None
        )

    @property
    def name(self) -> str:
        return f"ollama/{self._model}"

    @property
    def vision_name(self) -> str:
        return f"ollama/{self._vision_model}" if self._vision_model else ""

    def vision_chat(
        self,
        image_b64: str,
        prompt: str,
        mime: str = "image/png",
        options: dict | None = None,
    ) -> str | None:
        """Ollama 的多模态接口：messages 里带 images（base64 字符串列表）"""
        if not self._vision_model:
            return None
        opts = {
            "temperature": 0.1,
            "num_predict": 400,
            "num_ctx": 4096,
        }
        if options:
            opts.update(options)
        try:
            resp = (self._vision_cli or self._cli).chat(
                model=self._vision_model,
                messages=[
                    {
                        "role": "user",
                        "content": prompt,
                        # 注意：Ollama 接受纯 base64，不带 data:image/... 前缀
                        "images": [image_b64],
                    }
                ],
                options=opts,
            )
        except Exception:
            # 模型没下载 / 显存不足 / 服务重启，都当作"看不了图"降级处理
            return None
        msg = resp.get("message", {})
        return (msg.get("content") or "").strip() or None

    @property
    def supports_tools(self) -> bool:
        # qwen2.5 / llama3.1 / mistral-nemo 等支持工具调用
        return True

    def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        options: dict | None = None,
    ) -> dict:
        opts = {
            # 不显式指定 num_ctx 时 Ollama 默认只有 4k，长文档会被静默截断
            "num_ctx": self._num_ctx,
            # 低压采样：工具调用和抽取任务要稳定，不要发挥
            "temperature": 0.2,
            # 默认输出上限偏小会截断结构化结果
            "num_predict": 2048,
        }
        if options:
            opts.update(options)
        kwargs = {
            "model": self._model,
            "messages": [self._to_ollama_msg(m) for m in messages],
            "options": opts,
        }
        if tools:
            # Ollama 需要 {type: "function", function: {...}} 包装，
            # 直接传扁平结构会导致模型拿不到工具名
            kwargs["tools"] = [{"type": "function", "function": t} for t in tools]

        resp = self._cli.chat(**kwargs)
        msg = resp.get("message", {})

        # ollama 库可能返回 pydantic 对象，统一转成 dict
        tool_calls = []
        raw_calls = msg.get("tool_calls") or []
        for i, tc in enumerate(raw_calls):
            fn = tc.get("function", {}) if isinstance(tc, dict) else getattr(tc, "function", None)
            if fn is None:
                continue
            if isinstance(fn, dict):
                name = fn.get("name", "")
                args = fn.get("arguments") or {}
            else:
                name = getattr(fn, "name", "")
                args = getattr(fn, "arguments", {}) or {}
            if not isinstance(args, dict):
                try:
                    import json

                    args = json.loads(args)
                except Exception:
                    args = {}
            tool_calls.append({"id": f"call_{i}", "name": name, "arguments": args})

        content = msg.get("content") or None
        if content:
            # 模型偶尔把工具调用写成 <tool_call>{...}</tool_call> 的文本形式，
            # 解析器只认走一半，残留的标记和 JSON 会直接印给用户。
            # 这里先把标记剥掉，下面再从正文里把这坨调用捞出来。
            content = TOOL_TAG_RE.sub("", content).strip() or None
        if not tool_calls and tools and content:
            rescued, leftover = self._rescue_json_tool_call(
                content, allow_embedded=bool(TOOL_TAG_RE.search(msg.get("content") or ""))
            )
            if rescued:
                tool_calls = rescued
                content = leftover or None
        return {"content": content, "tool_calls": tool_calls}

    @staticmethod
    def _rescue_json_tool_call(
        content: str | None, allow_embedded: bool = False
    ) -> tuple[list[dict] | None, str]:
        """兜底：模型偶尔不按 tool_calls 字段返回，而是把工具调用
        以 JSON 文本写在 content 里（实测 qwen2.5 偶发）。
        识别出来转成正常的 tool_calls，否则这轮调用就白做了。

        返回 (工具调用列表 或 None, 剥掉调用后的剩余文本)。
        剩余文本要保留：模型有时前半句是说明、后半句才是调用，
        整段丢掉会把有用的话一起吞了。

        allow_embedded 只在原文本确实带 <tool_call> 标记时打开。
        否则一段正文里恰好出现 {"name":..,"arguments":{..}} 就会被误当调用，
        用户让我们解释一个 JSON 时尤其容易踩。
        """
        if not content:
            return None, content or ""
        import json

        text = content.strip()

        # 纯 JSON 的老路径：整段就是一个调用
        s = text
        if s.startswith("{") and s.endswith("}"):
            try:
                data = json.loads(s)
            except Exception:
                data = None
            call = OllamaLLM._as_tool_call(data)
            if call:
                return [call], ""

        # 带标记时的老路径：从正文里挖出第一个能解析的 JSON 对象
        if allow_embedded:
            for obj, start, end in _iter_json_objects(text):
                try:
                    data = json.loads(obj)
                except Exception:
                    continue
                call = OllamaLLM._as_tool_call(data)
                if call:
                    leftover = (text[:start] + text[end:]).strip()
                    return [call], leftover

        return None, content

    @staticmethod
    def _as_tool_call(data) -> dict | None:
        """把解析出的 JSON 认成工具调用；不像就返回 None"""
        if not isinstance(data, dict):
            return None
        fn = data.get("function") or {}
        name = data.get("name") or (fn.get("name") if isinstance(fn, dict) else None)
        args = data.get("arguments")
        if args is None and isinstance(fn, dict):
            args = fn.get("arguments")
        if not name or not isinstance(name, str) or "." in name:
            return None
        if not isinstance(args, dict):
            return None
        return {"id": "call_json", "name": name, "arguments": args}

    @staticmethod
    def _to_ollama_msg(m: dict) -> dict:
        """
        把内部统一格式转成 Ollama 要求的格式。
        关键点：Ollama 的 tool_calls 里 function 是必填的，
        而内部格式用的是扁平的 name/arguments，必须转换。
        """
        role = m.get("role")
        content = m.get("content") or ""

        if role == "tool":
            # Ollama 的工具结果用 role=tool + tool_name 表达
            return {"role": "tool", "content": content}

        if role == "assistant" and m.get("tool_calls"):
            return {
                "role": "assistant",
                "content": content,
                "tool_calls": [
                    {
                        "function": {
                            "name": tc.get("name", ""),
                            "arguments": tc.get("arguments") or {},
                        }
                    }
                    for tc in m["tool_calls"]
                ],
            }

        return {"role": role, "content": content}


class OpenAICompatLLM(BaseLLM):
    """
    任意 OpenAI 兼容接口：OpenAI / DeepSeek / 通义 / 智谱 / Kimi 等
    厂商的接口地址在 myagent/providers.py 里预设，界面上选一家即可
    """

    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        vision_model: str = "",
        provider_name: str = "api",
    ):
        from openai import OpenAI

        # 超时给足：长文档单次请求要几十秒，默认 10 分钟太长，
        # 但 60 秒会把慢模型误判成失败，折中 180 秒并只重试一次
        self._cli = OpenAI(api_key=api_key, base_url=base_url, timeout=180, max_retries=1)
        self._model = model
        self._vision_model = (vision_model or "").strip()
        self._provider = (provider_name or "api").strip()

    @property
    def name(self) -> str:
        return f"{self._provider}/{self._model}"

    @property
    def vision_name(self) -> str:
        return f"{self._provider}/{self._vision_model}" if self._vision_model else ""

    def vision_chat(
        self,
        image_b64: str,
        prompt: str,
        mime: str = "image/png",
        options: dict | None = None,
    ) -> str | None:
        """OpenAI 兼容的多模态格式：content 里放 text + image_url 两种块"""
        if not self._vision_model:
            return None
        try:
            resp = self._cli.chat.completions.create(
                model=self._vision_model,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:{mime};base64,{image_b64}"},
                            },
                        ],
                    }
                ],
                temperature=0.1,
                max_tokens=400,
            )
        except Exception:
            return None
        content = (resp.choices[0].message.content or "").strip()
        return content or None

    @property
    def supports_tools(self) -> bool:
        return True

    @staticmethod
    def _to_openai_msg(m: dict) -> dict:
        """
        把内部统一格式转成 OpenAI 协议要求的格式。

        内部历史里 assistant 的工具调用是扁平的 {id, name, arguments}，
        tool 消息还带 name 字段——本地 Ollama 原生接口能容忍，
        但云端 OpenAI 兼容服务会严格校验 schema，缺 function 字段
        直接 422「缺失字段」拒收，表现为第一轮能答、第二轮必挂。
        必须在发出去之前转成标准结构。
        """
        import json as _json

        role = m.get("role")
        content = m.get("content")

        if role == "assistant" and m.get("tool_calls"):
            tcs = []
            for i, tc in enumerate(m["tool_calls"]):
                args = tc.get("arguments")
                # 协议要求 arguments 是字符串，不是对象
                if isinstance(args, str):
                    args_str = args
                else:
                    args_str = _json.dumps(args or {}, ensure_ascii=False)
                tcs.append(
                    {
                        "id": tc.get("id") or f"call_{i}",
                        "type": "function",
                        "function": {"name": tc.get("name", ""), "arguments": args_str},
                    }
                )
            return {"role": "assistant", "content": content, "tool_calls": tcs}

        if role == "tool":
            # tool 消息只保留 tool_call_id；name 字段部分服务不认，去掉更稳
            return {
                "role": "tool",
                "content": content if content is not None else "",
                "tool_call_id": m.get("tool_call_id") or "call_0",
            }

        # 其余角色：content 缺失时补空串，避免 null 被严格服务拒收
        return {"role": role, "content": content if content is not None else ""}

    def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        options: dict | None = None,
    ) -> dict:
        messages = [self._to_openai_msg(m) for m in messages]
        kwargs: dict[str, Any] = {"model": self._model, "messages": messages}
        if tools:
            kwargs["tools"] = [{"type": "function", "function": t} for t in tools]
        if options:
            # OpenAI 兼容协议的参数名不同，做一次映射，不认识的丢掉
            if "temperature" in options:
                kwargs["temperature"] = options["temperature"]
            if "num_predict" in options:
                kwargs["max_tokens"] = options["num_predict"]

        try:
            resp = self._cli.chat.completions.create(**kwargs)
        except Exception as e:
            # 部分厂商的推理模型不接受 temperature（如把采样参数固定死），
            # 报错里点名了才退一步去掉重试，不能吞掉真正的鉴权/余额错误
            msg = str(e).lower()
            if "temperature" in msg and "temperature" in kwargs:
                kwargs.pop("temperature", None)
                resp = self._cli.chat.completions.create(**kwargs)
            else:
                raise
        msg = resp.choices[0].message
        tool_calls = []
        for tc in msg.tool_calls or []:
            import json

            tool_calls.append(
                {
                    "id": tc.id,
                    "name": tc.function.name,
                    "arguments": json.loads(tc.function.arguments or "{}"),
                }
            )
        content = msg.content
        # 与本地后端同样的毛病：模型把调用写成文本，标记会直接印给用户
        if content and TOOL_TAG_RE.search(content):
            content = TOOL_TAG_RE.sub("", content).strip() or None
        return {"content": content, "tool_calls": tool_calls}


class UnavailableLLM(BaseLLM):
    """配置不可用时的占位后端。

    典型场景：用户在界面上换了一家云端模型但还没填密钥就重启了服务。
    直接抛异常会让网页起不来、连界面都打不开；
    这里让程序正常启动，只在实际对话时给出可操作的提示。
    """

    def __init__(self, reason: str = ""):
        self._reason = reason or "模型还没配置好"

    @property
    def name(self) -> str:
        return "unavailable"

    @property
    def supports_tools(self) -> bool:
        return False

    def chat(self, messages, tools=None, options=None) -> dict:
        raise RuntimeError(
            f"{self._reason}。打开网页的「模型」标签页，选一家厂商、填上密钥，"
            "点「保存并启用」即可。"
        )


def build_llm(cfg) -> BaseLLM:
    """按配置造一个模型后端"""
    if cfg.backend == "ollama":
        return OllamaLLM(
            model=cfg.model,
            host=cfg.ollama_host,
            num_ctx=getattr(cfg, "num_ctx", 8192),
            vision_model=getattr(cfg, "vision_model", ""),
        )
    if cfg.backend == "api":
        from . import providers

        pid = getattr(cfg, "provider", "") or ""
        p = providers.get(pid)
        base_url = (getattr(cfg, "base_url", "") or "").strip() or (p.base_url if p else "")
        key = (getattr(cfg, "api_key", "") or "").strip()
        if not key:
            where = f"，可以在 {p.key_url} 申请" if (p and p.key_url) else ""
            raise ValueError(f"{providers.label(pid)} 需要 API 密钥{where}")
        if not base_url:
            raise ValueError(f"{providers.label(pid)} 缺少接口地址（base_url）")
        return OpenAICompatLLM(
            model=cfg.model,
            api_key=key,
            base_url=base_url,
            vision_model=getattr(cfg, "vision_model", ""),
            provider_name=pid or "api",
        )
    raise ValueError(f"未知后端: {cfg.backend}")


def ping(llm: BaseLLM, timeout_note: str = "") -> tuple[bool, str]:
    """发一次最小请求，确认模型真的能用。

    给界面上的「测试连接」用。只判断"能不能拿到回复"，
    不评判回答质量——那是评测脚本的事。
    """
    import time

    started = time.time()
    try:
        out = llm.chat(
            [
                {"role": "system", "content": "只回答一个字，不要解释。"},
                {"role": "user", "content": "在吗"},
            ],
            options={"temperature": 0, "num_predict": 16},
        )
    except Exception as e:  # noqa: BLE001 - 任何异常都要变成人能看懂的话
        return False, _explain(e)
    cost = time.time() - started
    text = (out.get("content") or "").strip()
    if not text:
        return False, f"连上了，但模型没有返回文字（耗时 {cost:.1f}s）。换个模型名再试。"
    return True, f"连通正常（{cost:.1f}s）：{text[:30]}"


def _explain(e: Exception) -> str:
    """把常见报错翻成能照着修的中文"""
    text = str(e)
    low = text.lower()
    if "401" in text or "unauthorized" in low or "invalid api key" in low or "invalid_api_key" in low:
        return "密钥被拒绝（401）。检查是否复制完整、是否和所选厂商对得上。"
    if "403" in text or "permission" in low:
        return "密钥没有该模型的权限（403）。确认账号已开通这个模型。"
    if "404" in text or "model_not_found" in low or "does not exist" in low:
        return f"接口地址或模型名不对（404）。确认接口地址写到 /v1，模型名照厂商文档填。原始信息：{text[:120]}"
    if "429" in text or "rate limit" in low:
        return "被限流或余额不足（429）。稍后重试，或检查账户余额。"
    if "timeout" in low or "timed out" in low:
        return "连接超时。可能是网络不通，或该厂商在境内需要代理。"
    if "connection" in low or "resolve" in low:
        return f"连不上这个地址，检查接口地址是否写错、网络是否可达。原始信息：{text[:120]}"
    return f"调用失败：{text[:200]}"

