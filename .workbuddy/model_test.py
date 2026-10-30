"""
模型选择功能的单元测试

覆盖的不是"界面长什么样"，而是几个真会出事的地方：
1. 预设表本身是否完整（少了 key_url，用户根本找不到密钥在哪申请）
2. 保存的设置能否正确覆盖到 config（覆盖错了就是"选了 DeepSeek 还在用本地模型"）
3. 换厂商时密钥会不会串台（把 A 家的 key 发到 B 家，报错还指向 B 家）
4. 文件写坏 / 用户手滑删掉时程序能不能照常起来
5. 无密钥时的报错是不是人能照着修的中文

最后两条是真实调用：本地 Ollama 真发一次请求，确认改造没把原来的路弄坏。
"""

import json
import socket
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from myagent import providers, settings  # noqa: E402
from myagent.llm import UnavailableLLM, build_llm, ping  # noqa: E402

passed = 0
failed = 0


def check(name: str, cond: bool, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ✅ {name}")
    else:
        failed += 1
        print(f"  ❌ {name}  {detail}")


def tmp_cfg() -> SimpleNamespace:
    """一个干净的 config 替身，避免动到真实配置"""
    return SimpleNamespace(
        provider="ollama",
        backend="ollama",
        model="qwen2.5:7b",
        ollama_host="http://localhost:11434",
        api_key="",
        base_url="",
        vision_model="",
        num_ctx=8192,
        data_dir=Path(tempfile.gettempdir()) / "_model_test_cfg",
    )


print("=== 1. 预设表完整性 ===")
ids = [p.id for p in providers.all_providers()]
check("厂商 id 不重复", len(ids) == len(set(ids)), ids)
check("厂商不少于 10 家", len(ids) >= 10, len(ids))
for p in providers.all_providers():
    if p.id == "custom":
        continue
    check(f"{p.id} 有接口地址", p.base_url.startswith("http"), p.base_url)
    check(f"{p.id} 有默认模型", len(p.models) > 0, p.models)
    if p.need_key:
        check(f"{p.id} 写了密钥申请地址", bool(p.key_url), p.key_url)

print("=== 2. 下拉框与反查 ===")
check("下拉项与预设数量一致", len(providers.choices()) == len(ids))
_names = {pid: label for label, pid in providers.choices()}
check("显示名带中文说明", "DeepSeek" in _names.get("deepseek", ""), _names.get("deepseek"))
check(
    "从地址反查 DeepSeek",
    providers.guess_id("https://api.deepseek.com/v1") == "deepseek",
)
check("从地址反查本地", providers.guess_id("http://127.0.0.1:11434") == "ollama")
check("认不出的地址归到自定义", providers.guess_id("https://my-gateway.example.com/v1") == "custom")
check("空地址不崩", providers.guess_id("") == "custom")

print("=== 3. 保存前校验 ===")
ok, why = settings.validate("ollama", "qwen2.5:7b", "", "")
check("本地模型不需要密钥", ok, why)
ok, why = settings.validate("deepseek", "deepseek-v4-flash", "", "")
check("云端模型没密钥被拦", not ok and "密钥" in why, why)
ok, why = settings.validate("deepseek", "deepseek-v4-flash", "sk-test-123", "")
check("云端模型填了密钥放行", ok, why)
ok, why = settings.validate("custom", "my-model", "sk-x", "")
check("自定义没地址被拦", not ok and "接口地址" in why, why)
ok, why = settings.validate("custom", "my-model", "sk-x", "my-gateway:8000/v1")
check("地址没有协议被拦", not ok and "http" in why, why)
ok, why = settings.validate("deepseek", "", "sk-test", "")
check("模型名为空被拦", not ok and "模型名" in why, why)
ok, why = settings.validate("不存在的厂商", "x", "k", "https://a/v1")
check("未知厂商被拦", not ok and "未知" in why, why)

print("=== 4. 密钥掩码 ===")
check("空密钥不显示", settings.mask("") == "")
check("短密钥全遮", settings.mask("sk-123") == "******", settings.mask("sk-123"))
masked = settings.mask("sk-1234567890abcdef")
check("长密钥只露头尾", masked.startswith("sk-1") and masked.endswith("cdef"), masked)
check("掩码里没有完整密钥", "234567890ab" not in masked, masked)

print("=== 5. 设置文件往返 ===")
with tempfile.TemporaryDirectory() as d:
    data_dir = Path(d)
    p = settings.save(data_dir, "deepseek", "deepseek-v4-flash", "sk-test-123", "", "")
    check("文件落在数据目录", p == data_dir / "settings.json", p)
    check("文件真的写出来了", p.exists())
    raw = json.loads(p.read_text(encoding="utf-8"))
    check("内容完整", raw["provider"] == "deepseek" and raw["api_key"] == "sk-test-123", raw)
    check("记了保存时间", bool(raw.get("updated_at")))
    cfg = tmp_cfg()
    cfg.data_dir = data_dir
    check("设置能应用到 config", settings.apply_to(cfg) is True)
    check("后端切成 api", cfg.backend == "api", cfg.backend)
    check("厂商记对了", cfg.provider == "deepseek", cfg.provider)
    check("接口地址取预设", cfg.base_url == "https://api.deepseek.com", cfg.base_url)
    check("模型名正确", cfg.model == "deepseek-v4-flash", cfg.model)
    check("密钥带过去了", cfg.api_key == "sk-test-123")

    print("=== 6. 换厂商不串密钥 ===")
    settings.save(data_dir, "ollama", "qwen2.5:7b", "", "", "")
    cfg = tmp_cfg()
    cfg.data_dir = data_dir
    settings.apply_to(cfg)
    check("切回本地后后端是 ollama", cfg.backend == "ollama", cfg.backend)
    check("切回本地后密钥被清空", cfg.api_key == "", cfg.api_key)
    check("本地地址用 ollama_host", cfg.ollama_host.startswith("http"), cfg.ollama_host)

    settings.save(data_dir, "moonshot", "kimi-k3.1-pro", "sk-moon", "", "")
    cfg = tmp_cfg()
    cfg.data_dir = data_dir
    settings.apply_to(cfg)
    check("换厂商后地址跟着换", "moonshot" in cfg.base_url, cfg.base_url)
    check("只用了新厂商的密钥", cfg.api_key == "sk-moon", cfg.api_key)

    print("=== 7. 文件损坏与清除 ===")
    p = data_dir / "settings.json"
    p.write_text("{这不是 JSON", encoding="utf-8")
    check("写坏的文件当没配置", settings.load(data_dir) == {})
    cfg = tmp_cfg()
    cfg.data_dir = data_dir
    check("写坏时不应用设置", settings.apply_to(cfg) is False)
    check("清除有返回真实结果", settings.clear(data_dir) is True)
    check("清除后文件消失", not p.exists())
    check("重复清除不报错", settings.clear(data_dir) is False)

print("=== 8. 构建后端与报错话术 ===")
cfg = tmp_cfg()
cfg.backend = "api"
cfg.provider = "deepseek"
cfg.model = "deepseek-v4-flash"
try:
    build_llm(cfg)
    check("缺密钥时应当报错", False, "没有抛异常")
except ValueError as e:
    check("缺密钥报错提到密钥", "密钥" in str(e), str(e))
    check("报错说清是哪家", "DeepSeek" in str(e), str(e))

cfg.api_key = "sk-fake-for-name-test"
llm = build_llm(cfg)
check("云端后端带上厂商标识", llm.name == "deepseek/deepseek-v4-flash", llm.name)
check("云端后端声明支持工具", llm.supports_tools is True)

ok, why = ping(UnavailableLLM("模型还没配置好"))
check("未配置时给出可操作提示", not ok and "模型" in why, why)

print("=== 9. 本地 Ollama 真实连通（回归）===")
def _port_open(host="127.0.0.1", port=11434) -> bool:
    s = socket.socket()
    s.settimeout(0.6)
    try:
        s.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


if _port_open():
    local = build_llm(tmp_cfg())
    ok, why = ping(local)
    check("本地模型能正常回话", ok, why)
else:
    print("  ⚠️ Ollama 没开，跳过真实调用（不影响其它结论）")

key = (__import__("os").getenv("DEEPSEEK_API_KEY") or "").strip()
if key:
    cfg = tmp_cfg()
    cfg.data_dir = data_dir
    cfg.backend = "api"
    cfg.provider = "deepseek"
    cfg.model = "deepseek-v4-flash"
    cfg.api_key = key
    cfg.base_url = "https://api.deepseek.com"
    ok, why = ping(build_llm(cfg))
    check("真实 DeepSeek 密钥可用", ok, why)
else:
    print("  ⚠️ 没有 DEEPSEEK_API_KEY，跳过真实云端调用")

print(f"\n通过 {passed} 项，失败 {failed} 项")
sys.exit(0 if failed == 0 else 1)
