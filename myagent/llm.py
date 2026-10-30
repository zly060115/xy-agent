# 模型后端抽象层
# 本地 Ollama 和云端 API 共用同一套接口，切换只改 config.py

from abc import ABC, abstractmethod
from typing import Any


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
        if not tool_calls and tools:
            rescued = self._rescue_json_tool_call(content)
            if rescued:
                tool_calls = rescued
                content = None
        return {"content": content, "tool_calls": tool_calls}

    @staticmethod
    def _rescue_json_tool_call(content: str | None) -> list[dict] | None:
        """兜底：模型偶尔不按 tool_calls 字段返回，而是把工具调用
        以 JSON 文本写在 content 里（实测 qwen2.5 偶发）。
        识别出来转成正常的 tool_calls，否则这轮调用就白做了。
        """
        if not content:
            return None
        s = content.strip()
        if not (s.startswith("{") and s.endswith("}")):
            return None
        try:
            import json

            data = json.loads(s)
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        name = data.get("name") or (data.get("function") or {}).get("name")
        args = data.get("arguments") or (data.get("function") or {}).get("arguments")
        if not name or not isinstance(name, str) or "." in name:
            return None
        if not isinstance(args, dict):
            return None
        return [{"id": "call_json", "name": name, "arguments": args}]

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
    任意 OpenAI 兼容接口：OpenAI / DeepSeek / 通义 / 智谱 / Moonshot 等
    以后要接 API，只要在 config.py 里切到这一档并填 base_url + key + model
    """

    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        vision_model: str = "",
    ):
        from openai import OpenAI

        self._cli = OpenAI(api_key=api_key, base_url=base_url)
        self._model = model
        self._vision_model = (vision_model or "").strip()

    @property
    def name(self) -> str:
        return f"api/{self._model}"

    @property
    def vision_name(self) -> str:
        return f"api/{self._vision_model}" if self._vision_model else ""

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

    def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        options: dict | None = None,
    ) -> dict:
        kwargs: dict[str, Any] = {"model": self._model, "messages": messages}
        if tools:
            kwargs["tools"] = [{"type": "function", "function": t} for t in tools]
        if options:
            # OpenAI 兼容协议的参数名不同，做一次映射，不认识的丢掉
            if "temperature" in options:
                kwargs["temperature"] = options["temperature"]
            if "num_predict" in options:
                kwargs["max_tokens"] = options["num_predict"]

        resp = self._cli.chat.completions.create(**kwargs)
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
        return {"content": msg.content, "tool_calls": tool_calls}


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
        if not cfg.api_key:
            raise ValueError("使用 api 后端必须配置 api_key")
        return OpenAICompatLLM(
            model=cfg.model,
            api_key=cfg.api_key,
            base_url=cfg.base_url,
            vision_model=getattr(cfg, "vision_model", ""),
        )
    raise ValueError(f"未知后端: {cfg.backend}")
