"""
小满 Agent 主入口

用法：
    python main.py                       # 交互模式
    python main.py "问题或任务"           # 单次执行
    python main.py --steps 25 "复杂任务"   # 指定最大步数
    python main.py --ingest <文件路径>     # 解析入库一个文件（需确认）
    python main.py --list-files           # 列出白名单目录下的可解析文件
    python main.py --stats                # 查看知识库状态
"""

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

from myagent.agent import Agent
from myagent.config import config
from myagent.confirm import build_gateway
from myagent.llm import build_llm
from myagent.memory import ConversationMemory
from myagent.rag import RAGStore, build_embed_fn
from myagent.runtime import set_runtime
from myagent.safe_reader import SafeFileReader


def create_runtime():
    """初始化全局运行时组件：模型、知识库、文件读取器、确认网关"""
    config.backend = os.getenv("BACKEND", config.backend)
    config.model = os.getenv("MODEL", config.model) or config.model
    config.ollama_host = os.getenv("OLLAMA_HOST", config.ollama_host) or config.ollama_host
    config.api_key = os.getenv("API_KEY", config.api_key) or ""
    config.base_url = os.getenv("BASE_URL", config.base_url) or config.base_url
    config.max_steps = int(os.getenv("MAX_STEPS", config.max_steps))
    # 图片理解：视觉模型留空则只用 OCR
    config.vision_model = os.getenv("VISION_MODEL", config.vision_model) or ""
    config.ocr_enabled = os.getenv("OCR_ENABLED", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )
    config.max_images = int(os.getenv("MAX_IMAGES", config.max_images) or config.max_images)
    config.vision_max_images = int(
        os.getenv("MAX_VISION_IMAGES", config.vision_max_images) or config.vision_max_images
    )

    llm = build_llm(config)
    rag = RAGStore(embed_fn=build_embed_fn())
    reader = SafeFileReader(
        vision=llm,
        ocr_enabled=config.ocr_enabled,
        max_images=config.max_images,
        vision_max_images=config.vision_max_images,
    )
    # 数据目录（记忆库/上传/工作区）自动进白名单，开源用户零配置可用
    config.uploads_dir.mkdir(parents=True, exist_ok=True)
    for d in (config.uploads_dir, config.workspace_dir, config.data_dir):
        d.mkdir(parents=True, exist_ok=True)
        if d.resolve() not in reader.allowed_dirs:
            reader.allowed_dirs.append(d.resolve())
    gateway = build_gateway()

    set_runtime(llm=llm, rag=rag, reader=reader, gateway=gateway)
    return llm, rag, reader, gateway


def banner(llm, rag, reader):
    print("=" * 60)
    print("  小满 Agent   |   能自己动手办事")
    print("=" * 60)
    print(f"  模型    : {llm.name}")
    print(f"  知识库  : {rag.stats()['knowledge_chunks']} 个片段")
    allowed = reader.info()["allowed_dirs"] or ["(未配置，文件读取已禁用)"]
    print(f"  可读目录: {', '.join(allowed)}")
    print(f"  确认模式: {os.getenv('CONFIRM_MODE', 'terminal')}")
    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(description="小满 Agent")
    parser.add_argument("goal", nargs="*", help="要执行的任务")
    parser.add_argument("--steps", type=int, help="最大步数")
    parser.add_argument("--ingest", help="解析入库一个文件")
    parser.add_argument("--list-files", action="store_true", help="列出可解析文件")
    parser.add_argument("--stats", action="store_true", help="查看知识库状态")
    args = parser.parse_args()

    try:
        llm, rag, reader, gateway = create_runtime()
    except Exception as e:
        print(f"❌ 初始化失败: {e}")
        print("\n提示：确认 Ollama 服务已启动，或检查 .env 配置")
        sys.exit(1)

    if args.steps:
        config.max_steps = args.steps

    # --- 知识库状态 ---
    if args.stats:
        s = rag.stats()
        print(f"知识库片段数: {s['knowledge_chunks']}")
        print(f"记忆片段数  : {s['memory_chunks']}")
        print(f"数据库路径  : {s['db_path']}")
        return

    # --- 列出可读文件 ---
    if args.list_files:
        info = reader.info()
        if not info["allowed_dirs"]:
            print("⚠️  未配置 ALLOWED_FILE_DIRS，无法读取任何文件")
            print("   请在 .env 中设置允许访问的目录")
            return
        print(f"允许目录: {info['allowed_dirs']}")
        print(f"支持格式: {info['allowed_ext']}\n")
        files = reader.list_allowed_files()
        if not files:
            print("(没有可解析的文件)")
        for f in files:
            print(f"  {f['size'] / 1024:8.1f} KB  {f['path']}")
        return

    # --- 解析入库 ---
    if args.ingest:
        from myagent.tools import execute_tool

        path = str(Path(args.ingest).resolve())
        print(f"准备解析: {path}")
        result = execute_tool("ingest_local_file", {"path": path}, reason="用户命令行请求入库")
        print(result)
        return

    banner(llm, rag, reader)

    # --- 单次任务 ---
    if args.goal:
        agent = Agent(llm, max_steps=config.max_steps, memory=ConversationMemory(rag))
        agent.run(" ".join(args.goal))
        print()
        return

    # --- 交互模式 ---
    agent = Agent(llm, max_steps=config.max_steps, memory=ConversationMemory(rag))
    print("\n输入目标，回车执行。命令：exit 退出 / reset 清空上下文 / stats 看知识库\n")
    while True:
        try:
            goal = input("你 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见 🌾")
            break

        if not goal:
            continue
        low = goal.lower()
        if low in ("exit", "quit", "q"):
            print("再见 🌾")
            break
        if low == "reset":
            agent.reset()
            print("上下文已清空\n")
            continue
        if low == "stats":
            s = rag.stats()
            print(f"知识库 {s['knowledge_chunks']} 片段 / 记忆 {s['memory_chunks']} 片段\n")
            continue

        agent.run(goal)
        print()


if __name__ == "__main__":
    main()
