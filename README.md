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
| **模型自选** | 13 家厂商预设，网页点选 + 填自己的密钥 + 一键测试连接，热切换不重启 |
| 桌面版 | 双击 `desktop.bat`，原生窗口打开（pywebview + WebView2），无地址栏、关窗即退 |
| Gradio 网页 | 对话、文档分析、上传入库、知识库状态、模型选择、风格设置 |

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
# 0. 建虚拟环境并装依赖（需要 Python 3.10+）
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt

# 1. 确认 Ollama 服务在跑
ollama serve

# 2. 拉模型（模型 + 嵌入）
ollama pull qwen2.5:7b
ollama pull nomic-embed-text

# 3. 配置
copy .env.example .env
# 编辑 .env：
#   DATA_DIR            数据目录（记忆库/知识库/上传文件/工作区），
#                       留空 = 用户主目录下 .xiaoman，建议放代码之外
#   ALLOWED_FILE_DIRS   额外允许 Agent 读取的目录（数据目录会自动加入白名单）
#   ALLOWED_WRITE_DIRS  Agent 能写的目录，留空 = 桌面/文档/下载
#   （模型不用在这里配，启动后到网页「模型」标签页点选即可）

# 4. 启动
双击 desktop.bat                        # 桌面版：原生窗口（推荐）
.venv\Scripts\python.exe main.py        # 命令行
.venv\Scripts\python.exe web.py         # 网页端 http://127.0.0.1:7860
```

## 桌面版（原生窗口）

`desktop.py` 是一个约 300 行的 pywebview 外壳，不碰 `web.py` 和核心逻辑：

- **窗口先出来，界面后加载**。双击后立刻看到启动画面；Gradio（chromadb、
  onnxruntime 这些都要好几秒）在后台线程加载，就绪后自动切进主界面。
- **端口复用**。7860 已有小满在跑就直接接入（秒开），被别的程序占用就换端口，
  不会抢。
- **关窗即退**。窗口关闭后连服务线程一起收掉，任务管理器里不留 `python.exe`。
- 日志在 `~/.xiaoman/desktop.log`；排查界面问题用 `desktop.bat -d`
  （保留控制台 + 窗口内 F12 开发者工具）。

图标由 `.workbuddy/make_icon.py` 生成（PIL 画圆角渐变 + 「满」字）。
桌面快捷方式由 `.workbuddy/make_shortcut.py` 创建：指向 `pythonw.exe` 而不是
`.bat`，双击不闪黑框，图标用 `assets/icon.ico`；换电脑或搬目录后跑一次即可重建。
网页端照常可用：`web.py` 起的服务，桌面窗口和浏览器可以同时连。

## 数据目录（与代码分离）

所有会积累的数据都在 `DATA_DIR`（默认 `~/.xiaoman`）下，代码目录保持干净：

```
~/.xiaoman/
├── chroma_db/       # 向量库：知识库 + 对话长期记忆
├── uploads/         # 网页上传的原始文档
├── workspace/       # Agent 的读写工作区
├── skills/          # 按需加载的技能（SKILL.md）
├── webview_cache/   # 桌面版窗口的缓存（可删，删了下次启动稍慢）
└── desktop.log      # 桌面版日志
```

这样做的好处：开源、换电脑、重新 clone 代码互不影响；备份这个目录 = 备份全部记忆。

## 报告自检（准确性兜底）

7B 模型会做三件事让人难受：编数字、漏章节、少写小节。提示词管不住这类问题，
所以分析结束后由 `myagent/checks.py` 用确定性代码核对，有疑点就**如实写进报告的「自检」一节**，
而不是偷偷改掉——悄悄修正会让整份报告看起来都可信，更危险。

核对三项：

| 检查 | 判据 | 减少的误报 |
|------|------|-----------|
| 数值溯源 | 「关键数据」里的每个数必须能在原文找到 | 千分位、全角、`1.5万→15000`、两数加减的汇总值都算命中 |
| 章节覆盖 | 原文标题被报告提及的比例低于 60% 才提示 | 摘要式报告本来就不点章节名，逐章报警会全是噪声 |
| 结构完整 | 概况/关键数据/结论三个小节是否齐全 | 只卡决策必需的三项，辅助小节不参与 |

单元测试：`.venv\Scripts\python.exe .workbuddy\checks_test.py`（22 条，一半是"不该误报"的反例）。

## 技能（Skills）

兼容 `SKILL.md` 约定（frontmatter + Markdown 正文），放进 `DATA_DIR/skills/<名字>/SKILL.md` 即可：

```markdown
---
name: doc-analysis
description: 分析合同、报告、纪要时的输出规范与自检清单
keywords: 文档,分析,合同,报告,风险
---
# 正文：具体的做法
```

**只注入命中的那一份，不重复注入。** 8192 上下文里塞多份技能，7B 会把 A 的步骤套到 B 上，
还会挤掉工具定义。匹配走「关键词直接命中 + 二元字重叠」两条路，取高者。

内置一份 `doc-analysis`（文档分析规范），首次运行时可自行增删。

## 文件安全机制

三层防护，缺一不可：

**第一层：白名单目录**
只有 `ALLOWED_FILE_DIRS` 里配置的目录能被访问。不配置 = 什么都读不了（默认拒绝）。

**第二层：路径校验**
每次访问都检查：绝对路径、非符号链接、落在白名单内、路径段不含 `Windows/System32/Program Files/AppData` 等受保护目录、扩展名在白名单、大小不超 50MB。

**第三层：人工确认**
`read_local_file` / `ingest_local_file` / `list_local_files` 三个工具，调用前必先弹出确认。你拒绝一次，本次会话内相同请求不再打扰。

本模块**不提供任何写、删、改文件的接口**，物理上做不到修改本地文件。

## 写文件到哪（可写范围）

读取是"只读 + 白名单"，写入是另一套规则：

| 位置 | 能不能写 | 说明 |
|---|---|---|
| 工作区 `DATA_DIR/workspace` | 可以，免确认 | Agent 的默认落点 |
| 桌面 / 文档 / 下载 | 可以 | 用户说"放我桌面"就真写到桌面，不再塞进工作区 |
| `ALLOWED_WRITE_DIRS` 里配的目录 | 可以 | 想加别的位置就配这个 |
| 系统目录、程序目录、AppData | 永远拒绝 | 即使配进白名单也拦 |
| 其它任意路径 | 拒绝 | 返回明确原因，不会偷偷换个地方写 |

扩展名只放 `.txt .md .docx .csv .json`。`.bat / .exe / .ps1 / .vbs / .lnk` 这类能被双击执行的**一律拒绝**——写入路径上不做"可以执行代码"的事。`.docx` 生成的是真正的 Word 文档（标题认 `#`、列表认 `-`），不是改了后缀的文本。

