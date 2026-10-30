"""清理长期记忆里的有害条目：假成功、做不到、过时的能力声明。

判定规则（两条，都只看记忆原文，不做语义猜测）：
1. 含"请提供文档路径 / 无法直接 / 权限限制"这类失败措辞 → 删。
   这类内容会不断被召回，模型照着复述，表现得像"永远做不到"。
2. 声称"已创建 / 已写入 文件"但文中提到的路径一个都不存在 → 删（假成功）。

用法：
  .venv/Scripts/python.exe .workbuddy/memory_clean.py            # 只报告
  .venv/Scripts/python.exe .workbuddy/memory_clean.py --apply    # 真删
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from myagent.agent import Agent  # noqa: E402
from myagent.memory import FAILURE_MARKS  # noqa: E402
from myagent.rag import RAGStore, build_embed_fn  # noqa: E402

PATH_RE = re.compile(r"[A-Za-z]:\\[^\s`\"'（）()，。；]+")
NAME_RE = re.compile(r"[^\s`\"'（）()，。；：]+\.(?:md|docx|txt|csv|doc|pdf)", re.I)


def existing_paths(text: str) -> list[str]:
    """文中提到的文件里，哪些是真实存在的。
    "notes.txt" 这种相对名要连工作区一起找——模型说写入时一般不带全路径。"""
    from myagent.tools import WORKSPACE

    cands = PATH_RE.findall(text) + NAME_RE.findall(text)
    bases = [Path("."), WORKSPACE]
    found = []
    for c in cands:
        if Path(c).exists():
            found.append(c)
            continue
        if any((b / c).exists() for b in bases):
            found.append(c)
    return found


def judge(doc: str) -> str | None:
    """返回删除理由；不删返回 None"""
    for m in FAILURE_MARKS:
        if m in doc:
            return f"失败措辞「{m}」"
    if Agent._claims_write(doc) and not existing_paths(doc):
        return "声称已写入但文件不存在"
    return None


def main(apply: bool) -> int:
    r = RAGStore(embed_fn=build_embed_fn())
    got = r.memory.get()
    ids = got.get("ids") or []
    docs = got.get("documents") or []
    print(f"记忆总数: {len(ids)}")

    drop: list[tuple[str, str, str]] = []
    for i, d in enumerate(docs):
        if not d:
            continue
        why = judge(d)
        if why:
            drop.append((ids[i], why, d.replace("\n", " ")[:70]))

    print(f"待清理: {len(drop)} 条")
    for i, (cid, why, head) in enumerate(drop, 1):
        print(f"  {i:2d}. [{why}] {head}")

    if apply and drop:
        r.memory.delete(ids=[cid for cid, _, _ in drop])
        print(f"\n已删除 {len(drop)} 条，剩余 {r.memory.count()} 条")
    elif drop:
        print("\n（未执行删除，加 --apply 生效）")
    return 0


if __name__ == "__main__":
    sys.exit(main("--apply" in sys.argv))
