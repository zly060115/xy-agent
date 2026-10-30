"""
文档分析：逐节覆盖式通读，产出结构化要点。

为什么不用"整篇一次概括"：
7B 模型的注意力覆盖不住整篇长文，整篇送入时它通常只抓开头几段就收尾，
输出看着像样，实际丢了大量内容。所以改成"分节抽取 → 完整组装"。

流程：
1. 识别文档结构（markdown 标题 / 一、二、三 / 第X章 / 1.1 这类编号）
2. 按章节边界切段，每段不超过 SEG_SIZE 字，尽量不跨章
3. 逐段"抽取事实"（不是概括），每条保留原文的数字、名称、术语
4. 把各段抽取结果 + 章节骨架交给模型，组装成完整报告

关键约束写在模板里：
- 不许跳过章节、不许合并章节
- 宁可条目多，不许省略
- 只用原文有的内容，不许补

短文档（<= TINY_LIMIT）跳过抽取，直接把原文交给组装步骤，省一次调用。
"""

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .checks import audit

TINY_LIMIT = 600        # 短于这个字数，跳过逐段抽取
SEG_SIZE = 1200         # 每段上限字数（越小模型覆盖越细，但调用次数变多）
MAX_TOTAL = 200_000     # 总字数上限，超了就明确告知截断
MAX_SEGMENTS = 30       # 段数上限，防止超大文档跑太久
MERGE_INPUT_LIMIT = 2600  # 喂给合并步骤的要点总长上限（防超上下文）

# 文档内嵌图片的章节标题，与 safe_reader 生成的保持一致
IMAGE_HEAD = "## 文档内嵌图片与图表内容"

# 采样参数：抽取任务要稳定，不要发挥
OPT_EXTRACT = {"temperature": 0.1, "num_predict": 1600}
OPT_MERGE = {"temperature": 0.1, "num_predict": 3000}

# 顶层标题：只按这些切段（一、二、三 / 第X章 / markdown 井号）
# 次级编号（1. 2. (1)）不切段，留在正文里给模型看，
# 否则选做、小步骤这类条目会被误当成章节，把内容切碎。
HEAD_RE = re.compile(
    r"^\s*(?:"
    r"#{1,6}\s*\S"                                      # # markdown 标题
    r"|第[一二三四五六七八九十百零\d]+\s*[章节部分讲篇]"    # 第X章
    r"|[一二三四五六七八九十]+\s*[、.．]\s*\S"            # 一、
    r")"
)

EXTRACT_TMPL = """下面是一份文档中的一段内容，需要你把里面的信息**逐条抽出来**。

要求：
- 每条只写一个事实，不超过 60 字
- 保留原文里的数字、金额、日期、名称、术语，不要换说法
- 有编号或小标题的，把编号/标题带上
- 至少 5 条（这段确实没那么多内容时除外）
- 不要写"本段介绍了""作者指出"这类空话
- 代码、配置、XML 片段不要照抄，用一句话说明这段代码在做什么
- 不要推测，不要补充原文没有的内容

先看一个示范（示例内容与本任务无关，只学写法，绝对不要把示例里的事实写进结果）：

原文：
---
二、设备巡检
车间每班需对 12 台注塑机点检一次，记录油温（不超过 65℃）与压力。
点检由操作工完成，班组长复核。
异常设备须在 30 分钟内报修，维修后复检签字。
---
输出：
- 二、设备巡检：每班对 12 台注塑机点检一次
- 点检需记录油温，上限 65℃，同时记录压力
- 点检由操作工完成，班组长复核
- 异常设备须在 30 分钟内报修
- 维修完成后需复检并签字

现在处理下面的真实内容，只输出列表，每行以 "- " 开头，不要任何开头结尾说明。

{where}
原文：
---
{text}
---
"""

