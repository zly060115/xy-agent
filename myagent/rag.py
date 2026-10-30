"""
RAG 检索模块 - 基于本地 ChromaDB

设计约束（务必保持）：
1. 数据全部持久化在本地 chroma_db 目录，不外传
2. 双向量库结构：files（文件知识库） / memory（对话记忆库）互不干扰
3. 所有入库操作都是幂等的：同一文件重复入库不会产生重复片段
"""

import os
import re
from pathlib import Path

import chromadb
from chromadb.config import Settings

from .config import config

DB_DIR = config.chroma_dir

# 双库结构：两个独立的 collection，互不覆盖
COLLECTION_FILES = "knowledge_files"    # 文件解析入库
COLLECTION_MEMORY = "chat_memory"       # 对话历史记忆

# Markdown 标题行，用于给切片打"章节归属"
HEAD_LINE = re.compile(r"^(#{1,6})\s+(.+)$")


class RAGStore:
    def __init__(self, persist_dir: str | Path = DB_DIR, embed_fn=None):
        """
        embed_fn: 把 list[str] 转成 list[list[float]] 的函数。
                  默认为空时会用 Chroma 自带的默认嵌入（需联网下载模型）。
        """
        self.persist_dir = Path(persist_dir)
        self.persist_dir.mkdir(parents=True, exist_ok=True)

        self.client = chromadb.PersistentClient(
            path=str(self.persist_dir),
            settings=Settings(anonymized_telemetry=False, allow_reset=True),
        )
        self.embed_fn = embed_fn

        # 两个库分别拿，不重置、不清空，保证旧数据安全
        self.files = self.client.get_or_create_collection(
            name=COLLECTION_FILES, metadata={"hnsw:space": "cosine"}
        )
        self.memory = self.client.get_or_create_collection(
            name=COLLECTION_MEMORY, metadata={"hnsw:space": "cosine"}
        )

    # ---------- 切分 ----------

    @staticmethod
    def _split_para(para: str, chunk_size: int, overlap: int) -> list[str]:
        """单个段落切分：太长的按句子拆，单句仍超长才硬切"""
        if len(para) <= chunk_size:
            return [para]

        out: list[str] = []
        sentences = re.split(r"(?<=[。！？.!?])\s*", para)
        buf = ""
        for s in sentences:
            if len(buf) + len(s) <= chunk_size:
                buf += s
            else:
                if buf:
                    out.append(buf.strip())
                # 单句仍超长 → 硬切
                while len(s) > chunk_size:
                    out.append(s[:chunk_size])
                    s = s[chunk_size - overlap :]
                buf = s
        if buf.strip():
            out.append(buf.strip())
        return out

    @classmethod
    def split_text(cls, text: str, chunk_size: int = 600, overlap: int = 0) -> list[str]:
        """按章节累积切分：小段落合并到接近 chunk_size，遇到新章节才断开

        两个要点：
        1. **合并小段**：一个段落一段会产生大量"只有标题没有正文"的碎片，
           这种片段 embedding 几乎没信息量，检索时却会挤掉真正有用的内容。
        2. **带章节归属**：按段落切会把「### 5. Temperature」和它所属的
           「## 基础 6 件套」拆开，问"基础六件套有哪些"就检索不到。
           给每段加一行"（所属：章节名）"，语义不再依赖相邻片段。
        纯文本文档没有 ## 标题，不加前缀，切分行为与原来一致。
        """
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        if not text:
            return []

        raw: list[tuple[str, str]] = []  # (片段内容, 所属章节)
        buf = ""
        topic = ""  # 最近的二级标题
        has_body = False  # 缓冲区里除了标题行，有没有真正的正文

        def flush():
            nonlocal buf, has_body
            if buf.strip():
                raw.append((buf.strip(), topic))
            buf = ""
            has_body = False

        for para in (p.strip() for p in text.split("\n\n")):
            if not para:
                continue

            head = HEAD_LINE.match(para.split("\n", 1)[0])
            new_topic = None
            if head and len(head.group(1)) <= 2:
                # 去掉标题里的括注，避免前缀里出现嵌套括号「（所属：A（B））」
                new_topic = re.sub(r"[（(][^）)]*[）)]", "", head.group(2)).strip()

            # 单段就超长：先冲掉缓冲，再按句子拆
            if len(para) > chunk_size:
                flush()
                cur = new_topic or topic
                for piece in cls._split_para(para, chunk_size, overlap):
                    raw.append((piece, cur))
                continue

            # 遇到标题就断开：让"标题 + 它的正文"落在一起，不产生只有标题的空壳。
            # 但缓冲区里只有标题、还没有正文时不断开，否则「## 基础 6 件套」
            # 会单独成段，问"六件套有哪些"检索不到下面 6 个条目
            if buf and (len(buf) + len(para) + 2 > chunk_size or (head and has_body)):
                flush()

            if new_topic is not None:
                topic = new_topic
            buf = f"{buf}\n\n{para}" if buf else para
            if not head:
                has_body = True

        flush()

        chunks: list[str] = []
        for piece, topic in raw:
            # 本段已提到该章节名就不重复；太长的不加，避免超上限
            # （标题开头的段落也加：否则「### 5. Temperature」这一段完全不含
            #   「基础 6 件套」字样，问"六件套有哪些"检索不到它）
            if (
                topic
                and topic not in piece
                and len(piece) + len(topic) + 6 <= chunk_size
            ):
                piece = f"（所属：{topic}）\n{piece}"
            chunks.append(piece)

        # 加重叠，保留上下文连续性
        if overlap > 0 and len(chunks) > 1:
            merged = []
            for i, c in enumerate(chunks):
                if i == 0:
                    merged.append(c)
                else:
                    prev_tail = chunks[i - 1][-overlap:]
                    # 补换行：不补的话上段结尾会和本段开头粘成一行，
                    # 标题和数据标签会被拼坏（"## 基础 6 件套### 1. LLM"）
                    merged.append(prev_tail + "\n" + c)
            chunks = merged

        return chunks

    # ---------- 入库 ----------

    def add_file_chunks(
        self,
        file_path: str,
        chunks: list[str],
        source_name: str | None = None,
    ) -> int:
        """把一个文件的切片写入知识库。返回实际写入条数"""
        if not chunks:
            return 0

        source = source_name or Path(file_path).name
        # 先删掉这个来源的旧数据，避免重复入库
        self.delete_by_source(source)

        ids = [f"{source}::{i}" for i in range(len(chunks))]
        metadatas = [
            {"source": source, "path": str(file_path), "chunk_index": i}
            for i in range(len(chunks))
        ]

        kwargs = {"ids": ids, "documents": chunks, "metadatas": metadatas}
        if self.embed_fn:
            kwargs["embeddings"] = self.embed_fn(chunks)

        self.files.add(**kwargs)
        return len(chunks)

    def delete_by_source(self, source: str) -> int:
        """删除某个来源的所有切片"""
        existing = self.files.get(where={"source": source})
        if existing and existing.get("ids"):
            self.files.delete(ids=existing["ids"])
            return len(existing["ids"])
        return 0

    # ---------- 检索 ----------

    def search(
        self,
        query: str,
        top_k: int = 4,
        collection: str = "files",
    ) -> list[dict]:
        """检索最相关的片段（向量召回 + 关键词重排）

        只用向量的话，中文短问句的相似度全挤在 0.65~0.75 之间，
        分不出重点（实测问"基础六件套是哪六样"，正确的段落排在第 4 名开外）。
        所以先多召回一些，再用"问句里的词有没有出现在片段里"重排。
        """
        coll = self.files if collection == "files" else self.memory
        total = coll.count()
        if total == 0:
            return []

        # 多召回几倍候选，给重排留空间
        n = min(max(top_k * 4, 12), total)
        kwargs = {"query_texts": [query], "n_results": n}
        if self.embed_fn:
            kwargs.pop("query_texts")
            kwargs["query_embeddings"] = self.embed_fn([query])

        res = coll.query(**kwargs)

        docs = (res.get("documents") or [[]])[0]
        metas = (res.get("metadatas") or [[]])[0]
        dists = (res.get("distances") or [[]])[0]

        q_grams = _query_grams(query)
        out = []
        skipped = 0
        for i, doc in enumerate(docs):
            # 偶尔会读到残缺记录（文本为 None 或空串）。多进程同时打开同一个
            # Chroma 目录时尤其容易碰到。这里直接跳过：一条脏数据不该让整次
            # 检索失败——以前这种情况会抛 AttributeError，被调用方 except 吞掉，
            # 用户看到的就是「明明入库成功，问它却说没有」。
            if not isinstance(doc, str) or not doc.strip():
                skipped += 1
                continue
            vec = 1 - dists[i] if dists and i < len(dists) else 0.0
            cover = _coverage(q_grams, doc)
            meta = metas[i] if metas and i < len(metas) else {}
            out.append(
                {
                    "text": doc,
                    "source": (meta or {}).get("source", "?"),
                    # 向量占七成、关键词覆盖占三成：语义相近但没提到关键词的片段会掉下去
                    "score": 0.7 * vec + 0.3 * cover,
                    "vector_score": vec,
                    "keyword_hit": cover,
                }
            )

        out.sort(key=lambda d: -d["score"])
        return out[:top_k]

    def stats(self) -> dict:
        return {
            "knowledge_chunks": self.files.count(),
            "memory_chunks": self.memory.count(),
            "db_path": str(self.persist_dir),
        }


