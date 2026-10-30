# 小满 Agent

一个能自己动手办事的本地 Agent，带本地知识库、安全文件读取和人工确认。

## 核心能力

| 能力 | 说明 |
|---|---|
| 自主执行 | 给目标，自己规划步骤、调工具、看结果，直到完成 |
| **文档分析** | 上传文件直接出重点：一句话结论、核心要点、关键信息、注意事项。不入库 |
| RAG 知识库 | 文件解析 → 切片 → 向量化 → 存入本地 ChromaDB → 语义检索 |
| 安全文件读取 | 白名单目录、只读、禁止系统目录，越界直接拒绝 |
| 人工确认 | 每次读本地文件都要你点头，Agent 不能私自读 |
| 双模型后端 | 本地 Ollama / 云端 API，改配置即可切换 |
| Gradio 网页 | 文档分析、对话、上传入库、知识库状态、风格设置 |

### 文档分析怎么用

打开网页 → 「文档分析」→ 拖入 txt / md / pdf / docx / **doc** → 点「开始分析」。

分析过程会实时显示进度（逐段抽取 3/14…），不用盯着空白页猜。

**输出结构**（固定 7 段，不让模型自由发挥）：

```
文档概况     一句话说清是什么文档
文档结构     实际章节清单
逐部分要点   按章节逐节列，每节至少两条
关键数据     金额 / 数量 / 日期 / 编号，逐条列
涉及人员     人名、部门、角色
结论与要求   明确要求的动作、交付物、期限
疑点与风险   矛盾、遗漏、需要注意的地方
覆盖范围     哪些内容读不到（图片、扫描页）
```

**怎么保证"全"**（这几条是踩坑后改出来的）：

| 做法 | 解决什么 |
|---|---|
| 按章节切段，每段 ≤1200 字 | 整篇一次喂进去，7B 只看开头就收尾 |
| 逐段"抽取事实"而不是"概括" | 概括必然丢细节 |
| 逐部分要点由**代码拼装**，不交给模型 | 模型组装时会整节漏掉（实测漏过一整节） |
| 关键数据模型漏了就**从原文扫数字兜底** | 7B 分类能力弱，常写"无" |
| 模板说明行会被**代码过滤** | 7B 会把提示词当内容抄出来 |
| 重复条目去重 | 7B 长输出末尾会复读（实测同一句 57 遍） |
| 并发 3 路抽取 | 14 段从 6 分钟压到 75 秒 |

长度策略：≤600 字直接分析；更长按章节切段，每段 ≤1200 字，最多 30 段；
超过 20 万字只分析前 20 万字并提示截断。

**耗时参考**（RTX 4060，8G 显存）：2 千字约 1 分钟，1.4 万字约 75 秒。

分析默认**不入库**。想之后在对话里追问细节，勾上「同时存入知识库」。

> **图片读不到**：docx/pdf 里的截图、扫描页、图表，文字提取拿不到内容。
> 报告末尾的「覆盖范围」会明确写出有几张图片没覆盖。
> 要分析图片内容，得换视觉模型（如 qwen2.5-vl），当前 7B 文本模型做不到。

## 快速开始

```bash
# 1. 确认 Ollama 服务在跑
ollama serve

# 2. 拉模型（模型 + 嵌入）
ollama pull qwen2.5:7b
ollama pull nomic-embed-text

# 3. 配置
copy .env.example .env
# 编辑 .env，至少设置 ALLOWED_FILE_DIRS

# 4. 启动
.venv\Scripts\python.exe main.py          # 命令行
.venv\Scripts\python.exe web.py           # 网页端 http://127.0.0.1:7860
```

## 文件安全机制

三层防护，缺一不可：

**第一层：白名单目录**
只有 `ALLOWED_FILE_DIRS` 里配置的目录能被访问。不配置 = 什么都读不了（默认拒绝）。

