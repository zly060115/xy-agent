"""checks.py 单元测试：每条都带"不该误报"的反例。

误报比漏报更伤——用户一旦发现报告里的"存疑"是瞎报的，
整份报告的可信度就没了。所以这里一半用例是反例。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from myagent.checks import audit, numbers_in, parse_number  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}  {detail}")


def base_report(key_lines, extra=""):
    body = "\n".join(f"- {x}" for x in key_lines)
    return f"""## 文档概况
这是一份测试报告。{extra}

## 关键数据
{body}

## 结论与要求
- 按要求执行。

## 疑点与风险
- 无
"""


print("=== 解析层 ===")
check("parse_number 千分位", parse_number("1,500") == 1500, parse_number("1,500"))
check("parse_number 全角逗号", parse_number("1，500") == 1500, parse_number("1，500"))
check("parse_number 小数", parse_number("98.1") == parse_number("98.1"))
check("numbers_in 混合", len(numbers_in("甲厂 1500 台，合格率 98.1%")) == 2,
      numbers_in("甲厂 1500 台，合格率 98.1%"))

print("=== 数值溯源：该抓的 ===")
src = "甲厂 10 月产量 1500 台，合格率 98.1%。乙厂产量 1300 台，合格率 98.9%。"
r = audit(base_report(["甲厂产量：9999 台"]), src)
check("编造的数字被抓", r.unverified == ["9999"], r.unverified)

print("=== 数值溯源：不该误报的 ===")
r = audit(base_report(["甲厂产量：1,500 台"]), src)
check("千分位写法不误报", r.unverified == [], r.unverified)

r = audit(base_report(["甲厂合格率：98.1％"]), src)
check("全角百分号不误报", r.unverified == [], r.unverified)

r = audit(base_report(["两厂合计：2800 台"]), src)
check("两数相加的汇总值不误报", r.unverified == [], r.unverified)

r = audit(base_report(["两厂差额：200 台"]), src)
check("两数相减的差值不误报", r.unverified == [], r.unverified)

src2 = "合同总价 1.47 万元，工期 30 天。"
r = audit(base_report(["合同总价：14700 元", "工期：30 天"]), src2)
check("万元换算不误报", r.unverified == [], r.unverified)

r = audit(base_report(["工期：30 个工作日"]), src2)
check("同数值不同单位（不做单位比对）不误报", r.unverified == [], r.unverified)

print("=== 章节覆盖：该抓的 ===")
src3 = """# 项目立项书
## 一、项目背景
背景内容。
## 二、预算
预算 26 万元。
## 三、风险评估
风险内容。
"""
r = audit(base_report(["预算：26 万元"]), src3)
check("只覆盖 1/3 时报不全", bool(r.uncovered), r.uncovered)
check("漏掉的章节被点名", "风险评估" in r.uncovered, r.uncovered)
check("一级标题（文档名）不计入章节", "项目立项书" not in r.uncovered, r.uncovered)

print("=== 章节覆盖：不该误报的 ===")
report_two = base_report(["预算：26 万元"], extra="项目背景见上文。")
r = audit(report_two, src3)
check("覆盖 2/3 不报警", r.uncovered == [], r.uncovered)

report_full = base_report(["预算：26 万元"], extra="项目背景、预算、风险评估都已涉及。")
r = audit(report_full, src3)
check("全覆盖不报警", r.uncovered == [], r.uncovered)

src4 = """3.2 进度安排
内容。
1. 总则
内容。
2. 分工安排
内容。
"""
r = audit(base_report(["进度：30 天"], extra="进度安排、总则、分工安排均已说明。"), src4)
check("3.2 这类多级编号去噪正确", r.uncovered == [], r.uncovered)

print("=== 结构完整性 ===")
r = audit("## 文档概况\n只是概况。\n", src)
check("缺关键数据被抓", "## 关键数据" in r.missing_sections, r.missing_sections)
check("缺结论被抓", "## 结论与要求" in r.missing_sections, r.missing_sections)

print("=== 回答数字抄写核对 ===")
from myagent.checks import number_guard  # noqa: E402

src_calc = "3580*0.85 = 3043"
check("抄错一位被抓", number_guard("打8.5折后是 3053 元", [src_calc]) != [],
      number_guard("打8.5折后是 3053 元", [src_calc]))
check("换位抄错被抓", number_guard("打8.5折后是 3003 元", [src_calc]) != [],
      number_guard("打8.5折后是 3003 元", [src_calc]))
check("正确数字不提示", number_guard("打8.5折后是 3043 元", [src_calc]) == [],
      number_guard("打8.5折后是 3043 元", [src_calc]))
check("用户原话里的数字不提示",
      number_guard("3580 打 8.5 折是 3043", ["3580 打 8.5 折", src_calc]) == [])
check("差两位不提示", number_guard("结果是 3993 元", [src_calc]) == [],
      number_guard("结果是 3993 元", [src_calc]))
check("小数不参与", number_guard("每人 1014.33 元", [src_calc]) == [],
      number_guard("每人 1014.33 元", [src_calc]))
check("无来源时不乱报", number_guard("一共 3053 元", []) == [])

print("=== 汇总 ===")
lines = r.summary()
check("summary 能出人话", any("结构缺" in l for l in lines), lines)
check("ok 属性在有问题时为 False", r.ok is False)
full = audit(base_report(["甲厂产量：1500 台"], extra="甲厂、乙厂合格率均已说明。"), src)
check("干净报告 ok 为 True", full.ok, (full.uncovered, full.unverified, full.missing_sections))

print(f"\n结果：{PASS} 通过，{FAIL} 失败")
sys.exit(0 if FAIL == 0 else 1)
