"""生成桌面版图标：assets/icon.ico（多尺寸）+ assets/icon.png

设计上不画复杂图形：一个圆角方块 + 琥珀色渐变 + 白色「满」字。
小尺寸（16px）下只有这种大色块 + 单字还能看清。
"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "assets"
S = 1024

# 渐变：顶部 #fbbf24（amber-400）→ 底部 #d97706（amber-600）
TOP = (251, 191, 36)
BOTTOM = (217, 119, 6)

grad = Image.new("RGB", (S, S))
d = ImageDraw.Draw(grad)
for y in range(S):
    t = y / (S - 1)
    d.line(
        [(0, y), (S, y)],
        fill=tuple(int(TOP[i] + (BOTTOM[i] - TOP[i]) * t) for i in range(3)),
    )

mask = Image.new("L", (S, S), 0)
ImageDraw.Draw(mask).rounded_rectangle([0, 0, S - 1, S - 1], radius=int(S * 0.225), fill=255)

img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
img.paste(grad, (0, 0), mask)

# 字：微软雅黑 Bold 优先，退回黑体
FONT = None
for p in ("C:/Windows/Fonts/msyhbd.ttc", "C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simhei.ttf"):
    if Path(p).exists():
        FONT = p
        break
if FONT is None:
    raise SystemExit("找不到中文字体，无法生成图标")

draw = ImageDraw.Draw(img)
size = 520
font = ImageFont.truetype(FONT, size)
while size > 200:
    box = draw.textbbox((0, 0), "满", font=font)
    if box[2] - box[0] <= S * 0.62 and box[3] - box[1] <= S * 0.62:
        break
    size -= 20
    font = ImageFont.truetype(FONT, size)
box = draw.textbbox((0, 0), "满", font=font)
# 视觉居中：用字形的实际外框，而不是 font size 的理论行高
x = (S - (box[2] - box[0])) / 2 - box[0]
y = (S - (box[3] - box[1])) / 2 - box[1]
draw.text((x, y), "满", font=font, fill=(255, 255, 255, 255))

OUT.mkdir(parents=True, exist_ok=True)
img.save(OUT / "icon.png")
img.resize((256, 256), Image.LANCZOS).save(
    OUT / "icon.ico",
    format="ICO",
    sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
)
print("ok:", OUT / "icon.ico", (OUT / "icon.ico").stat().st_size, "bytes")