**第二层：路径校验**
每次访问都检查：绝对路径、非符号链接、落在白名单内、路径段不含 `Windows/System32/Program Files/AppData` 等受保护目录、扩展名在白名单、大小不超 50MB。

**第三层：人工确认**
`read_local_file` / `ingest_local_file` / `list_local_files` 三个工具，调用前必先弹出确认。你拒绝一次，本次会话内相同请求不再打扰。

本模块**不提供任何写、删、改文件的接口**，物理上做不到修改本地文件。

## 双向量库结构

```
chroma_db/
├── knowledge_files   ← 文件解析入库的内容
└── chat_memory       ← 对话记忆
```

两个 collection 完全隔离，互不覆盖。同一文件重复入库会先删旧片段再写新的，不会产生重复数据。

## 命令行用法

```bash
python main.py "帮我总结知识库里的定价策略"
python main.py --ingest "C:\资料\产品手册.pdf"
python main.py --list-files
python main.py --stats
python main.py --steps 25 "复杂任务"
```

## 切换成云端 API

改 `.env`：

```ini
BACKEND=api
API_KEY=sk-xxxx
BASE_URL=https://api.deepseek.com/v1
MODEL=deepseek-chat
```

代码不用动。常用 base_url：

| 服务 | BASE_URL | MODEL |
|---|---|---|
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` |
| 通义千问 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` |
| 智谱 GLM | `https://open.bigmodel.cn/api/paas/v4` | `glm-4-plus` |
| OpenAI | `https://api.openai.com/v1` | `gpt-4o-mini` |

## 加自己的工具

在 `myagent/tools.py` 写函数，注册进 `TOOLS`：

```python
def tool_weather(city: str) -> str:
    return f"{city} 今天晴，25 度"

TOOLS["weather"] = {
    "fn": tool_weather,
    "spec": {
        "name": "weather",
        "description": "查某城市天气",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string", "description": "城市名"}},
            "required": ["city"],
        },
    },
}
```

如果这个工具需要人工确认，把名字加进 `myagent/confirm.py` 的 `SENSITIVE_TOOLS`。

## 目录结构

```
├── main.py            # 命令行入口
├── web.py             # Gradio 网页端
├── .env               # 配置（不进版本库）
├── .env.example
├── myagent/
│   ├── agent.py       # ReAct 循环
│   ├── tools.py       # 工具集
│   ├── analyst.py     # 文档分析（全文通读 + 分级总结）
│   ├── rag.py         # ChromaDB 知识库
│   ├── safe_reader.py # 安全文件读取
│   ├── confirm.py     # 人工确认网关
│   ├── llm.py         # 双模型后端
│   ├── runtime.py     # 全局组件容器
│   └── config.py
├── workspace/         # Agent 产出文件（写文件都落这里）
├── uploads/           # 网页上传的临时文件
└── chroma_db/         # 向量数据（自动生成）
```

## 已知限制

- 本地 7B 模型规划能力弱于云端大模型，复杂任务容易绕圈。任务描述越具体，成功率越高。
- 7B 在 4060 上大约 2-5 秒一步，5 步任务约十几秒。
- **文档分析的分节内容由抽取+代码拼装保证覆盖，但"一句话概况"和"疑点"仍是模型判断，可能偏浅。**
  换 API 模型（DeepSeek 等）这两块会明显变好。
- 文档里的图片、扫描页、截图**读不到内容**，报告会写明有几张没覆盖。
- 长文档分析耗时：2 千字约 1 分钟；1.4 万字约 75 秒（14 段，并发 3 路抽取）。
  段数越多越久，进度会实时显示。
- 扫描版 PDF（纯图片）提取不出文字，会明确报错。
- `.doc`（Word 97-2003 老格式）依赖本机 Word 或 WPS 做转换：原文件只读，
  在系统临时目录转出 .docx 副本再解析。没装 Office/WPS 会明确报错，提示另存为 .docx。
- 风格设置只影响措辞，不会改变工具调用和安全策略。
