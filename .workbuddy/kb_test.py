# -*- coding: utf-8 -*-
"""知识库链路回归：标记剥离、脏记录容错、检索可用性。"""
import sys

sys.path.insert(0, ".")
from myagent.agent import scrubbed_rag_tags
from myagent.rag import RAGStore, _coverage, build_embed_fn

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


print("[1] 注入标记剥离")
cases = [
    ("[知识库·01_生产报告.txt] 中提到良率 97.6%", "中提到良率 97.6%"),
    ("根据知识库中的内容，[知识库·A.md] 显示 Q3 良率 97.6%", "根据知识库中的内容，显示 Q3 良率 97.6%"),
    ("【知识库：文档.docx】第一段内容是 X", "第一段内容是 X"),
    ("没有标记的正常回答", "没有标记的正常回答"),
    ("", ""),
    ("来源：01_生产报告.txt\n一次良率为 97.6%", "一次良率为 97.6%"),
    ("数据显示（来源：A.md）良率 97.6%", "数据显示良率 97.6%"),
]
for src, want in cases:
    got = scrubbed_rag_tags(src)
    check(f"{src[:24]!r}", got == want, f"got={got!r} want={want!r}")

print("[2] 脏记录容错（doc 为 None / 空串都不许崩）")
check("_coverage(None) 返回 0", _coverage({"a"}, None) == 0.0)
check("_coverage('') 返回 0", _coverage({"a"}, "") == 0.0)
check("_coverage 正常", _coverage({"良率"}, "一次良率 97.6%") > 0)

print("[3] 真实库检索可用")
r = RAGStore(embed_fn=build_embed_fn())
s = r.stats()
print(f"  库容量: {s['knowledge_chunks']} 段")
check("知识库非空", s["knowledge_chunks"] > 0)
hits = r.search("生产报告的良率是多少", top_k=3)
check("能检回结果", len(hits) > 0, f"hits={len(hits)}")
if hits:
    print(f"  首条: [{hits[0]['source']}] score={hits[0]['score']:.2f}")
    check("得分合理(>0.2)", hits[0]["score"] > 0.2, f"score={hits[0]['score']:.3f}")
    check("来源非空", bool(hits[0]["source"].strip()))
    check("文本非空", bool(hits[0]["text"].strip()))

print("[4] 高分问句应命中对应文档")
h2 = r.search("Agent 搭建方法论讲了什么", top_k=1)
if h2:
    check("命中 Agent搭建方法论.md", "Agent搭建方法论" in h2[0]["source"], h2[0]["source"])

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
