"""生成 pcldl 图标：蓝色圆角方块 + 白色下载箭头。"""
import math
import os

from PIL import Image, ImageDraw

S = 1024
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

# 背景：竖向渐变 + 圆角
top = (76, 141, 255)
bottom = (31, 99, 208)
grad = Image.new("RGB", (1, S))
for y in range(S):
    t = y / (S - 1)
    grad.putpixel((0, y), tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)))
grad = grad.resize((S, S))

mask = Image.new("L", (S, S), 0)
ImageDraw.Draw(mask).rounded_rectangle([0, 0, S - 1, S - 1], radius=int(S * 0.22), fill=255)
img.paste(grad, (0, 0), mask)

# 白色下载箭头
cx = S / 2
stem_w = S * 0.13
stem_top = S * 0.24
stem_bottom = S * 0.52
d.rounded_rectangle([cx - stem_w / 2, stem_top, cx + stem_w / 2, stem_bottom],
                    radius=stem_w * 0.3, fill="white")

# 箭头头部（三角形，带一点圆角）
hw = S * 0.19
head_top = stem_bottom - S * 0.01
head_tip = S * 0.68
d.polygon([(cx - hw, head_top), (cx + hw, head_top), (cx, head_tip)], fill="white")

# 底部托盘（表示"落地"）
bar_w = S * 0.44
bar_h = S * 0.075
bar_y = S * 0.755
d.rounded_rectangle([cx - bar_w / 2, bar_y, cx + bar_w / 2, bar_y + bar_h],
                    radius=bar_h / 2, fill="white")

out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pcldl.ico")
img.save(out, format="ICO", sizes=[(16, 16), (24, 24), (32, 32), (48, 48),
                                   (64, 64), (128, 128), (256, 256)])
print("saved", out, os.path.getsize(out), "bytes")
