"""造一份含图片的 docx，用来验证图片识别链路"""
from pathlib import Path

import docx
from PIL import Image, ImageDraw

base = Path(__file__).parent
kb = base.parent / "kb_test"
kb.mkdir(exist_ok=True)

# --- 图1：一张"数据截图"，含中文和数字 ---
from PIL import ImageFont

font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 30)
img1 = Image.new("RGB", (980, 230), "white")
d = ImageDraw.Draw(img1)
d.text((25, 30), "2026年三季度核心指标", font=font, fill="black")
d.text((25, 85), "营业收入 1.28 亿元  同比 +23.5%", font=font, fill="black")
d.text((25, 140), "毛利率 38.2%  同比下降 2.1 个百分点", font=font, fill="black")
d.text((25, 190), "研发费用 1580 万元  占比 12.3%", font=font, fill="black")
p1 = base / "fig_table.png"
img1.save(p1)

# --- 图2：柱状图，几乎没有文字，用来触发视觉模型 ---
img2 = Image.new("RGB", (900, 420), "white")
d = ImageDraw.Draw(img2)
d.line((90, 360, 850, 360), fill="black", width=2)
d.line((90, 360, 90, 40), fill="black", width=2)
bars = [(700, 210), (480, 150), (390, 120), (560, 180)]
for i, (h, w) in enumerate(bars):
    x = 140 + i * 170
    d.rectangle((x, 360 - h, x + 90, 360), fill=(70, 110, 200))
p2 = base / "fig_chart.png"
img2.save(p2)

# --- 组装 docx ---
doc = docx.Document()
doc.add_heading("2026年第三季度经营分析报告", level=1)
doc.add_paragraph("本报告汇总第三季度的经营情况，重点说明收入结构、成本变化与人员情况。")

doc.add_heading("一、经营概况", level=2)
doc.add_paragraph(
    "第三季度公司实现营业收入1.28亿元，同比增长23.5%，主要来自华东区新签客户。"
    "营业成本0.79亿元，毛利率38.2%，同比下降2.1个百分点。"
)
doc.add_paragraph("核心指标见下表截图：")
doc.add_picture(str(p1), width=docx.shared.Inches(6))

doc.add_heading("二、季度走势", level=2)
doc.add_paragraph("下图为本年前四个季度的收入走势：")
doc.add_picture(str(p2), width=docx.shared.Inches(5.5))

doc.add_heading("三、风险与措施", level=2)
doc.add_paragraph(
    "原材料价格持续上涨，挤压毛利空间。措施：与两家供应商签订长协价，"
    "并在四季度启动替代料验证。"
)

out = kb / "带图片的季度报告.docx"
doc.save(out)
print("已生成:", out, out.stat().st_size, "字节")