另外两道防"嘴上说做完"：
- 回答里声称"已创建/已写入文件"但这一轮没有真正写成功 → 打回要求真实执行；仍不做就在回答末尾标注"文件还没有生成"。
- 这类假成功不会写进长期记忆，避免以后被当成事实反复复述。


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
python main.py --models                        # 看有哪些厂商可选
python main.py --set-model deepseek --key sk-xxxx   # 存下模型选择
```

## 选模型：界面点选，填自己的密钥

三个入口，优先级 **界面保存 > `.env` > 代码默认值**。

**1）网页端（推荐，改完不用重启）**

启动 `web.py` → 打开「模型」标签页。最省事的用法是**快速选择**：
一列精选卡片（免费/便宜/长文本/综合最强各一档），**点一张卡片，
厂商、模型名、接口地址、视觉模型全部自动填好**，只需要再填密钥：

| 卡片 | 说明 |
|---|---|
| 🆓 qwen2.5:7b · 本地 Ollama | 免费，数据不出本机 |
| 💰 deepseek-v4-flash · DeepSeek | 大厂里最便宜，中文强 |
| 🎁 glm-5.3-flash · 智谱 GLM | 有免费额度，原生会看图 |
| 📄 kimi-k3 · Kimi | 超长上下文 |
| ⭐ qwen3.8-max · 通义千问 | 综合最强档 |
| 🧩 DeepSeek-V4-Flash · 硅基流动 | 一个 key 调多家开源模型 |

想用列表外的模型，展开「高级设置」手动选厂商、填任意模型名：

| 步骤 | 说明 |
|---|---|
| 选厂商 | 13 家预设（含本地 Ollama），切厂商时模型名、接口地址、视觉模型一起自动换 |
| 填模型名 | 下拉里是常用值，**可以直接输入任意模型名**（厂商换代很快，预设会过期） |
| 填密钥 | 本地模型不需要；云端模型留空则沿用上一次保存的那把 |
| 测试连接 | 真发一次请求，报错会翻成中文（401 密钥错 / 404 模型名错 / 429 余额或限流 / 超时） |
| 保存并启用 | 写入 `DATA_DIR/settings.json`，模型、Agent、读图组件同时热切换 |

**2）命令行（服务器上没有浏览器时）**

```bash
python main.py --models                              # 列出所有厂商、常用模型、密钥申请地址
python main.py --set-model deepseek --key sk-xxxx    # 存下来，下次启动生效
python main.py --set-model ollama --model qwen2.5:7b # 切回本地
python main.py --set-model custom --model my-llm \
    --base-url https://gateway.example.com/v1 --key sk-xxxx
