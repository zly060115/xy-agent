"""
用户级设置：模型选择 + 密钥

为什么单独存一个文件，而不是写回 .env：
- .env 跟着代码走，会被 git 追踪、被误提交，密钥一旦推到 GitHub 就泄露了
- 这个文件放在 DATA_DIR（默认 ~/.xiaoman），属于"用户数据"，
  和代码彻底分离，换电脑/备份跟着数据走

明文存储，这是本项目有意的取舍：本地单用户工具，加密要用户再记一个密码，
反而经常导致密钥找不回来。文件会尝试收紧为仅当前用户可读；
界面上也只显示掩码。若要更严，请用系统级密钥管理，本项目不假装自己更安全。
"""

import json
import os
import stat
import time
from pathlib import Path

from . import providers

FILENAME = "settings.json"


def path_for(data_dir) -> Path:
    return Path(data_dir) / FILENAME


def load(data_dir) -> dict:
    p = path_for(data_dir)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        # 文件被写坏时不要连带整个程序起不来，当作没配置，界面可重新保存
        return {}
    return data if isinstance(data, dict) else {}


def save(data_dir, provider_id: str, model: str, api_key: str, base_url: str, vision_model: str = "") -> Path:
    """写入设置，返回落盘路径。原子写：先写临时文件再替换，避免断电留半个 JSON。"""
    p = path_for(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "provider": provider_id,
        "model": model.strip(),
        "api_key": (api_key or "").strip(),
        "base_url": (base_url or "").strip(),
        "vision_model": (vision_model or "").strip(),
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)
    _tighten(p)
    return p


def clear(data_dir) -> bool:
    """删掉设置，回到 .env / 默认值。返回是否真的删了东西。"""
    p = path_for(data_dir)
    if p.exists():
        try:
            p.unlink()
            return True
        except OSError:
            return False
    return False


def _tighten(p: Path) -> None:
    """尽量把文件权限收到仅当前用户。Windows 上 chmod 基本无效，失败就跳过。"""
    try:
        os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)
    except Exception:
        pass


def mask(key: str) -> str:
    """密钥掩码。短密钥少露几位，避免把短 key 整个露出来。"""
    k = (key or "").strip()
    if not k:
        return ""
    if len(k) <= 8:
        return "*" * len(k)
    return k[:4] + "*" * max(4, len(k) - 8) + k[-4:]


def validate(provider_id: str, model: str, api_key: str, base_url: str) -> tuple[bool, str]:
    """保存前校验，返回 (是否通过, 中文原因)。只做能确定性判断的事。"""
    p = providers.get(provider_id)
    if p is None:
        return False, f"未知的模型厂商：{provider_id}"
    if not (model or "").strip():
        return False, "模型名不能为空"
    if p.kind == "local":
        return True, ""
    if not (api_key or "").strip():
        where = f"，可以到 {p.key_url} 申请" if p.key_url else ""
        return False, f"{p.name} 需要 API 密钥{where}"
    url = (base_url or "").strip() or p.base_url
    if not url:
        return False, "自定义接口必须填接口地址（形如 https://example.com/v1）"
    if not url.lower().startswith(("http://", "https://")):
        return False, "接口地址要以 http:// 或 https:// 开头"
    return True, ""


def apply_to(cfg) -> bool:
    """把用户设置覆盖到 config 上。返回是否应用了设置。

    优先级：界面保存的 settings.json > .env > 代码默认值。
    界面是用户的显式动作，理当压过可能过期的 .env。
    """
    data = load(cfg.data_dir)
    pid = (data.get("provider") or "").strip()
    if not pid:
        return False
    p = providers.get(pid)
    cfg.provider = pid
    cfg.backend = "ollama" if (p and p.kind == "local") else "api"
    cfg.model = (data.get("model") or "").strip() or (p.models[0] if p and p.models else cfg.model)
    if p and p.kind == "local":
        cfg.ollama_host = (data.get("base_url") or "").strip() or cfg.ollama_host or p.base_url
        # 本地模式不用密钥，避免把云端密钥留在内存里
        cfg.api_key = ""
    else:
        cfg.base_url = (data.get("base_url") or "").strip() or (p.base_url if p else cfg.base_url)
        # 只认界面里存的那把密钥：换厂商时不能把上一家的密钥带过去，
        # 否则请求会带上错误凭据，报错信息还指向错的厂商
        cfg.api_key = (data.get("api_key") or "").strip()
    cfg.vision_model = (data.get("vision_model") or "").strip()
    return True


def describe(cfg) -> str:
    """当前生效的模型配置，给界面和命令行显示用（密钥走掩码）"""
    pid = getattr(cfg, "provider", "") or providers.guess_id(getattr(cfg, "base_url", ""))
    p = providers.get(pid)
    if p and p.kind == "local":
        return (
            f"厂商：{p.name}\n"
            f"模型：{cfg.model}\n"
            f"地址：{cfg.ollama_host}\n"
            f"密钥：不需要"
        )
    key = getattr(cfg, "api_key", "")
    return (
        f"厂商：{providers.label(pid)}\n"
        f"模型：{cfg.model}\n"
        f"地址：{getattr(cfg, 'base_url', '')}\n"
        f"密钥：{mask(key) or '未填写'}\n"
        f"视觉模型：{getattr(cfg, 'vision_model', '') or '未启用（只用 OCR）'}"
    )
