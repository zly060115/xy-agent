"""网页端 API 端点清单回归。

背景：同一个函数被绑到多个组件时，Gradio 会自动生成 chat_fn_1 / stats_fn_1
这种后缀端点，API 文档页就变成一堆看不出用途的重复入口。这个脚本锁住三件事：

1. 对外暴露的端点必须是人工命名的，且不带 _1/_2 后缀
2. 界面内部动作（回车提交、保存后刷新状态、页面初始化）必须 private
3. 每个对外端点都有输入输出定义，可以被 gradio_client 真正调用

跑法：.venv\\Scripts\\python.exe .workbuddy/api_test.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

# 用临时数据目录，绝不碰真实配置
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="xiaoman_api_"))

import web  # noqa: E402

# 对外端点白名单：改这里等于改对外的接口承诺
EXPECTED_PUBLIC = [
    "chat",
    "reset",
    "analyze",
    "ingest",
    "stats",
    "provider_info",
    "test_model",
    "apply_model",
    "clear_model",
    "save_style",
]

passed = failed = 0


def check(name, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1
        print(f"  ✅ {name}")
    else:
        failed += 1
        print(f"  ❌ {name}" + (f" | {detail}" if detail else ""))


deps = web.demo.config["dependencies"]
public = [d for d in deps if d.get("api_visibility") == "public"]
private = [d for d in deps if d.get("api_visibility") != "public"]
public_names = [d.get("api_name") or "" for d in public]


def events(dep):
    """依赖项绑定的 (组件, 事件名) 列表"""
    return [e for _, e in (dep.get("targets") or []) if e]


print("=== 1. 对外端点集合 ===")
check(
    "对外端点数量与白名单一致",
    len(public) == len(EXPECTED_PUBLIC),
    f"实际 {len(public)}，白名单 {len(EXPECTED_PUBLIC)}",
)
missing = set(EXPECTED_PUBLIC) - set(public_names)
extra = set(public_names) - set(EXPECTED_PUBLIC)
check("没有缺失的端点", not missing, f"缺 {sorted(missing)}")
check("没有计划外的端点", not extra, f"多 {sorted(extra)}")

print("=== 2. 端点命名 ===")
auto = [n for n in public_names if any(n.endswith(f"_{i}") for i in range(10))]
check("没有 Gradio 自动加的后缀端点", not auto, f"发现 {auto}")
check("没有空名字的端点", all(n.strip() for n in public_names),
      f"{[n for n in public_names if not n.strip()]}")
check("没有把 False 转成字符串的名字", "false" not in public_names,
      f"{[n for n in public_names if n.startswith('false')]}")

print("=== 3. 界面内部动作必须私有 ===")
check("存在私有端点", len(private) > 0, f"实际 {len(private)}")
# 回车提交与按钮发送是同一个动作，不该额外占一个对外端点
check("回车提交没有单独对外暴露", public_names.count("chat") == 1,
      f"chat 出现 {public_names.count('chat')} 次")

leaked_load = [d.get("api_name") for d in deps
               if "load" in events(d) and d.get("api_visibility") == "public"]
check("页面初始化加载没有对外暴露", not leaked_load, f"{leaked_load}")

leaked_then = [d.get("api_name") for d in deps
               if "then" in events(d) and d.get("api_visibility") == "public"]
check("后置状态刷新没有对外暴露", not leaked_then, f"{leaked_then}")

leaked_submit = [d.get("api_name") for d in deps
                 if "submit" in events(d) and d.get("api_visibility") == "public"]
check("回车提交（submit 事件）没有对外暴露", not leaked_submit, f"{leaked_submit}")

print("=== 4. 每个对外端点都有输入输出定义 ===")
for d in public:
    name = d.get("api_name")
    check(
        f"/{name} 定义了 inputs/outputs",
        d.get("inputs") is not None and d.get("outputs") is not None,
        f"inputs={d.get('inputs')} outputs={d.get('outputs')}",
    )

print("=== 5. 每个对外端点都能在界面里手工触发 ===")
# 排除 load/then/submit 这类非人为事件后，public 端点必须挂在按钮/下拉上
for d in public:
    name = d.get("api_name")
    ev = events(d)
    check(
        f"/{name} 绑定在可交互组件上",
        any(e in ("click", "change", "input") for e in ev),
        f"事件 {ev}",
    )

print("\n--- 对外端点一览 ---")
for d in public:
    ev = events(d)
    print(
        f"  /{d['api_name']:<14} 入参 {len(d.get('inputs') or []):<2} "
        f"出参 {len(d.get('outputs') or []):<2} 事件 {ev}"
    )

print(f"\n通过 {passed} 项，失败 {failed} 项")
sys.exit(1 if failed else 0)
