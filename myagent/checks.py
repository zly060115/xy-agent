"""报告自检：用确定性代码核对模型产出的分析报告。

为什么要有这一层：
7B 模型会做三件让人难受的事——
1. 编数字（原文没有，但它写得很像）；
2. 漏章节（长文档只写了一部分，还写成"完整报告"）；
3. 结构缺项（少了"疑点与风险"，用户以为文档没问题）。

提示词管不住这三件事（同一类问题在提示注入上已经验证过：必须代码层拦），
所以这里用纯函数做三件可复现的核对：
- 数字溯源：报告「关键数据」里的每个数值，必须能在原文找到，或能由原文数字算出；
- 章节覆盖：原文的每个标题，报告里得提到；
- 结构完整性：报告该有的二级标题是否齐全。

设计原则：宁可漏报，不可误报。
误报会让用户对报告失去信任，比不检查更糟。所以：
- 数字比较先归一化（千分位 / 全角 / 百分号 / 万·亿换算 / 两数加减），
- 章节匹配按去噪后的标题核心词比对，不要求字面完全一致。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

# 报告必须有的二级标题（与 analyst.MERGE_TMPL 的模板一致）。
# 只卡三个决策必需的小节：概况决定读者看不看得懂，关键数据决定准不准，
# 结论决定能不能照做。其余小节（文档结构/逐部分要点/人员）属于辅助，
# 短文档本来就未必有，卡进去只会天天误报。
REQUIRED_SECTIONS = ("## 文档概况", "## 关键数据", "## 结论与要求")

# 原文里认作"标题"的行：markdown 标题、中文序号标题、常见公文小节
HEADING_RE = re.compile(
    r"^\s{0,3}(?:#{1,4}\s+(?P<md>.+?)"
    r"|(?:第[一二三四五六七八九十百]+[章节条]\s*[、.．]?\s*(?P<cn_ch>.+))"
    r"|[一二三四五六七八九十]+[、.．]\s*(?P<cn_num>.+?)"
    r"|\d+[.．、]\s*(?P<num>.+?)"
    r"|[（(][一二三四五六七八九十]+[)）]\s*(?P<paren>.+?))\s*$"
)

# 数值 token：允许千分位、小数点、全角
NUM_TOKEN_RE = re.compile(r"\d+(?:[.,，]\d+)*")

# 数值后紧跟的倍率词
SCALE_WORDS = {"万": 10_000, "亿": 100_000_000, "千": 1_000}

# 标题里这些词是噪声，比对核心词时去掉。
# 必须先把 3.2 / 1.1.1 这类多级编号整段剥掉，否则会残留 "2 进度安排" 对不上
HEADING_NOISE = re.compile(
    r"^(?:第[一二三四五六七八九十百]+[章节条]"
    r"|\d+(?:[.．、]\d+)*[.．、]?"
    r"|[一二三四五六七八九十]+[、.．]"
    r"|[（(][一二三四五六七八九十]+[)）])\s*"
)

# 覆盖度低于这个比例才提示"可能只分析了一部分"。
# 设成比例而非逐章报警，是因为摘要式报告本来就不会点每章的名字，
# 逐章比对会把正常报告全打成"不全面"。
COVERAGE_WARN_RATIO = 0.6


def _normalize(text: str) -> str:
    """全角转半角，统一各种连字符/空格，便于比对"""
    t = unicodedata.normalize("NFKC", text)
    return t.replace("，", ",").replace("．", ".")


def parse_number(raw: str) -> Decimal | None:
    """把 '1,500.25' 这样的文本转成 Decimal；失败返回 None"""
    s = raw.replace(",", "").replace("，", "").strip()
    try:
        return Decimal(s)
    except (InvalidOperation, ValueError):
        return None


def numbers_in(text: str) -> list[Decimal]:
    """抽出文本里所有数值（已归一化）"""
    out: list[Decimal] = []
    for m in NUM_TOKEN_RE.finditer(_normalize(text)):
        v = parse_number(m.group())
        if v is not None:
            out.append(v)
    return out


def _with_scales(text: str) -> set[Decimal]:
    """数值集合 + 带倍率词的展开值（1.5万 → 15000）"""
    norm = _normalize(text)
    values: set[Decimal] = set(numbers_in(norm))
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*(万|亿|千)", norm):
        base = parse_number(m.group(1))
        if base is not None:
            values.add(base * SCALE_WORDS[m.group(2)])
    return values


def _derived(values: set[Decimal], limit: int = 150) -> set[Decimal]:
    """由原文数字两两加减得到的结果。

    报告里出现"总计 2800"这种汇总值很常见，原文只有 1500 和 1300。
    不做这一步就会大面积误报。
    """
    base = sorted(values)[:limit]
    out: set[Decimal] = set()
    for i, a in enumerate(base):
        for b in base[i:]:
            out.add(a + b)
            out.add(abs(a - b))
    return out


def _heading_core(line: str) -> str:
    """取标题的核心文本用于比对"""
    text = HEADING_NOISE.sub("", line.strip().strip("#").strip())
    text = re.sub(r"[#*`_]", "", text)
    text = re.sub(r"[（(].*?[)）]", "", text)
    return text.strip(" ：:、.。")


@dataclass
class AuditResult:
    """三项核对的结果"""

    uncovered: list[str] = field(default_factory=list)      # 原文有、报告没提的章节
    unverified: list[str] = field(default_factory=list)     # 原文找不到出处的数值
    missing_sections: list[str] = field(default_factory=list)  # 报告缺的必备小节

    @property
    def ok(self) -> bool:
        return not (self.uncovered or self.unverified or self.missing_sections)

    def summary(self, limit: int = 5) -> list[str]:
        """转成人能读的提示行；有问题的才返回"""
        lines: list[str] = []
        if self.missing_sections:
            lines.append("报告结构缺： " + "、".join(self.missing_sections))
        if self.uncovered:
            head = "、".join(self.uncovered[:limit])
            more = f" 等 {len(self.uncovered)} 处" if len(self.uncovered) > limit else ""
            lines.append(f"原文这些章节报告里没提到： {head}{more}")
        if self.unverified:
            head = "、".join(self.unverified[:limit])
            more = f" 等 {len(self.unverified)} 处" if len(self.unverified) > limit else ""
            lines.append(f"这些数值在原文里找不到出处，请核对： {head}{more}")
        return lines


def _key_section(md: str, head: str) -> str:
    """取报告里某个二级标题下的正文"""
    lines = md.splitlines()
    try:
        start = next(i for i, l in enumerate(lines) if l.strip().startswith(head))
    except StopIteration:
        return ""
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines)
    )
    return "\n".join(lines[start + 1 : end])


def audit(report: str, source: str, *, max_uncovered: int = 12) -> AuditResult:
    """核对报告与原文，返回结构化结果（不修改报告）"""
    res = AuditResult()
    if not report or not source:
        return res

    # 1) 结构完整性
    norm_report = _normalize(report)
    res.missing_sections = [s for s in REQUIRED_SECTIONS if s not in norm_report]

    # 2) 数值溯源：只看「关键数据」小节，避免把正文里的普通数字也拖进来
    key_block = _key_section(report, "## 关键数据")
    if key_block:
        allowed = _with_scales(source)
        allowed |= _derived(allowed)
        seen: set[str] = set()
        for line in key_block.splitlines():
            if not line.strip().startswith("-"):
                continue
            for m in NUM_TOKEN_RE.finditer(_normalize(line)):
                val = parse_number(m.group())
                if val is None or val in allowed:
                    continue
                raw = m.group()
                if raw in seen:
                    continue
                seen.add(raw)
                res.unverified.append(raw)

    # 3) 章节覆盖：先算比例，只有明显只分析了一部分才提示
    norm_source = _normalize(source)
    covered_text = re.sub(r"\s+", "", norm_report)
    headings: list[str] = []
    for line in norm_source.splitlines():
        stripped = line.strip()
        if not stripped or HEADING_RE.match(line) is None:
            continue
        # 一级标题是文档名，不是章节，不计入覆盖度
        if stripped.startswith("# ") and not stripped.startswith("## "):
            continue
        core = re.sub(r"\s+", "", _heading_core(line))
        if len(core) < 2:
            continue
        headings.append(core)

    if len(headings) >= 3:
        hit = 0
        missing: list[str] = []
        for core in headings:
            # 标题核心词整体出现过就算覆盖，避免"3.2 进度安排"里的"进度安排"被拆散
            if core in covered_text:
                hit += 1
                continue
            parts = [p for p in re.split(r"[^\w\u4e00-\u9fff]+", core) if len(p) >= 2]
            if parts and all(p in covered_text for p in parts):
                hit += 1
                continue
            missing.append(core)
        if hit / len(headings) < COVERAGE_WARN_RATIO:
            res.uncovered = missing[:max_uncovered]

    return res


# ---------------- 回答里的数字抄写核对 ----------------
# 实测问题：计算器返回 3043，模型在回答里写成 3053 / 3003，除法结果却是对的。
# 这类错误提示词管不住（模型不是在编，是在"照抄时抄错"），只能事后再核一遍。

INT_NUM_RE = re.compile(r"(?<![\d.])\d{3,}(?![\d.])")


def _near_miss(value: str, candidates: dict[str, str]) -> str | None:
    """value 与某个候选位数相同、只差一位 → 返回该候选

    只比"差一位"，是因为抄错几乎都是单个数字写错或换位；
    差两位以上可能是另一个真实数值，误判会伤害可信度。
    """
    for other in candidates:
        if other == value or len(other) != len(value):
            continue
        diff = sum(1 for a, b in zip(other, value) if a != b)
        if diff == 1:
            return candidates[other]
    return None


def number_guard(answer: str, sources: list[str], max_notes: int = 3) -> list[str]:
    """核对回答里的整数，找出疑似抄错的，返回提示行

    只做提示，不改写答案——擅自替换数字会让错误变得更难发现。
    """
    if not answer:
        return []
    allowed_raw = set()
    for s in sources:
        if s:
            allowed_raw.update(INT_NUM_RE.findall(_normalize(s)))
    if not allowed_raw:
        return []
    allowed = {v: v for v in allowed_raw}

    notes: list[str] = []
    seen: set[str] = set()
    for m in INT_NUM_RE.findall(_normalize(answer)):
        if m in allowed_raw or m in seen:
            continue
        seen.add(m)
        hit = _near_miss(m, allowed)
        if hit:
            notes.append(f"> 注：回答里的 {m} 与工具算出的 {hit} 不一致，以 {hit} 为准。")
        if len(notes) >= max_notes:
            break
    return notes
