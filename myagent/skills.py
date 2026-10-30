"""Skill 加载：把「按需注入的提示词模块」接进小满。

为什么需要这一层：
GitHub 上的 Agent Skills（anthropics/skills 等）本质就是
「一个文件夹 + 一份 SKILL.md（YAML 头 + Markdown 指令）+ 可选脚本」。
它能被复用的前提，是 agent 有地方放、且知道什么时候该读哪一份。

本地跑 7B 的现实约束（8192 上下文）决定了不能全量注入：
把几千字的 skill 一股脑塞进 system prompt，模型会开始跑偏、漏工具调用。
所以这里只做一件事——按用户这句话**挑一份最相关的**注入，其余不加载。

目录约定：
    {DATA_DIR}/skills/<skill 名>/SKILL.md

与代码目录分离，clone 下来是纯代码，skill 和文档一样跟数据走。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# frontmatter 里认的字段
FIELD_RE = re.compile(r"^([A-Za-z_][\w-]*)\s*:\s*(.*)$")

# 命中阈值：查询里有多大比例的字词出现在 skill 描述中
MATCH_THRESHOLD = 0.30

# 关键词直接命中时给的分数（高于阈值，保证能过）
KEYWORD_SCORE = 0.9

# 单份 skill 注入上限（字符）。7B 上下文吃不下长文，超了截断并说明
MAX_SKILL_CHARS = 2200


@dataclass
class Skill:
    """一份已解析的 skill"""

    name: str
    description: str
    body: str
    path: Path
    keywords: tuple[str, ...] = ()

    @property
    def searchable(self) -> str:
        return " ".join((self.name, self.description, *self.keywords))


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """拆出 YAML 头与正文；没有头就整篇当正文"""
    text = text.replace("\r\n", "\n")
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end < 0:
        return {}, text
    head = text[3:end].strip("\n")
    body = text[end + 4 :].lstrip("\n")
    meta: dict[str, str] = {}
    for line in head.splitlines():
        m = FIELD_RE.match(line.strip())
        if m:
            meta[m.group(1).lower()] = m.group(2).strip().strip("'\"")
    return meta, body


def _grams(text: str) -> set[str]:
    """中文按二元字切，英文按词切——和知识库检索用的是同一套思路"""
    text = text.lower()
    cjk = re.findall(r"[\u4e00-\u9fff]+", text)
    grams: set[str] = set()
    for run in cjk:
        grams.update(run[i : i + 2] for i in range(len(run) - 1))
        if len(run) == 1:
            grams.add(run)
    grams.update(w for w in re.findall(r"[a-z0-9_]{3,}", text))
    return grams


class SkillStore:
    """扫描目录、按查询挑选 skill"""

    def __init__(self, root: Path):
        self.root = Path(root)
        self._skills: list[Skill] | None = None

    def load(self, refresh: bool = False) -> list[Skill]:
        """扫描 root 下所有 */SKILL.md；结果缓存，refresh=True 重新扫"""
        if self._skills is not None and not refresh:
            return self._skills
        skills: list[Skill] = []
        if self.root.is_dir():
            for md in sorted(self.root.glob("*/SKILL.md")):
                try:
                    meta, body = _parse_frontmatter(md.read_text(encoding="utf-8"))
                except OSError:
                    continue
                if not body.strip():
                    continue
                name = meta.get("name") or md.parent.name
                desc = meta.get("description", "")
                kws = tuple(
                    k.strip()
                    for k in re.split(r"[,，、;；]", meta.get("keywords", ""))
                    if k.strip()
                )
                skills.append(
                    Skill(name=name, description=desc, body=body.strip(), path=md, keywords=kws)
                )
        self._skills = skills
        return skills

    def match(self, query: str, threshold: float = MATCH_THRESHOLD) -> list[Skill]:
        """返回与查询相关的 skill，按相关度降序，最多 1 份。

        只取 1 份是有意的：7B 同时跟随两份指令时会串味，
        实测表现为把 A skill 的步骤套到 B 任务上。

        打分走两条路，取高者：
        - 关键词命中：用户原话里直接出现 skill 声明过的词（合同、风险…），最可靠；
        - 二元字重叠：应付换说法的情况，但口语噪声（"帮我""这份"）会稀释比例，
          所以阈值不能设高。
        """
        q = _grams(query)
        if not q:
            return []
        q_text = query.lower()
        scored: list[tuple[float, Skill]] = []
        for skill in self.load():
            target = _grams(skill.searchable)
            if not target:
                continue
            overlap = len(q & target) / len(q)
            # 关键词直接命中给高分：这些词是 skill 作者明确声明过的触发词
            if any(kw.lower() in q_text for kw in skill.keywords if len(kw) >= 2):
                overlap = max(overlap, KEYWORD_SCORE)
            if overlap >= threshold:
                scored.append((overlap, skill))
        scored.sort(key=lambda x: -x[0])
        return [s for _, s in scored[:1]]

    def get(self, name: str) -> Skill | None:
        for skill in self.load():
            if skill.name == name:
                return skill
        return None

    def render(self, skill: Skill) -> str:
        """拼成注入 system prompt 的文本块"""
        body = skill.body
        note = ""
        if len(body) > MAX_SKILL_CHARS:
            body = body[:MAX_SKILL_CHARS].rstrip() + "\n…（后文略）"
            note = "\n（这份技能较长，已截断；需要更多细节时按需再问）"
        return f"## 当前任务适用的技能：{skill.name}\n{body}{note}"
