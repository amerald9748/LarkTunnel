# -*- coding: utf-8 -*-
r"""Aggressive icon variants for LarkTunnel (must NOT be confused with the
company logo used by another program).

  A  "inverted portal" : dark navy tile (the app's dark topbar gradient),
                          RGB-inverted logo, concentric rounded frames
                          receding to the centre = a tunnel
  B  "duotone stamp"   : crimson->black tile, logo reduced to a gold/cream
                          duotone, tunnel rings
  C  "hue flip"        : near-black tile, logo hue-rotated 180 deg (white
                          stripes stay white), single thick cyan portal ring

usage (needs Pillow):
    python webapp\make_icon.py <company-logo.png> <preview_dir> [A|B|C]
Writes preview PNGs (+ a comparison sheet) to <preview_dir>; with a variant
letter it also (re)writes static/larktunnel.ico (9 sizes, each rendered
separately so the frames stay crisp at 16 px) and static/larktunnel.png (the
web favicon). Shipped icon = variant B (chosen 2026-09-21).
"""
import os
import sys
from PIL import Image, ImageDraw, ImageOps, ImageFilter, ImageChops

SRC, PREVIEW_DIR = sys.argv[1], sys.argv[2]
PICK = sys.argv[3] if len(sys.argv) > 3 else None
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