```

**3）`.env`（当作默认值）**

```ini
BACKEND=api
API_KEY=sk-xxxx
BASE_URL=https://api.deepseek.com
MODEL=deepseek-v4-flash
```

**预置厂商**（`myagent/providers.py`，都走 OpenAI 兼容协议，所以一套客户端通吃）：

| 厂商 | 接口地址 | 常用模型（仅为默认值，可手填） |
|---|---|---|
| 本地 Ollama | `http://localhost:11434` | `qwen2.5:7b` / `qwen2.5:14b` / `deepseek-r1:7b` |
| DeepSeek | `https://api.deepseek.com` | `deepseek-v4-flash` / `deepseek-v4-pro` |
| 阿里云百炼 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen3.8-max` / `qwen3.8-flash` / `qwen-long` |
| 智谱 GLM | `https://open.bigmodel.cn/api/paas/v4` | `glm-5.3` / `glm-5.3-flash` / `glm-4.6` |
| 月之暗面 Kimi | `https://api.moonshot.cn/v1` | `kimi-k3.1-pro` / `kimi-k3` / `kimi-k2.7-code` |
| 字节豆包（方舟） | `https://ark.cn-beijing.volces.com/api/v3` | `doubao-seed-2-1-pro-260915` |
| 腾讯混元 | `https://tokenhub.tencentcloudmaas.com/v1` | `hy3` / `hy4-preview` |
| MiniMax | `https://api.minimaxi.com/v1` | `MiniMax-M3` |
| 阶跃星辰 | `https://api.stepfun.com/v1` | `step-3.7-flash` |
| 硅基流动 | `https://api.siliconflow.cn/v1` | `deepseek-ai/DeepSeek-V4-Flash` |
| OpenAI | `https://api.openai.com/v1` | `gpt-4o-mini` / `gpt-4o` |
| OpenRouter | `https://openrouter.ai/api/v1` | `openai/gpt-4o-mini` 等「厂商/型号」 |
| 自定义 | 自己填 | 自建 vLLM / LM Studio / 公司网关 |

**密钥存在哪、安全吗**：存在 `DATA_DIR/settings.json`（默认 `~/.xiaoman/`），
**明文**、只在本机、已在 `.gitignore` 里，不会跟着代码推到 GitHub；界面只显示掩码。
为什么不明文加密：本地单用户工具加一层密码，最常见的结局是用户自己忘了密码、
密钥找不回来。需要更严的话请用系统的密钥管理（或只填临时 key 用完就清除）。

## HTTP API（程序调用）

网页底部「Use via API」打开的是**接口文档页**（`http://127.0.0.1:7860/?view=api`），
正常使用界面访问 `http://127.0.0.1:7860/` 就行。对外只暴露 10 个端点，
都是人工命名的；回车提交、状态刷新、页面初始化这类界面内部动作不占用端点。

