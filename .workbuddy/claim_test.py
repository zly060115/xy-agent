"""「声称已写但没写」防线的回归测试。

用一个假 LLM 复现小模型的行为：
1. 直接说"已创建文件"，不调工具 → 必须被打回，逼出真实 write_file
2. 打回也不写、继续嘴硬 → 回答里必须出现事实更正，且不写入长期记忆
3. 「文件在哪」这类提问不能被误判成写请求

跑法：.venv/Scripts/python.exe .workbuddy/claim_test.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from myagent.agent import Agent  # noqa: E402
from myagent.tools import WORKSPACE  # noqa: E402

ok = 0
fail = 0


def check(name: str, cond: bool, extra: str = ""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  [PASS] {name}")
    else:
        fail += 1
        print(f"  [FAIL] {name}  {extra}")


class FakeLLM:
    """按剧本返回的假模型，记录自己被问了几次"""

    def __init__(self, replies):
        self.replies = replies
        self.calls = 0
        self.seen: list[list[dict]] = []

    @property
    def name(self):
        return "fake"

    def chat(self, messages, tools=None, options=None):
        self.seen.append(list(messages))
        i = min(self.calls, len(self.replies) - 1)
        self.calls += 1
        return self.replies[i]


class FakeMemory:
    def __init__(self):
        self.saved = []

    def remember(self, goal, reply):
        self.saved.append((goal, reply))
        return True

    def recall(self, q):
        return []


TARGET = str(WORKSPACE / "claim_test_out.md")

print("=" * 60)
print("1. 说「已创建」却没调工具 → 打回后真的写出来")
fake = FakeLLM(
    [
        {"content": "我已经在您的桌面上创建了文件 小满测试.md，内容如下……", "tool_calls": []},
        {
            "content": "",
            "tool_calls": [
                {
                    "id": "c1",
                    "name": "write_file",
                    "arguments": {"path": TARGET, "content": "真实写入的内容"},
                }
            ],
        },
        {"content": "已写入文件，内容已保存。", "tool_calls": []},
    ]
)
mem = FakeMemory()
ag = Agent(fake, verbose=False, use_rag=False, memory=mem)
out = ag.run("在我桌面写一个文档，文件名 小满测试.md")
p = Path(TARGET)
check("模型被追问了一次", fake.calls >= 3, f"calls={fake.calls}")
check("文件真的写出来了", p.exists(), TARGET)
check("内容正确", p.exists() and "真实写入的内容" in p.read_text(encoding="utf-8"))
check("回答里没有假话尾注", "补充说明" not in out, out[:80])
check("成功后写入记忆", len(mem.saved) == 1, str(len(mem.saved)))
check("被打回时给了明确指令", any("并没有调用 write_file" in (m.get("content") or "") for m in fake.seen[1]))
if p.exists():
    p.unlink()

print("=" * 60)
print("2. 打回了还是不写、继续嘴硬 → 明确更正，且不进记忆")
fake2 = FakeLLM([{"content": "已在您的桌面创建文件 abc.md，路径 C:\\Users\\zly\\Desktop\\abc.md", "tool_calls": []}])
mem2 = FakeMemory()
ag2 = Agent(fake2, verbose=False, use_rag=False, memory=mem2)
out2 = ag2.run("帮我写一个文档放桌面，叫 abc.md")
check("最后附上了事实更正", "补充说明" in out2 and "还没有生成" in out2, out2[-80:])
check("假话没有被存进长期记忆", len(mem2.saved) == 0, str(mem2.saved))

print("=" * 60)
print("3. 非写请求不误判")
cases = [
    ("刚才那个文件在哪", False),
    ("你上次写的文档说的什么", False),
    ("帮我写一个文档放桌面，叫 a.md", True),
    ("生成一份报告保存到文档目录", True),
    ("把这段内容整理成文件", True),
    ("数据库里有多少条记录", False),
]
for text, want in cases:
    got = Agent._goal_wants_write(text)
    check(f"「{text}」→ {'算写请求' if want else '不算'}", got == want, f"实际 {got}")

print("=" * 60)
print("4. 声称识别")
yes = [
    "我已经在您的桌面上创建了文件 小满测试.md",
    "已写入 C:\\Users\\zly\\Desktop\\x.md（6 字符）",
    "文件已成功保存",
    "文档已经生成好了",
]
no = [
    "这个文档里提到良率 97.6%",
    "需要我帮你写一份吗？告诉我文件名就行",
    "写入失败：目录不在可写范围",
]
for t in yes:
    check(f"识别为声称：{t[:18]}…", Agent._claims_write(t))
for t in no:
    check(f"不识别为声称：{t[:18]}…", not Agent._claims_write(t))

print("=" * 60)
print(f"通过 {ok} 项，失败 {fail} 项")
sys.exit(0 if fail == 0 else 1)
