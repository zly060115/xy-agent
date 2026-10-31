"""
网页端冒烟测试：确认「模型」标签页真的能构起来、按钮逻辑真的接上了

只测能确定性判断的事：
- Gradio 界面能构建（构建失败等于用户打开就是一片报错）
- 切厂商时模型名 / 接口地址 / 视觉模型是否一起换
- 保存后内存里的模型、Agent、读图组件是否同时切过去（漏一个就是"看起来切了实际没切"）
- 清除配置能否回到 .env

全程在临时数据目录里跑，不碰真实的 ~/.xiaoman。
"""

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

# 必须在 import web 之前设好：config 在导入时就会读 DATA_DIR
_TMP = tempfile.mkdtemp(prefix="xiaoman_ui_")
os.environ["DATA_DIR"] = _TMP

import web  # noqa: E402

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


print("=== 1. 界面构建 ===")
check("Blocks 构建成功", web.demo is not None)
check("启动时用的是本地模型", web.LLM.name.startswith("ollama/"), web.LLM.name)
check("没有残留的启动错误", web.STARTUP_ERR == "", web.STARTUP_ERR)

print("=== 2. 切厂商联动 ===")
up = web.on_provider_change("deepseek")
check("返回 6 个组件更新（含清除快速选择、带回密钥）", len(up) == 6, len(up))
check("接口地址换成 DeepSeek", "api.deepseek.com" in (up[1].get("value") or ""), up[1])
check("模型列表非空", len(up[0].get("choices") or []) > 0)
check("说明里带密钥申请地址", "platform.deepseek.com" in (up[2].get("value") or ""))
check("手动切厂商会清掉点亮的卡片", up[4].get("value") is None, up[4])

up = web.on_provider_change("zhipu")
check("智谱自带视觉模型", bool(up[3].get("value")), up[3].get("value"))

up = web.on_provider_change("ollama")
check("切回本地时视觉模型是 qwen2.5vl", "qwen2.5vl" in (up[3].get("value") or ""), up[3])

print("=== 2b. 快速选择卡片 ===")
up = web.pick_model_fn("zhipu::glm-5.3-flash")
check("卡片点选切到对应厂商", up[0] == "zhipu", up[0])
check("卡片点选模型跟着变", up[1].get("value") == "glm-5.3-flash", up[1])
check("接口地址自动填好", "open.bigmodel.cn" in (up[2] or ""), up[2])
check("视觉模型自动填好", up[4] == "glm-5.3-flash", up[4])
check("提示带密钥申请地址", "open.bigmodel.cn" in (up[5] or ""), up[5])

up = web.pick_model_fn("ollama::qwen2.5:7b")
check("本地卡片提示免密钥", "不需要密钥" in (up[5] or ""), up[5])
check("空输入不崩", len(web.pick_model_fn(None)) == 7)
check("未知卡片返回空提示", web.pick_model_fn("nope::x")[5] == "")
check(
    "卡片数量与精选推荐一致",
    len(web.providers.RECOMMENDED) == 6,
    len(web.providers.RECOMMENDED),
)

print("=== 3. 保存前的拦截 ===")
msg = web.test_model_fn("deepseek", "deepseek-v4-flash", "", "", "")
check("没密钥时给出中文原因", msg.startswith("❌") and "密钥" in msg, msg)
msg = web.test_model_fn("custom", "my-model", "sk-x", "", "")
check("自定义没地址被拦", msg.startswith("❌") and "接口地址" in msg, msg)

print("=== 4. 保存并热切换 ===")
msg = web.apply_model_fn("deepseek", "deepseek-v4-flash", "sk-ui-test-4321", "", "")
check("提示切换成功", msg.startswith("✅"), msg.splitlines()[0] if msg else "")
check("全局模型已换", web.LLM.name == "deepseek/deepseek-v4-flash", web.LLM.name)
check("Agent 用的是新模型", web.AGENT.llm.name == web.LLM.name, web.AGENT.llm.name)
check("读图组件也换了", web.READER.vision.name == web.LLM.name, web.READER.vision.name)
check(
    "设置文件落在临时数据目录",
    (Path(_TMP) / "settings.json").exists(),
    _TMP,
)
check("状态文字里密钥是掩码", "sk-u" in web.model_status_fn() and "4321" in web.model_status_fn())
check("状态文字不含完整密钥", "sk-ui-test-4321" not in web.model_status_fn())

print("=== 5. 清除配置 ===")
msg = web.clear_model_fn()
check("提示清除成功", msg.startswith("✅"), msg.splitlines()[0] if msg else "")
check("回到 .env 的本地模型", web.LLM.name.startswith("ollama/"), web.LLM.name)
check("设置文件已删除", not (Path(_TMP) / "settings.json").exists())

print("=== 6. 输入框回车发送 ===")
# Gradio 6 的规则：输入框是单行时 Enter 才触发 submit，多行时 Enter 只换行。
# 这里锁住 lines=1，否则用户按 Enter 只会换行，看起来像"发送按钮坏了"。
check("输入框初始单行（Enter 才会发送）", web.msg_box.lines == 1, web.msg_box.lines)
check("输入框可自动撑高", web.msg_box.max_lines == 6, web.msg_box.max_lines)
_sub = [
    (ev, fn.api_name)
    for fn in web.demo.fns.values()
    for b, ev in (getattr(fn, "targets", None) or [])
    if b == web.msg_box._id
]
check("输入框绑定了 submit 事件", any(ev == "submit" for ev, _ in _sub), _sub)
check("submit 与按钮走同一个处理函数", any(name == "chat_fn" for _, name in _sub), _sub)
_out_ids = [
    [getattr(o, "_id", o) for o in (fn.outputs or [])]
    for fn in web.demo.fns.values()
    for b, ev in (getattr(fn, "targets", None) or [])
    if b == web.msg_box._id and ev == "submit"
]
check(
    "提交后同时更新输入框与对话区",
    any(
        ids[0] == web.msg_box._id and web.chatbot._id in ids
        for ids in _out_ids
        if ids
    ),
    _out_ids,
)

print(f"\n通过 {passed} 项，失败 {failed} 项")
sys.exit(0 if failed == 0 else 1)
