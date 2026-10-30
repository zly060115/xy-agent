"""OCR 冒烟测试：造一张中文图，看 RapidOCR 能不能读出来"""
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

out = Path(__file__).parent / "ocr_test.png"
img = Image.new("RGB", (1000, 260), "white")
d = ImageDraw.Draw(img)
font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 34)
d.text((30, 40), "2026年第三季度营收 1.28 亿元，同比增长 23.5%", font=font, fill="black")
d.text((30, 110), "研发投入占比 12%，团队规模 480 人", font=font, fill="black")
d.text((30, 180), "风险：原材料价格上涨导致毛利率下降 2.1 个百分点", font=font, fill="black")
img.save(out)
print("测试图已生成:", out)

print("正在加载 RapidOCR（首次会下载模型）...")
from rapidocr import RapidOCR

engine = RapidOCR()
res = engine(str(out))

# 3.x 返回对象，2.x 返回 (list, elapse)
txts = getattr(res, "txts", None)
if txts is None and isinstance(res, (list, tuple)):
    txts = [item[1] for item in res[0]] if res and res[0] else []

print("识别结果:")
for t in txts or []:
    print("  ", t)
print("共", len(txts or []), "段")
