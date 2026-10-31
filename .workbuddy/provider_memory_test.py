"""
密钥记忆测试：换模型后不能要求用户重新粘一次密钥

覆盖用户实际遇到的那条路：
  填 DeepSeek 密钥 → 保存 → 切到本地 Ollama 保存 → 再切回 DeepSeek
期望：密钥自动带回，输入框留空也能用；同时不能把 A 家的密钥带进 B 家。

全程在临时数据目录里跑，不碰真实的 ~/.xiaoman。
"""

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

# 必须在 import web 之前设好：config 在导入时就会读 DATA_DIR
_TMP = tempfile.mkdtemp(prefix="xiaoman_memo_")
os.environ["DATA_DIR"] = _TMP
os.environ["PROVIDER"] = "ollama"

import web  # noqa: E402
from myagent import settings  # noqa: E402

PASS = 0
FAIL = 0


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def read_file(d):
    return json.loads(settings.path_for(d).read_text(encoding="utf-8"))


print(f"临时数据目录: {_TMP}")

# ---------- 1. 记 DeepSeek 密钥 ----------
print("\n[1] 保存 DeepSeek 密钥")
d = _TMP
settings.save(d, "deepseek", "deepseek-v4-flash", "sk-deep-1234567890", "https://api.deepseek.com", "")
data = read_file(d)
check("顶层 provider = deepseek", data["provider"] == "deepseek")
check("顶层 api_key 落盘", data["api_key"] == "sk-deep-1234567890")
check("saved.deepseek 有记录", (data.get("saved") or {}).get("deepseek", {}).get("api_key") == "sk-deep-1234567890")
check("记下了模型名", data["saved"]["deepseek"]["model"] == "deepseek-v4-flash")
check("remembered() 能取回", settings.remembered(d, "deepseek").get("api_key") == "sk-deep-1234567890")
check("remembered_providers 含 deepseek", "deepseek" in settings.remembered_providers(d))

# ---------- 2. 切到本地 Ollama 保存（用户遇到的那一步） ----------
print("\n[2] 切到本地 Ollama 保存，DeepSeek 密钥不能丢")
settings.save(d, "ollama", "qwen2.5:7b", "sk-deep-1234567890", "http://localhost:11434", "")
data = read_file(d)
check("顶层 provider = ollama", data["provider"] == "ollama")
check("顶层 api_key 被清空（本地不需要，不能串台）", data["api_key"] == "", data.get("api_key"))
check("DeepSeek 密钥仍在 saved 里", data["saved"]["deepseek"]["api_key"] == "sk-deep-1234567890")
check("saved.ollama 不含密钥", not (data["saved"]["ollama"].get("api_key") or ""))
check("remembered(ollama) 无密钥", not settings.remembered(d, "ollama").get("api_key"))

# ---------- 3. 切回 DeepSeek，留空也要能用 ----------
print("\n[3] 切回 DeepSeek：输入框留空应自动沿用")
got = web._effective_key("deepseek", "")
check("_effective_key 自动带回记住的密钥", got == "sk-deep-1234567890", got)
check("手填的新密钥优先", web._effective_key("deepseek", "sk-new-999") == "sk-new-999")

# ---------- 4. 不串台 ----------
print("\n[4] 别家厂商不能拿到 DeepSeek 的密钥")
check("dashscope 留空取不到 DeepSeek 的 key", web._effective_key("dashscope", "") == "")
check("ollama 留空取不到密钥", web._effective_key("ollama", "") == "")

# ---------- 5. 点卡片自动回填 ----------
print("\n[5] 点卡片时密钥框自动带回")
out = web.pick_model_fn("deepseek::deepseek-v4-flash")
check("pick_model_fn 返回 7 项（多了密钥框）", len(out) == 7, len(out))
key_upd = out[6]
check("密钥框带回 DeepSeek 的 key", getattr(key_upd, "get", lambda *_: None)("value") == "sk-deep-1234567890", key_upd)
check("提示里说明已记住", "已记住" in (out[5] or ""), out[5])

out_ol = web.pick_model_fn("ollama::qwen2.5:7b")
check("点本地卡片时密钥框清空", (getattr(out_ol[6], "get", lambda *_: None)("value") or "") == "")
check("本地卡片不提示密钥", "已记住" not in (out_ol[5] or ""))

out_sw = web.on_provider_change("deepseek")
check("高级设置切厂商也带回密钥", getattr(out_sw[5], "get", lambda *_: None)("value") == "sk-deep-1234567890")
out_sw2 = web.on_provider_change("moonshot")
check("切到没记过的厂商密钥框清空", (getattr(out_sw2[5], "get", lambda *_: None)("value") or "") == "")

# ---------- 6. 两家各存各的 ----------
print("\n[6] 两家厂商并存")
settings.save(d, "moonshot", "kimi-k2", "sk-moon-abcdefg", "https://api.moonshot.cn/v1", "")
data = read_file(d)
check("deepseek 仍保留", data["saved"]["deepseek"]["api_key"] == "sk-deep-1234567890")
check("moonshot 已记录", data["saved"]["moonshot"]["api_key"] == "sk-moon-abcdefg")
check("remembered_providers 两家都在", set(settings.remembered_providers(d)) == {"deepseek", "moonshot"})
check("各自取各自", settings.remembered(d, "deepseek")["api_key"] != settings.remembered(d, "moonshot")["api_key"])

# ---------- 7. 留空保存不能抹掉已记的密钥 ----------
print("\n[7] 留空保存沿用旧密钥")
settings.save(d, "deepseek", "deepseek-chat", "", "https://api.deepseek.com", "")
check("换了模型名，密钥还在", read_file(d)["saved"]["deepseek"]["api_key"] == "sk-deep-1234567890")
check("模型名已更新", read_file(d)["saved"]["deepseek"]["model"] == "deepseek-chat")

# ---------- 8. 老格式文件迁移 ----------
print("\n[8] 老版本 settings.json（没有 saved 字段）")
d2 = tempfile.mkdtemp(prefix="xiaoman_old_")
settings.path_for(d2).write_text(
    json.dumps(
        {
            "provider": "deepseek",
            "model": "deepseek-chat",
            "api_key": "sk-legacy-0001",
            "base_url": "https://api.deepseek.com",
            "vision_model": "",
        }
    ),
    encoding="utf-8",
)
check("老文件能读出密钥", settings.remembered(d2, "deepseek")["api_key"] == "sk-legacy-0001")
check("老文件别家仍为空", settings.remembered(d2, "dashscope") == {})
settings.save(d2, "ollama", "qwen2.5:7b", "", "http://localhost:11434", "")
check("迁移后 deepseek 密钥没丢", settings.remembered(d2, "deepseek")["api_key"] == "sk-legacy-0001")

# ---------- 9. 写坏文件不影响启动 ----------
print("\n[9] 文件被写坏时容错")
d3 = tempfile.mkdtemp(prefix="xiaoman_bad_")
settings.path_for(d3).write_text("{ 坏掉的 json", encoding="utf-8")
check("load 返回空而不是抛异常", settings.load(d3) == {})
check("remembered 返回空", settings.remembered(d3, "deepseek") == {})

# ---------- 10. 清除配置 ----------
print("\n[10] 清除配置")
check("clear 返回 True", settings.clear(d) is True)
check("文件已删", not settings.path_for(d).exists())
check("remembered 已清空", settings.remembered_providers(d) == [])

print(f"\n结果：{PASS} 项通过，{FAIL} 项失败")
sys.exit(1 if FAIL else 0)