MERGE_TMPL = """下面是一份文档的章节结构和逐段抽取的内容。
请整理成一份完整的分析报告，输出 markdown。

严格使用下面这些标题，不要增加、删除、改名：

## 文档概况
（一句话说清这是什么文档、主题是什么）

## 文档结构
（按顺序列出文档的章节，每行一条）

## 逐部分要点
（按文档顺序，每个章节一个小标题，下面列该章要点）

## 关键数据
（列出文档里所有数字类信息：金额、数量、比例、日期、期限、编号、指标数值）

## 涉及人员与部门
（只写人名、部门、角色、职务）

## 结论与要求
（文档明确要求的动作、交付物、期限、结论）

## 疑点与风险
（矛盾、遗漏、需要注意的地方）

写法示范（只学格式，X、Y 是占位符，不要把示例里的事实写进报告）：

## 文档概况
本文是某门店的月度盘点通知，规定了盘点时间、人员分工与差异处理方式。

## 逐部分要点
### 一、盘点安排
- 盘点定于每月 X 日闭店后进行，由店长带队
- 收银区安排 2 人、库房安排 3 人
- 差异超过 Y 元的须当日上报区域经理

反例（这样写等于没写，绝对不许出现）：
- 本部分介绍了盘点的工作安排，强调了规范管理的重要性
- 该章节说明了相关流程和注意事项

写不出内容的区块统一写"无"，不要留空。

硬规则：
- 只用下面给出的内容，不许编造
- 不许跳过或合并章节，每章至少两条
- 宁可条目多，不许省略
- 每条保留原文的关键词与数字

文档：{name}（{chars} 字，分 {total} 段）

章节结构：
{structure}

逐段抽取内容：
{notes}
"""


# 读取器插入的覆盖范围说明（图片数、无文字页数）
NOTICE_RE = re.compile(r"\[提示：[^\]]*\]")

# "标签：值"形式的行，用于给"关键数据"做兜底
LABELED_RE = re.compile(r"^-\s*([^：:\n]{2,24})[：:]\s*(.+)$")

# 带单位的数字，用于从原文自动扫出关键数据
NUM_RE = re.compile(
    r"\d[\d,，\.]*\s*(?:万元|亿元|元|%|％|倍|天|个工作日|日|周|年|月|小时|分钟|秒"
    r"|次|人|户|台|套|份|页|GB|MB|KB)"
)

# Few-shot 示例里的专属词。报告里出现这些词、而原文根本没有 → 是模型抄的示范内容
LEAK_TOKENS = (
    "注塑机", "巡检", "点检", "班组长", "油温", "复检",
    "门店", "盘点", "收银区", "区域经理",
)