def knock_out_background(im):
    im = im.convert("RGBA")
    w, h = im.size
    for xy in [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1),
               (w // 2, 0), (w // 2, h - 1), (0, h // 2), (w - 1, h // 2)]:
        if im.getpixel(xy)[3] != 0 and sum(im.getpixel(xy)[:3]) > 600:
            ImageDraw.floodfill(im, xy, (0, 0, 0, 0), thresh=60)
    return im.crop(im.getbbox())


def gradient(size, c1, c2, diagonal=True):
    g = Image.new("RGBA", (size, size))
    px = g.load()
    for y in range(size):
        for x in range(size):
            t = (x + y) / (2 * (size - 1)) if diagonal else y / (size - 1)
            px[x, y] = tuple(int(a + (b - a) * t) for a, b in zip(c1, c2)) + (255,)
    return g


def rounded_mask(S, r, inset=0):
    m = Image.new("L", (S, S), 0)
    ImageDraw.Draw(m).rounded_rectangle([inset, inset, S - 1 - inset, S - 1 - inset],
                                        max(r - inset, 1), fill=255)
    return m


# ---- logo treatments -------------------------------------------------------
def invert_rgb(logo):
    rgb, a = logo.convert("RGB"), logo.getchannel("A")
    out = ImageOps.invert(rgb).convert("RGBA")
    out.putalpha(a)
    return out


def hue_flip(logo):
    rgb, a = logo.convert("RGB"), logo.getchannel("A")
    h, s, v = rgb.convert("HSV").split()
    h = h.point(lambda p: (p + 128) % 256)
    s = s.point(lambda p: min(255, int(p * 1.25)))          # punchier
    out = Image.merge("HSV", (h, s, v)).convert("RGBA")
    out.putalpha(a)
    return out


def duotone(logo, dark, light):
    lum, a = logo.convert("L"), logo.getchannel("A")
    lum = ImageOps.autocontrast(lum, cutoff=2)
    out = ImageOps.colorize(lum, black=dark, white=light).convert("RGBA")
    out.putalpha(a)
    return out


# ---- motifs ----------------------------------------------------------------
def tunnel_frames(S, r, color, n=6, start=0.90, step=0.085, glow=True):
    """Concentric rounded frames shrinking to the centre (perspective tunnel)."""
    layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    for i in range(n):
        k = start - i * step
        inset = int(S * (1 - k) / 2)
        w = max(int(S * 0.012 * (1 - i / (n + 1))), 2)
        alpha = int(190 * (1 - i / n) ** 1.2) + 30
        d.rounded_rectangle([inset, inset, S - 1 - inset, S - 1 - inset],
                            max(int(r * k), 4), outline=color + (alpha,), width=w)
    if glow:
        blur = layer.filter(ImageFilter.GaussianBlur(S * 0.012))
        layer = ImageChops.add(blur, layer)
    return layer


def portal_ring(S, color, radius=0.42, width=0.075):
    layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    c, R, w = S / 2, S * radius, int(S * width)
    d.ellipse([c - R, c - R, c + R, c + R], outline=color + (255,), width=w)
    glow = layer.filter(ImageFilter.GaussianBlur(S * 0.03))
    return ImageChops.add(glow, layer)


def place(tile, art, S, frac, dy=0):
    k = S * frac / max(art.size)
    a = art.resize((max(1, round(art.width * k)), max(1, round(art.height * k))),
                   Image.Resampling.LANCZOS)
    tile.alpha_composite(a, ((S - a.width) // 2, (S - a.height) // 2 + dy))


# ---- variants --------------------------------------------------------------
def render_A(size, logo):
    S = size * 4
    r = int(S * 0.23)
    tile = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    tile.paste(gradient(S, (11, 26, 43), (23, 65, 110)), (0, 0), rounded_mask(S, r))
    tile.alpha_composite(tunnel_frames(S, r, (108, 176, 245)))
    place(tile, invert_rgb(logo), S, 0.62)
    # thin cyan edge so the tile reads on dark taskbars
    edge = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(edge).rounded_rectangle([0, 0, S - 1, S - 1], r,
                                           outline=(108, 176, 245, 200), width=max(int(S * 0.02), 2))
    tile.alpha_composite(edge)
    return tile.resize((size, size), Image.Resampling.LANCZOS)


def render_B(size, logo):
    S = size * 4
    r = int(S * 0.23)
    tile = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    tile.paste(gradient(S, (140, 20, 45), (20, 6, 12), diagonal=False), (0, 0), rounded_mask(S, r))
    tile.alpha_composite(tunnel_frames(S, r, (255, 200, 90), n=5, start=0.92, step=0.1))
    place(tile, duotone(logo, (70, 8, 20), (255, 225, 160)), S, 0.64)
    return tile.resize((size, size), Image.Resampling.LANCZOS)


def render_C(size, logo):
    S = size * 4
    r = int(S * 0.23)
    tile = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    tile.paste(gradient(S, (18, 18, 24), (6, 6, 10)), (0, 0), rounded_mask(S, r))
    tile.alpha_composite(portal_ring(S, (0, 220, 210)))
    place(tile, hue_flip(logo), S, 0.52)
    return tile.resize((size, size), Image.Resampling.LANCZOS)


RENDER = {"A": render_A, "B": render_B, "C": render_C}

logo = knock_out_background(Image.open(SRC))
os.makedirs(PREVIEW_DIR, exist_ok=True)
sheet = Image.new("RGBA", (3 * 560 + 40, 560 + 200), (245, 245, 245, 255))
for i, key in enumerate("ABC"):
    big = RENDER[key](512, logo)
    big.save(os.path.join(PREVIEW_DIR, f"variant_{key}.png"))
    sheet.alpha_composite(big, (20 + i * 560, 20))
    for j, s in enumerate((48, 32, 16)):          # how it reads small
        small = RENDER[key](s, logo)
        sheet.alpha_composite(small, (20 + i * 560 + j * 80, 560 + 60))
sheet.save(os.path.join(PREVIEW_DIR, "icon_variants.png"))
print("previews written")

if PICK:
    sizes = [256, 128, 64, 48, 40, 32, 24, 20, 16]
    frames = {s: RENDER[PICK](s, logo) for s in sizes}
    frames[256].save(f"{OUT_DIR}\\larktunnel.ico", format="ICO",
                     sizes=[(s, s) for s in sizes], append_images=[frames[s] for s in sizes[1:]])
    frames[128].save(f"{OUT_DIR}\\larktunnel.png")
    print("icon written from variant", PICK)