| 端点 | 入参 | 作用 |
|---|---|---|
| `/chat` | message, history | 对话（返回完整历史，回复取 `history[-1]`） |
| `/reset` | — | 清空会话上下文 |
| `/analyze` | file, also_ingest | 上传文档或图片 → 分析重点 |
| `/ingest` | file | 上传文件 → 解析入库 |
| `/stats` | — | 知识库状态 |
| `/provider_info` | pid | 查某厂商的默认模型与接口地址 |
| `/test_model` | pid, model, key, base_url, vision | 测试连通性 |
| `/apply_model` | 同上 | 保存并启用（热切换，不用重启） |
| `/clear_model` | — | 清除界面保存的配置 |
| `/save_style` | style | 设置回复风格 |

Python 客户端：

```python
from gradio_client import Client, handle_file

client = Client("http://127.0.0.1:7860")

# 对话
result = client.predict(message="帮我算一下 1280*0.123", history=[], api_name="/chat")
reply = result[1][-1]["content"]

# 分析一份文档
result = client.predict(
    file_obj=handle_file("C:/资料/合同.pdf"),
    also_ingest=False,
    api_name="/analyze",
)
print(result)
```

不用客户端库也可以走 REST（Gradio 的两步调用，先拿 event_id 再收流式结果）：

```bash
curl -X POST http://127.0.0.1:7860/gradio_api/call/stats -H "Content-Type: application/json" -d '{"data": []}'
# → {"event_id": "..."}
curl -N http://127.0.0.1:7860/gradio_api/call/stats/<event_id>
```

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
│   ├── llm.py         # 模型后端（本地 Ollama / OpenAI 兼容）
│   ├── providers.py   # 13 家厂商预设（接口地址 + 常用模型名）
│   ├── settings.py    # 模型选择与密钥的保存/校验（存 DATA_DIR）
│   ├── runtime.py     # 全局组件容器
│   └── config.py
└── kb_test/           # 测试资料（含 Agent 搭建方法论）

数据目录（DATA_DIR，默认 ~/.xiaoman，不随代码走）：
├── chroma_db/         # 向量数据：知识库 + 对话记忆
├── uploads/           # 网页上传的原始文档
├── workspace/         # Agent 读写工作区
├── skills/            # 按需加载的技能
└── settings.json      # 你选的模型与密钥（明文，仅本机）
```

## 已知限制

- 本地 7B 模型规划能力弱于云端大模型，复杂任务容易绕圈。任务描述越具体，成功率越高。
- 7B 在 4060 上大约 2-5 秒一步，5 步任务约十几秒。
- **文档分析的分节内容由抽取+代码拼装保证覆盖，但"一句话概况"和"疑点"仍是模型判断，可能偏浅。**
  换 API 模型（DeepSeek 等）这两块会明显变好。
- 模型复述工具结果时会抄错数字（计算器给 3043，回答写成 3053）。已做两道兜底：
  只差一位时打回重算一次；仍不一致就在答案末尾注明矛盾、以工具结果为准。差两位以上检测不到。
- **PDF 复杂表格**（合并单元格、跨页表）目前用 pypdf 抽文字，表头和数值的对应关系可能错位，
  比如把最后一根柱子的数值说成第一季度的。要根治得上版面分析模型：`docling`（MIT，CPU 可跑，
  每份文档约 200-500MB 内存）能识别表格结构与阅读顺序，中文文字仍交给 RapidOCR。
  代价是会拉进 torch（约 3GB 依赖），所以没做默认安装。
- 文档里的图片、扫描页、截图**读不到内容**，报告会写明有几张没覆盖。
- 长文档分析耗时：2 千字约 1 分钟；1.4 万字约 75 秒（14 段，并发 3 路抽取）。
  段数越多越久，进度会实时显示。
- 扫描版 PDF（纯图片）提取不出文字，会明确报错。
- `.doc`（Word 97-2003 老格式）依赖本机 Word 或 WPS 做转换：原文件只读，
  在系统临时目录转出 .docx 副本再解析。没装 Office/WPS 会明确报错，提示另存为 .docx。
- 风格设置只影响措辞，不会改变工具调用和安全策略。