class DocumentAnalyst:
    def __init__(self, reader, llm):
        self.reader = reader
        self.llm = llm

    # ---------- 对外接口 ----------

    def analyze(self, path: str, progress=None) -> str:
        """读文件 → 分析 → 返回结构化报告（markdown）

        progress: 可选的进度回调，签名为 fn(text: str)
        """
        p = Path(path)
        if progress:
            progress("正在读取文档（含图片识别）…")
        try:
            text = self.reader.read_text(str(p), progress)
        except TypeError:
            # 兼容不接受 progress 的读取器实现
            text = self.reader.read_text(str(p))

        if not text or not text.strip():
            return "这个文件里没有可提取的文字内容（可能是扫描件或空文档）。"

        # 图片/空白页这类"没法分析的部分"由代码记录并强制写进报告，
        # 不指望模型自己转述——7B 经常漏。
        notices = NOTICE_RE.findall(text)
        if notices:
            text = NOTICE_RE.sub("", text).strip()

        notice = ""
        if len(text) > MAX_TOTAL:
            notice = (
                f"\n\n> 注意：文档共 {len(text)} 字，超出单次分析上限，"
                f"以下只覆盖前 {MAX_TOTAL} 字。\n"
            )
            text = text[:MAX_TOTAL]

        raw_len = len(text)
        chunks = self._chunks(text)
        headings = [t for t in (c[0] for c in chunks) if t]

        if len(text) <= TINY_LIMIT:
            # 短文档：跳过抽取，原文直接交给组装，省一次调用
            notes = [("", text)]
            body = self._merge(p.name, raw_len, notes, 1)
        else:
            if progress:
                progress(f"已识别 {len(chunks)} 个部分，正在逐段抽取要点…")
            notes = self._extract_all(chunks, progress)

            if not notes:
                return "分析失败：模型没有返回任何内容。"

            if progress:
                progress("正在组装完整报告…")
            body = self._merge(p.name, raw_len, notes, len(chunks))

        body = self._strip_echo(body)
        body = self._drop_leak(body, text)
        # 逐节内容由代码拼装：模型组装时会漏章节，抽取结果本身是准的
        body = self._inject_sections(body, notes)
        if len(text) <= TINY_LIMIT:
            # 短文档不覆盖模型的组织，但图片那一节要补回来
            body = self._append_image_section(body, text)
        # 文档结构也由代码填：识别到的标题一定比模型复述的全
        body = self._fill_structure(body, headings)
        body = self._tidy(body)
        body = self._fix_empty_structure(body)
        body = self._fill_numbers(body, text)
        body = self._fill_people(body)
        body = self._fill_empty(body)
        # 代码层自检：数字出处、章节覆盖、结构完整
        # 模型编数字 / 漏章节靠提示词管不住，这里用确定性规则核对，有问题如实写进报告
        body = self._self_check(body, text)

        # 覆盖范围由代码强制附加，模型漏了也不影响
        if notices:
            hints = "\n".join(f"> - {n.strip('[]')}" for n in notices)
            body += f"\n\n## 覆盖范围\n{hints}"

        covered = f"，分 {len(chunks)} 段逐段覆盖" if len(chunks) > 1 else ""
        head = f"，识别到 {len(headings)} 个章节" if headings else ""
        return (
            f"{body}{notice}\n\n---\n"
            f"来源：`{p.name}`（{raw_len} 字{covered}{head}）"
        )

    @staticmethod
    def _append_image_section(md: str, text: str) -> str:
        """短文档：把图片内容补进「逐部分要点」末尾

        短文档由模型自由组织，实测它会把整节图片内容吞掉；
        而图片里的数字往往是正文没写、只存在于截图里的关键信息，必须补回。
        """
        idx = text.find(IMAGE_HEAD)
        if idx < 0:
            return md

        rest = text[idx:].strip()
        body = rest.split("\n", 1)[1].strip() if "\n" in rest else ""
        if not body:
            return md

        # 报告整体按 Markdown 渲染，单换行会被折叠成空格；
        # 图片原文靠换行区分行列，行尾补两个空格强制换行
        body = "\n".join(
            (line + "  ") if line.strip() else "" for line in body.splitlines()
        )

        block = f"### 文档内嵌图片与图表内容\n{body}"
        lines = md.splitlines()
        try:
            start = lines.index("## 逐部分要点")
        except ValueError:
            return f"{md}\n\n## 逐部分要点\n\n{block}"

        end = next(
            (i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")),
            len(lines),
        )
        lines[end:end] = ["", block, ""]
        return "\n".join(lines)

    @staticmethod
    def _fill_structure(md: str, headings: list[str]) -> str:
        """「文档结构」由代码按识别到的标题填死

        模型写这一节时经常敷衍：要么"未识别到章节"，要么只列前两三个。
        标题本来就是代码扫出来的（_headings），一定比模型复述准。
        """
        if not headings:
            return md

        lines = md.splitlines()
        try:
            start = lines.index("## 文档结构")
        except ValueError:
            return md

        end = next(
            (i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")),
            len(lines),
        )

        # 去掉重复标题和"（开头）"这类占位，保留顺序
        placeholders = {"（开头）", "(开头)", "开头", "（正文）", "(正文)", "正文"}
        seen: list[str] = []
        for h in headings[:40]:
            h = h.strip()
            if h and h not in seen and h not in placeholders:
                seen.append(h)
        if not seen:
            return md

        lines[start + 1 : end] = [""] + [f"- {h}" for h in seen] + [""]
        return "\n".join(lines)

    @staticmethod
    def _inject_sections(md: str, notes: list[tuple[str, str]]) -> str:
        """用逐段抽取结果直接拼出「逐部分要点」。

        7B 在组装时会漏掉整节（实测漏过整个"产品定价策略"），
        而抽取结果本身是逐段做出来的、不会漏，所以这一节交给代码拼。
        """
        if len(notes) <= 1:
            return md  # 单段文档，原文就是全部内容，保留模型的组织

        sections = "\n\n".join(
            f"### {DocumentAnalyst._sec_label(title, i)}\n"
            f"{DocumentAnalyst._clean_note(title, body)}"
            for i, (title, body) in enumerate(notes, 1)
        )

        lines = md.splitlines()
        try:
            start = lines.index("## 逐部分要点")
        except ValueError:
            return f"{md}\n\n## 逐部分要点\n\n{sections}"

        end = next(
            (i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")),
            len(lines),
        )
        lines[start + 1 : end] = ["", sections, ""]
        return "\n".join(lines)

    @staticmethod
    def _sec_label(title: str, i: int) -> str:
        """「逐部分要点」里的小标题"""
        t = (title or "").strip()
        if re.fullmatch(r"[（(]开头[）)]", t):
            return "开头部分"
        return t or f"第 {i} 段"

    @staticmethod
    def _clean_note(title: str, note: str) -> str:
        """清洗抽取结果：双列表符、章节标题重复成条目、"X/Y 章节描述"这类元话"""
        bare_title = re.sub(r"[（(]\d+/\d+[）)]$", "", title).strip()
        bare_title = re.sub(r"^[一二三四五六七八九十\d]+[、.．]\s*", "", bare_title)
        bare_title = bare_title.strip("：: 。.")
        # 原始标题也要留着：模型会把"一、经营概况：xxx"整句抄成条目
        raw_title = re.sub(r"[（(]\d+/\d+[）)]$", "", title).strip()

        out: list[str] = []
        for line in note.splitlines():
            s = line.strip()
            if not s:
                continue
            s = re.sub(r"^-\s*-\s*", "- ", s)          # - - xxx → - xxx
            if not s.startswith("- "):
                out.append(s)
                continue
            item = s[2:].strip()
            if re.match(r"^\d+/\d+\s*章节", item):      # "2/2 章节描述…" 元话
                continue
            # 条目开头重复章节目录（模型爱写"一、经营概况：本季营收…"）→ 剥掉前缀
            for pre in (raw_title, bare_title):
                if pre and item.startswith(pre):
                    item = item[len(pre) :].lstrip("：: ，,。. ")
                    break
            plain = item.strip("：: 。.")
            if bare_title and (
                plain in bare_title
                or (bare_title in plain and len(plain) <= len(bare_title) + 16)
            ):
                continue                                # 章节标题本身不入条目
            out.append("- " + item)
        return "\n".join(out)

    @classmethod
    def _strip_echo(cls, md: str) -> str:
        """7B 常把模板里的说明文字当成内容抄进输出。

        按模板说明行的前缀比对删除（模型会把两行说明合并成一行，
        精确匹配拦不住）。这比在提示词里反复强调"不要输出说明"可靠。
        """
        prefixes = [
            line.strip()[:12]
            for line in MERGE_TMPL.splitlines()
            if line.strip()
            and not line.strip().startswith(("#", "-", ">", "{"))
            and "{" not in line
            and len(line.strip()) >= 12
        ]
        kept = [
            l for l in md.splitlines() if not any(l.strip().startswith(p) for p in prefixes)
        ]
        return "\n".join(kept)

    @staticmethod
    def _drop_leak(md: str, source: str) -> str:
        """删掉示例里混进正文的行。

        Few-shot 的代价：7B 偶尔会把示范内容当成文档内容抄进来。
        判定依据是"这词只出现在示例里、原文根本没有"，所以原文若真讲门店、盘点，
        这些行不会被误删。
        """
        out = []
        for line in md.splitlines():
            # 模板说明、示范标记一律不要
            if "写法示范" in line or "反例（" in line or "示例内容" in line:
                continue
            hit = any(tok in line and tok not in source for tok in LEAK_TOKENS)
            if hit and line.lstrip().startswith("-"):
                continue
            if hit and line.lstrip().startswith("###"):
                continue
            out.append(line)
        return "\n".join(out)

    @staticmethod
    def _fill_empty(md: str) -> str:
        """模型偶尔只输出标题不写内容，补一个"无"，避免看起来像漏了"""
        required = ["## 关键数据", "## 涉及人员与部门", "## 结论与要求", "## 疑点与风险"]
        lines = md.splitlines()
        for head in required:
            try:
                start = lines.index(head)
            except ValueError:
                continue
            end = next(
                (i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")),
                len(lines),
            )
            if not any(l.strip() for l in lines[start + 1 : end]):
                # 末尾补空行：否则下一个 "## " 紧贴列表项，部分渲染器会当成列表的一部分
                lines[start + 1 : end] = ["", "- 无", ""]
        return "\n".join(lines)

    @staticmethod
    def _self_check(md: str, text: str) -> str:
        """对报告做确定性核对，有疑点就如实追加「自检」一节。

        写进报告而不是偷偷改掉，是因为用户需要知道哪几个数拿不准——
        悄悄修正会让整份报告看起来都可信，反而更危险。
        """
        try:
            result = audit(md, text)
        except Exception:
            return md  # 核对本身出问题，不能影响报告交付
        lines = result.summary()
        if not lines:
            return md
        block = "\n".join(f"- {l}" for l in lines)
        return f"{md}\n\n## 自检\n{block}\n"

    @staticmethod
    def _fix_empty_structure(md: str) -> str:
        """章节识别不出来时，模型会写"无"，读起来像文档没结构，换个说明"""
        lines = md.splitlines()
        try:
            start = lines.index("## 文档结构")
        except ValueError:
            return md
        end = next(
            (i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")),
            len(lines),
        )
        block = [l.strip() for l in lines[start + 1 : end] if l.strip()]
        empty_marks = ("无", "- 无", "- 无。", "无。")
        if block and all(l in empty_marks for l in block):
            lines[start + 1 : end] = ["", "（未识别到章节，已按段落逐段分析）", ""]
        return "\n".join(lines)

    @classmethod
    def _fill_numbers(cls, md: str, text: str = "") -> str:
        """模型常把编号、日期这类数字漏在「关键数据」外。

        如果模型给出的是"无"，就从全文的"标签：值"行里做一次代码兜底，
        宁可多抓几条，也不让用户看到假的"无"。
        """
        lines = md.splitlines()
        try:
            start = lines.index("## 关键数据")
        except ValueError:
            return md

        end = next(
            (i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")),
            len(lines),
        )
        block = [l.strip() for l in lines[start + 1 : end] if l.strip()]
        real_items = [l for l in block if l.startswith("- ") and l not in ("- 无", "- 无。")]
        if real_items:
            return md  # 模型已经给出内容，不覆盖

        found: list[str] = []
        for line in lines:
            s = line.strip()
            if not s.startswith("- "):
                continue
            m = LABELED_RE.match(s)
            if not m:
                continue
            label, value = m.group(1).strip(), m.group(2).strip()
            if not re.search(r"\d", value) or len(value) > 20:
                continue
            item = f"- {label}：{value}"
            if item not in found and label not in ("选做", "注意", "提示"):
                found.append(item)
            if len(found) >= 10:
                break

        if not found:
            # 模型没给，就从原文直接扫带单位的数字，附上上下文供核对
            auto = cls._auto_numbers(text)
            if auto:
                lines[start + 1 : end] = [""] + auto + [""]
            return "\n".join(lines)

        lines[start + 1 : end] = [""] + found + [""]
        return "\n".join(lines)

    @staticmethod
    def _fill_people(md: str) -> str:
        """人员区块为空时，从全文的"标签：值"行里找带角色关键词的补上"""
        lines = md.splitlines()
        try:
            start = lines.index("## 涉及人员与部门")
        except ValueError:
            return md
        end = next(
            (i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")),
            len(lines),
        )
        block = [l.strip() for l in lines[start + 1 : end] if l.strip()]
        if block and any(l.startswith("- ") and l not in ("- 无", "- 无。") for l in block):
            return md

        role_words = ("教师", "老师", "负责", "经理", "主任", "联系人", "工程师", "同学", "学生", "主管")
        found: list[str] = []
        for line in lines:
            s = line.strip()
            if not s.startswith("- ") or "：" not in s:
                continue
            m = LABELED_RE.match(s)
            if not m:
                continue
            label, value = m.group(1).strip(), m.group(2).strip()
            if len(value) > 12:
                continue
            if any(w in label or w in value for w in role_words):
                item = f"- {value}：{label}"
                if item not in found:
                    found.append(item)
        if found:
            lines[start + 1 : end] = [""] + found[:8] + [""]
        return "\n".join(lines)

    @staticmethod
    def _auto_numbers(text: str, limit: int = 10) -> list[str]:
        """从原文扫出带单位的数字，带一小段上下文，避免只看到孤零零的数值"""
        out: list[str] = []
        for m in NUM_RE.finditer(text):
            s = max(0, m.start() - 14)
            e = min(len(text), m.end() + 4)
            snippet = " ".join(text[s:e].split())
            item = f"- …{snippet}…"
            if item not in out:
                out.append(item)
            if len(out) >= limit:
                break
        return out

    @staticmethod
    def _tidy(md: str) -> str:
        """清理模型的小毛病：有实际条目时多余的"- 无"、连续空行"""
        blocks: list[list[str]] = []
        cur: list[str] = []
        for line in md.splitlines():
            if line.startswith("## ") and cur:
                blocks.append(cur)
                cur = [line]
            else:
                cur.append(line)
        blocks.append(cur)

        out: list[str] = []
        for block in blocks:
            items = [l.strip() for l in block if l.strip().startswith("- ")]
            has_real = any(i not in ("- 无", "- 无。", "- 无 ") for i in items)
            for line in block:
                if has_real and line.strip() in ("- 无", "- 无。", "- 无 "):
                    continue
                out.append(line)

        # 压缩连续空行 + 去掉重复条目
        # （7B 在长输出末尾会陷入重复循环，实测同一句吐了 37 遍）
        cleaned: list[str] = []
        seen: set[str] = set()
        for line in out:
            s = line.strip()
            if not s:
                if cleaned and not cleaned[-1].strip():
                    continue
                cleaned.append(line)
                continue
            if s.startswith("- "):
                if s in seen:
                    continue
                seen.add(s)
            cleaned.append(line)
        return "\n".join(cleaned).strip()

    # ---------- 内部实现 ----------

    def _ask(self, prompt: str, options: dict | None = None) -> str:
        """单次调用模型，只要文本回复"""
        resp = self.llm.chat([{"role": "user", "content": prompt}], options=options)
        return (resp.get("content") or "").strip()

    def _extract_all(self, chunks: list[tuple[str, str]], progress=None) -> list[tuple[str, str]]:
        """并发抽取各段要点，结果按原顺序返回。

        串行跑 14 段要六七分钟，本地 Ollama 并发 3 路能压到一分多钟，
        实测 3 段从 20 秒降到 11 秒。
        """
        def one(idx: int, title: str, seg: str):
            where = f"这一段所属章节：{title}" if title else f"这是第 {idx} 段"
            return idx, title, self._ask(
                EXTRACT_TMPL.format(where=where, text=seg), OPT_EXTRACT
            )

        results: dict[int, tuple[str, str]] = {}
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = {
                pool.submit(one, i, title, seg): i
                for i, (title, seg) in enumerate(chunks, 1)
            }
            for done, fut in enumerate(as_completed(futures), 1):
                try:
                    idx, title, note = fut.result()
                except Exception:
                    continue
                results[idx] = (title, note)
                if progress:
                    progress(f"逐段抽取 {done}/{len(chunks)} 完成…")

        return [
            (title or f"第 {idx} 段", note)
            for idx, (title, note) in sorted(results.items())
            if note
        ]

    def _merge(self, name: str, chars: int, notes: list[tuple[str, str]], total: int) -> str:
        """notes 是 [(章节名, 内容)]，短文档时章节名为空、内容就是原文"""
        # 去掉分片标记（1/2）并按顺序去重，避免结构里出现内部实现细节
        titles: list[str] = []
        for t, _ in notes:
            if not t:
                continue
            clean = re.sub(r"（\d+/\d+）\s*$", "", t).strip()
            if clean and clean not in titles:
                titles.append(clean)
        structure = "\n".join(f"- {t}" for t in titles) or "无"

        blocks = []
        for title, body in notes:
            if title:
                blocks.append(f"[{title}]\n{body}")
            else:
                blocks.append(f"[原文]\n{body}")

        joined = "\n\n".join(blocks)
        # 段数多时要点会超出模型上下文（8192 token ≈ 五千汉字），
        # 静默截断会丢数字和人员，所以先按优先级压缩
        if len(joined) > MERGE_INPUT_LIMIT:
            joined = self._compact_notes(notes, MERGE_INPUT_LIMIT)

        return self._ask(
            MERGE_TMPL.format(
                name=name,
                chars=chars,
                total=total,
                structure=structure,
                notes=joined,
            ),
            OPT_MERGE,
        )

    @staticmethod
    def _compact_notes(notes: list[tuple[str, str]], limit: int) -> str:
        """压缩喂给合并步骤的要点：优先保留带数字、带标签的行"""
        primary: list[str] = []
        secondary: list[str] = []
        for _title, body in notes:
            lines = [l.strip() for l in body.splitlines() if l.strip().startswith("- ")]
            primary.extend(l for l in lines if re.search(r"\d", l) or "：" in l)
            secondary.extend(lines)

        picked: list[str] = []
        seen: set[str] = set()
        size = 0
        for line in primary + secondary:
            if line in seen:
                continue
            seen.add(line)
            picked.append(line)
            size += len(line) + 1
            if size >= limit:
                break
        return "\n".join(picked)

    # ---------- 结构识别与切分 ----------

    @staticmethod
    def _headings(text: str) -> list[tuple[int, str]]:
        """返回 [(字符偏移, 标题文本)]，只认短行且不以句号结尾的"""
        marks: list[tuple[int, str]] = []
        offset = 0
        for line in text.splitlines(keepends=True):
            s = line.strip()
            if (
                s
                and len(s) <= 40
                and not s.endswith(("。", "；", ";", "！", "？", ".", "…"))
                and HEAD_RE.match(s)
            ):
                marks.append((offset, s.lstrip("#").strip()))
            offset += len(line)
        return marks

    def _chunks(self, text: str) -> list[tuple[str, str]]:
        """按章节切段；识别不到结构就按段落切"""
        marks = self._headings(text)

        if len(marks) < 2:
            return [("", seg) for seg in self._split(text, SEG_SIZE)]

        chunks: list[tuple[str, str]] = []

        # 第一个标题之前的内容
        if marks[0][0] > 0:
            pre = text[: marks[0][0]].strip()
            if len(pre) > 60:
                chunks.extend(("（开头）", seg) for seg in self._split(pre, SEG_SIZE))

        for i, (pos, title) in enumerate(marks):
            end = marks[i + 1][0] if i + 1 < len(marks) else len(text)
            body = text[pos:end].strip()
            if not body:
                continue
            if len(body) <= SEG_SIZE:
                chunks.append((title, body))
            else:
                subs = self._split(body, SEG_SIZE)
                # 章节名可能很长（文件名带说明），分片时截短，否则 11 段标题一排看着很乱
                short = title if len(title) <= 16 else title[:16] + "…"
                for j, sub in enumerate(subs, 1):
                    chunks.append((f"{short}（{j}/{len(subs)}）", sub))

        # 段数上限保护：超了就合并超出的尾部
        if len(chunks) > MAX_SEGMENTS:
            head = chunks[: MAX_SEGMENTS - 1]
            tail = "\n\n".join(c[1] for c in chunks[MAX_SEGMENTS - 1 :])
            head.append(("（后续部分合并）", tail))
            chunks = head

        return chunks

    @staticmethod
    def _split(text: str, size: int) -> list[str]:
        """按段落边界切，尽量不把一句话劈开"""
        paragraphs = [p for p in text.split("\n\n") if p.strip()]
        segments, buf = [], ""
        for para in paragraphs:
            if len(buf) + len(para) <= size:
                buf += para + "\n\n"
            else:
                if buf.strip():
                    segments.append(buf.strip())
                # 单段就超长，只能硬切
                while len(para) > size:
                    segments.append(para[:size])
                    para = para[size:]
                buf = para + "\n\n"
        if buf.strip():
            segments.append(buf.strip())
        return segments or [text]
