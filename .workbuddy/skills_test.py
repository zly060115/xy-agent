"""skills.py 单元测试：能否扫到、能否只挑对的那一份、注入是否受控。"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from myagent.skills import SkillStore, _parse_frontmatter  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}  {detail}")


print("=== frontmatter 解析 ===")
meta, body = _parse_frontmatter("---\nname: a-b\ndescription: 做某事\nkeywords: x, y\n---\n正文")
check("name 解析", meta.get("name") == "a-b", meta)
check("description 解析", meta.get("description") == "做某事", meta)
check("正文剥离干净", body == "正文", repr(body))
meta2, body2 = _parse_frontmatter("没有头的正文")
check("无头时整篇为正文", meta2 == {} and body2 == "没有头的正文", (meta2, body2))

print("=== 扫描真实目录 ===")
store = SkillStore(Path.home() / ".xy" / "skills")
skills = store.load()
check("扫到 doc-analysis", any(s.name == "doc-analysis" for s in skills), [s.name for s in skills])
ds = store.get("doc-analysis")
check("description 非空", ds is not None and bool(ds.description))
check("keywords 拆开", ds is not None and "合同" in ds.keywords, ds.keywords if ds else None)

print("=== 匹配：该命中的 ===")
for q in ["帮我分析这份合同有什么风险", "总结一下这份报告的重点", "看看这个文档有没有问题"]:
    hits = store.match(q)
    check(f"「{q[:12]}…」命中 doc-analysis",
          bool(hits) and hits[0].name == "doc-analysis", [h.name for h in hits] if hits else [])

print("=== 匹配：不该命中的 ===")
for q in ["今天几号", "帮我算 1280*0.123", "你好你能做什么"]:
    hits = store.match(q)
    check(f"「{q}」不注入技能", hits == [], [h.name for h in hits] if hits else [])

print("=== 注入渲染 ===")
long_skill = type(ds)(name="t", description="d", body="字" * 5000, path=Path("x"))
rendered = store.render(long_skill)
check("超长被截断", len(rendered) < 2600, len(rendered))
check("截断有说明", "已截断" in rendered)

hits = store.match("帮我分析这份合同有什么风险")
check("match 最多返回 1 份", len(hits) <= 1, len(hits))

print(f"\n结果：{PASS} 通过，{FAIL} 失败")
sys.exit(0 if FAIL == 0 else 1)
