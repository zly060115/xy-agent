"""把长期记忆里的旧助手名迁移成新名字。

背景：记忆的存储格式是「用户：xxx\n\n小满：yyy」——助手名写进了每条记录。
产品改名后，若只改代码不改存量记忆，每轮检索都会把「小满：」注入上下文，
模型看到旧称呼就会继续自称旧名（实测确实如此：提示词已写「你是 XY」，
模型仍回答「我是小满」）。

做法：逐条替换文本并重新计算 embedding 后 update 回 Chroma。
只改 memory 集合，不动 knowledge_files（知识库里是用户文档，不该被改写）。
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DATA_DIR", str(Path.home() / ".xy"))

from myagent.config import config
from myagent.rag import RAGStore, build_embed_fn

OLD, NEW = "小满", "XY"
DRY = "--apply" not in sys.argv

store = RAGStore(config.chroma_dir, embed_fn=build_embed_fn())
data = store.memory.get()
ids = data.get("ids") or []
docs = data.get("documents") or []

todo = [(i, d) for i, d in zip(ids, docs) if d and OLD in d]
print(f"记忆总数 {len(docs)}，含「{OLD}」{len(todo)} 条")
if not todo:
    print("无需迁移")
    raise SystemExit(0)

if DRY:
    for i, d in todo[:3]:
        print(f"  示例：{d.replace(OLD, NEW)[:70]!r}")
    print("\n这是预演，未写入。加 --apply 执行。")
    raise SystemExit(0)

new_docs = [d.replace(OLD, NEW) for _, d in todo]
new_ids = [i for i, _ in todo]
emb = build_embed_fn()(new_docs)

# 分批 update，避免单次请求过大
B = 16
for s in range(0, len(new_ids), B):
    store.memory.update(
        ids=new_ids[s : s + B],
        documents=new_docs[s : s + B],
        embeddings=emb[s : s + B],
    )
    print(f"  已更新 {min(s + B, len(new_ids))}/{len(new_ids)}")

# 校验
check = store.memory.get()
left = [d for d in (check.get("documents") or []) if d and OLD in d]
print(f"迁移完成，残留 {len(left)} 条")

hits = store.search("我的幸运数字是多少", top_k=2)
for h in hits:
    print(f"  检索抽查 [{h['score']:.3f}] {h['text'][:50]!r}")
