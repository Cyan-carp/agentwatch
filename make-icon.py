# -*- coding: utf-8 -*-
"""生成 WorkBuddy Monitor 图标：多尺寸 .ico + 预览图。

标识为纯图形：圆角底板 + 三根长短不一的活动条，不掺任何文字。
页面内联 SVG 使用同一套比例，任务栏、标题栏、界面品牌位是同一个符号。
"""
import os
import struct
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

OUT_DIR = Path(__file__).resolve().parent
FONT_UI = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "msyh.ttc"

TOP_BG = (30, 48, 78)
BOTTOM_BG = (8, 11, 17)
RING_TOP = (186, 226, 255)
RING_BOTTOM = (78, 138, 246)

# 几何比例（相对边长 S）：与 index.html 里 64×64 的 SVG 一一对应
BAR_H = (0.340, 0.520, 0.400)   # 三根条的高度
BAR_W = 0.088                  # 条宽
BAR_GAP = 0.062                # 条间距
BAR_RX = 0.5                   # 圆角半径 = 条宽的一半（圆头圆尾）


def vgrad(size, top, bottom):
    w, h = size
    strip = Image.new("RGB", (1, h))
    px = strip.load()
    for y in range(h):
        t = y / max(1, h - 1)
        px[0, y] = (
            int(top[0] + (bottom[0] - top[0]) * t),
            int(top[1] + (bottom[1] - top[1]) * t),
            int(top[2] + (bottom[2] - top[2]) * t),
        )
    return strip.resize((w, h), Image.BILINEAR)


def soft_glow(canvas, center, radius, color, strength=1.0):
    """用模糊圆做光晕，边缘自然衰减到 0（避免出现方块边）。"""
    layer = Image.new("RGBA", canvas, (0, 0, 0, 0))
    cx, cy = center
    ImageDraw.Draw(layer).ellipse(
        [cx - radius, cy - radius, cx + radius, cy + radius], fill=color + (255,)
    )
    layer = layer.filter(ImageFilter.GaussianBlur(radius * 0.62))
    if strength < 1.0:
        alpha = layer.getchannel("A").point(lambda v: int(v * strength))
        layer.putalpha(alpha)
    return layer


def render(size, scale=4):
    S = size * scale
    compact = size <= 24                      # 小尺寸加粗，避免糊成一团
    cx = cy = S / 2
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))

    shape = Image.new("L", (S, S), 0)
    ImageDraw.Draw(shape).rounded_rectangle(
        [0, 0, S - 1, S - 1], radius=int(S * 0.235), fill=255
    )
    img.paste(vgrad((S, S), TOP_BG, BOTTOM_BG), (0, 0), shape)

    # 左上冷光
    halo = soft_glow((S, S), (S * 0.30, S * 0.14), S * 0.60, (86, 182, 255), 0.38)
    halo.putalpha(ImageChops.multiply(halo.getchannel("A"), shape))
    img.alpha_composite(halo)

    # 顶部薄高光（渐变淡出，避免横向接缝）
    gloss_alpha = Image.new("L", (1, S), 0)
    gp = gloss_alpha.load()
    fade_to = int(S * 0.55)
    for y in range(fade_to):
        gp[0, y] = int(16 * (1 - y / fade_to) ** 1.8)
    gloss = Image.new("RGBA", (S, S), (255, 255, 255, 0))
    gloss.putalpha(gloss_alpha.resize((S, S), Image.BILINEAR))
    gloss.putalpha(ImageChops.multiply(gloss.getchannel("A"), shape))
    img.alpha_composite(gloss)

    # 三根活动条
    w = S * (BAR_W * (1.10 if compact else 1.0))
    gap = S * (BAR_GAP * (1.16 if compact else 1.0))
    total = w * 3 + gap * 2
    x = (S - total) / 2

    bars = Image.new("L", (S, S), 0)
    bd = ImageDraw.Draw(bars)
    for h_ratio in BAR_H:
        h = S * h_ratio
        bd.rounded_rectangle(
            [x, cy - h / 2, x + w, cy + h / 2], radius=w * BAR_RX, fill=255
        )
        x += w + gap

    if not compact:
        glow = bars.filter(ImageFilter.GaussianBlur(S * 0.030))
        layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        layer.paste((88, 156, 255, 135), (0, 0), glow)
        layer.putalpha(ImageChops.multiply(layer.getchannel("A"), shape))
        img.alpha_composite(layer)
    img.paste(vgrad((S, S), RING_TOP, RING_BOTTOM), (0, 0), bars)

    # 内描边
    ring_line = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(ring_line).rounded_rectangle(
        [1, 1, S - 2, S - 2], radius=int(S * 0.235),
        outline=(98, 156, 255, 68), width=max(1, int(S * 0.007)),
    )
    ring_line.putalpha(ImageChops.multiply(ring_line.getchannel("A"), shape))
    img.alpha_composite(ring_line)

    return img.resize((size, size), Image.LANCZOS)


def build_ico(frames, path):
    frames = sorted(frames, key=lambda im: im.width)
    blobs = []
    for im in frames:
        buf = BytesIO()
        im.save(buf, format="PNG", optimize=True)
        blobs.append(buf.getvalue())

    header = struct.pack("<HHH", 0, 1, len(frames))
    offset = 6 + 16 * len(frames)
    entries, data = b"", b""
    for im, blob in zip(frames, blobs):
        dim = 0 if im.width >= 256 else im.width
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(blob), offset)
        offset += len(blob)
        data += blob
    path.write_bytes(header + entries + data)


SIZES = [16, 24, 32, 48, 64, 128, 256]
frames = {s: render(s) for s in SIZES}
build_ico([frames[s] for s in SIZES], OUT_DIR / "workbuddy.ico")
render(68).save(OUT_DIR / "workbuddy-68.png")   # 侧栏头像用（subsample 2 → 34px）

# ---- 预览图 ----
W, H = 780, 250
sheet = Image.new("RGB", (W, H), (14, 16, 20))
big = frames[256]
sheet.paste(big, (28, 22), big)
d = ImageDraw.Draw(sheet)
f_small = ImageFont.truetype(FONT_UI, 12)
f_note = ImageFont.truetype(FONT_UI, 13)

x = 330
for s in SIZES[:-1]:
    tile = Image.new("RGB", (86, 86), (21, 25, 32))
    tile.paste(frames[s], ((86 - s) // 2, (86 - s) // 2), frames[s])
    sheet.paste(tile, (x, 40))
    d.text((x + 43, 138), f"{s}px", font=f_small, fill=(122, 130, 142), anchor="ma")
    x += 92

d.text((330, 178), "纯图形标识：三根活动条，无任何文字",
       font=f_note, fill=(150, 158, 170))
d.text((330, 202), "24px 及以下加宽条与间距，保证小尺寸不糊",
       font=f_note, fill=(104, 112, 124))
sheet.save(OUT_DIR / "icon-preview.png")

print("已生成:")
for name in ("workbuddy.ico", "workbuddy-68.png", "icon-preview.png"):
    p = OUT_DIR / name
    print(f"  {name:20s} {p.stat().st_size:>8,} bytes")
print("ICO 内含尺寸:", sorted(Image.open(OUT_DIR / "workbuddy.ico").info.get("sizes", [])))