def _query_grams(text: str) -> set[str]:
    """把问句拆成检索用的词：中文二元字组 + 英文/数字词"""
    s = text.lower()
    grams: set[str] = set()
    for seg in re.sub(r"[^\u4e00-\u9fff]+", " ", s).split():
        if len(seg) == 1:
            grams.add(seg)
        grams.update(seg[i : i + 2] for i in range(len(seg) - 1))
    grams.update(re.findall(r"[a-z0-9][a-z0-9\.\+#_-]{1,}", s))
    return {g for g in grams if g.strip()}


def _coverage(grams: set[str], text: str) -> float:
    """问句里的词有多大比例出现在这段文字里（0~1）"""
    if not grams or not text:
        return 0.0
    low = text.lower()
    hit = sum(1 for g in grams if g in low)
    return hit / len(grams)


def build_embed_fn():
    """
    用 Ollama 的嵌入接口做向量化，避免引入 PyTorch 这套重依赖。
    需要本地有 nomic-embed-text 或 bge-m3 这类嵌入模型。
    """
    import os

    import httpx

    host = os.getenv("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
    model = os.getenv("EMBED_MODEL", "nomic-embed-text")

    def _embed(texts: list[str]) -> list[list[float]]:
        out = []
        with httpx.Client(timeout=60, trust_env=False) as client:
            for t in texts:
                r = client.post(
                    f"{host}/api/embeddings", json={"model": model, "prompt": t}
                )
                r.raise_for_status()
                out.append(r.json()["embedding"])
        return out

    return _embed
