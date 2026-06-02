"""PIL-рендер «Архетипического профиля» в картинку (для отправки в боте).

render_profile(portrait_bytes, dist, name) -> PNG bytes.
Самодостаточно (Pillow + бандл шрифтов в assets/fonts) — работает на Render free
без headless-браузера. Вёрстка повторяет HTML-профиль (kydaidy.com/profile).
"""

from __future__ import annotations

import io
import os

from PIL import Image, ImageDraw, ImageFont

from shadow_test import ORDER, decode_distribution, winner_from_counts, _TIEBREAK
from profile_data import ARCH, PROF

_FONTS = os.path.join(os.path.dirname(__file__), "assets", "fonts")
_SERIF = os.path.join(_FONTS, "CormorantGaramond-VF.ttf")
_ITALIC = os.path.join(_FONTS, "CormorantGaramond-Italic-VF.ttf")
_HAND = os.path.join(_FONTS, "Caveat-Variable.ttf")

W = 1080
PAD = 70
INK = (42, 33, 20)
INK2 = (90, 74, 51)
BORDO = (110, 43, 48)
GOLD = (181, 138, 74)
CARD = (251, 245, 230)
CARD_A = (251, 245, 230, 180)


def _hex(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _font(size, weight="Regular", italic=False):
    f = ImageFont.truetype(_ITALIC if italic else _SERIF, size)
    try:
        f.set_variation_by_name(weight)
    except Exception:
        pass
    return f


def _hand(size):
    f = ImageFont.truetype(_HAND, size)
    try:
        f.set_variation_by_name("SemiBold")
    except Exception:
        pass
    return f


def _wrap(draw, text, font, max_w):
    words, lines, cur = text.split(), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if draw.textlength(t, font=font) <= max_w:
            cur = t
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _para(draw, text, font, x, y, max_w, fill, lh, align="left"):
    for ln in _wrap(draw, text, font, max_w):
        wln = draw.textlength(ln, font=font)
        xx = x + (max_w - wln) / 2 if align == "center" else x
        draw.text((xx, y), ln, font=font, fill=fill)
        y += lh
    return y


def _tracked(draw, text, font, x, y, fill, sp=2.0, upper=True):
    if upper:
        text = text.upper()
    for ch in text:
        draw.text((x, y), ch, font=font, fill=fill)
        x += draw.textlength(ch, font=font) + sp
    return x


def _tracked_w(draw, text, font, sp=2.0, upper=True):
    if upper:
        text = text.upper()
    return sum(draw.textlength(ch, font=font) + sp for ch in text) - sp


def _rounded(img, box, radius, fill):
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(overlay).rounded_rectangle(box, radius=radius, fill=fill)
    img.alpha_composite(overlay)


def _ranked(dist):
    counts = decode_distribution(dist) or {c: 0 for c in ORDER}
    total = sum(counts.values()) or 1
    arr = [{"k": k, "n": counts.get(k, 0), "pct": round(counts.get(k, 0) / total * 100)} for k in ORDER]
    arr.sort(key=lambda x: (-x["n"], _TIEBREAK.index(x["k"])))
    return [a for a in arr if a["n"] > 0]


def _portrait_rounded(portrait_bytes, w, h, rad=14):
    im = Image.open(io.BytesIO(portrait_bytes)).convert("RGB")
    sw, sh = im.size
    scale = max(w / sw, h / sh)
    im = im.resize((int(sw * scale) + 1, int(sh * scale) + 1))
    left = (im.width - w) // 2
    top = (im.height - h) // 2
    im = im.crop((left, top, left + w, top + h)).convert("RGBA")
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, w, h], radius=rad, fill=255)
    im.putalpha(mask)
    return im


def _icon_circle(img, cx, cy, r, color):
    """Простой кружок-иконка с акцентным кольцом (v1)."""
    d = ImageDraw.Draw(img)
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=color + (255,), width=3)
    d.ellipse([cx - 4, cy - 4, cx + 4, cy + 4], fill=color + (255,))


def render_profile(portrait_bytes, dist, name=None):
    rk = _ranked(dist)
    if not rk:
        rk = [{"k": "W", "n": 10, "pct": 100}]
    lead = rk[0]
    lk = lead["k"]
    lname, ltoo, lcolor_h, _ = ARCH[lk]
    lcolor = _hex(lcolor_h)
    p = PROF[lk]

    H = 2300
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    # фон-градиент
    top, bot = (246, 238, 220), (232, 219, 190)
    base = Image.new("RGB", (W, H))
    bd = ImageDraw.Draw(base)
    for yy in range(H):
        t = yy / H
        bd.line([(0, yy), (W, yy)], fill=tuple(int(top[i] + (bot[i] - top[i]) * t) for i in range(3)))
    img = Image.alpha_composite(base.convert("RGBA"), img)
    d = ImageDraw.Draw(img)

    x0 = PAD
    y = 60

    # header
    _tracked(d, "твоя тень", _font(22, "SemiBold"), x0, y, BORDO, sp=5)
    y += 34
    d.text((x0, y), "АРХЕТИПИЧЕСКИЙ", font=_font(76, "Bold"), fill=INK); y += 70
    d.text((x0, y), "ПРОФИЛЬ", font=_font(76, "Bold"), fill=INK); y += 96

    # ---- LEADING ----
    block_top = y
    LW = 600                      # left column width
    px = W - PAD - 330            # portrait x
    pw, ph = 330, 495
    portrait = _portrait_rounded(portrait_bytes, pw, ph)
    img.alpha_composite(portrait, (px, block_top))
    d = ImageDraw.Draw(img)

    ly = block_top
    # pill
    pill = "твой ведущий архетип"
    pf = _font(17, "SemiBold")
    pw_ = _tracked_w(d, pill, pf, sp=2.5) + 32
    _rounded(img, [x0, ly, x0 + pw_, ly + 38], 19, (110, 43, 48, 26))
    d = ImageDraw.Draw(img)
    _tracked(d, pill, pf, x0 + 16, ly + 9, INK2, sp=2.5)
    ly += 58
    # icon + name + too
    _icon_circle(img, x0 + 30, ly + 30, 30, lcolor); d = ImageDraw.Draw(img)
    d.text((x0 + 78, ly + 2), lname, font=_font(44, "Bold"), fill=INK)
    d.text((x0 + 78, ly + 50), ltoo, font=_font(24, italic=True), fill=INK2)
    ly += 92
    d.text((x0, ly), f"≈ {lead['pct']}%", font=_font(52, "Bold"), fill=GOLD); ly += 64
    d.text((x0, ly), "Главный вопрос:", font=_font(24, "SemiBold"), fill=BORDO); ly += 32
    ly = _para(d, p["q"], _font(26, italic=True), x0, ly, LW, INK, 34) + 8
    ly = _para(d, p["d"], _font(22), x0, ly, LW, INK, 30) + 14
    # fear card
    fear_lines = _wrap(d, p["fear"], _font(22, italic=True), LW - 48)
    fh = 30 + 26 + len(fear_lines) * 30 + 18
    _rounded(img, [x0, ly, x0 + LW, ly + fh], 14, CARD_A); d = ImageDraw.Draw(img)
    _tracked(d, "страх:", _font(16, "Bold"), x0 + 24, ly + 22, BORDO, sp=2)
    fy = ly + 50
    for ln in fear_lines:
        d.text((x0 + 24, fy), ln, font=_font(22, italic=True), fill=INK); fy += 30
    ly += fh

    y = max(ly, block_top + ph) + 46

    # ---- secondary header ----
    y = _section(img, d, "твои ведущие архетипы", y)
    d = ImageDraw.Draw(img)

    # ---- 4 cards ----
    sec = rk[:4]
    gap = 16
    cw = (W - 2 * PAD - gap * (len(sec) - 1)) / len(sec) if sec else 0
    # measure max height
    card_h = 0
    name_f, short_f = _font(22, "Bold"), _font(18)
    metas = []
    for s in sec:
        nm = ARCH[s["k"]][0]
        nm = nm if d.textlength(nm, font=name_f) <= cw - 24 else "Разрушит."
        sh = _wrap(d, ARCH[s["k"]][3], short_f, cw - 32)
        h = 22 + 58 + 14 + 28 + 8 + 28 + 4 + len(sh) * 24 + 20
        metas.append((nm, sh, h)); card_h = max(card_h, h)
    cx = PAD
    for s, (nm, sh, _h) in zip(sec, metas):
        col = _hex(ARCH[s["k"]][2])
        _rounded(img, [cx, y, cx + cw, y + card_h], 14, CARD_A); d = ImageDraw.Draw(img)
        _icon_circle(img, cx + cw / 2, y + 22 + 28, 26, col); d = ImageDraw.Draw(img)
        ny = y + 22 + 58 + 10
        wn = d.textlength(nm, font=name_f)
        d.text((cx + (cw - wn) / 2, ny), nm, font=name_f, fill=col); ny += 30
        pc = f"≈ {s['pct']}%"
        wp = d.textlength(pc, font=_font(22, "Bold"))
        d.text((cx + (cw - wp) / 2, ny), pc, font=_font(22, "Bold"), fill=col); ny += 34
        for ln in sh:
            wl = d.textlength(ln, font=short_f)
            d.text((cx + (cw - wl) / 2, ny), ln, font=short_f, fill=INK2); ny += 24
        cx += cw + gap
    y += card_h + 40

    # ---- inside header ----
    y = _section(img, d, "твоя тень изнутри", y); d = ImageDraw.Draw(img)

    # ---- 3 boxes ----
    g = 18
    bw = (W - 2 * PAD - 2 * g) / 3
    box_specs = [
        ("главная теневая тема", "quote", p["th"]),
        ("что движет тобой", "list", p["dr"]),
        ("твоя внутренняя фраза", "phrase", p["ph"]),
    ]
    y = _row_boxes(img, box_specs, PAD, y, bw, g)
    d = ImageDraw.Draw(img)

    # ---- 2 boxes ----
    g2 = 18
    bw2 = (W - 2 * PAD - g2) / 2
    y = _row_boxes(img, [
        ("как раскрывается твой потенциал", "list", p["po"]),
        ("твой след в мире", "quote", p["tr"]),
    ], PAD, y, bw2, g2)
    d = ImageDraw.Draw(img)

    # ---- footer ----
    y += 30
    big = p["fb"]; bf = _font(30, "Bold")
    d.text(((W - d.textlength(big, font=bf)) / 2, y), big, font=bf, fill=INK); y += 42
    sm = p["fs"]; sf = _font(22, italic=True)
    d.text(((W - d.textlength(sm, font=sf)) / 2, y), sm, font=sf, fill=BORDO); y += 40
    sg = "— Алёна Kyda Idy"; sgf = _hand(34)
    d.text(((W - d.textlength(sg, font=sgf)) / 2, y), sg, font=sgf, fill=BORDO); y += 54

    crop = img.crop((0, 0, W, min(H, y + 30))).convert("RGB")
    out = io.BytesIO()
    crop.save(out, "PNG")
    return out.getvalue()


def _section(img, d, label, y):
    f = _font(18, "SemiBold")
    w = _tracked_w(d, label, f, sp=3)
    cx = W / 2
    d.line([(cx - w / 2 - 110, y + 12), (cx - w / 2 - 24, y + 12)], fill=GOLD, width=1)
    d.line([(cx + w / 2 + 24, y + 12), (cx + w / 2 + 110, y + 12)], fill=GOLD, width=1)
    _tracked(d, label, f, cx - w / 2, y, INK2, sp=3)
    return y + 46


def _row_boxes(img, specs, x, y, bw, gap):
    d = ImageDraw.Draw(img)
    lbl_f = _font(16, "Bold")
    heights = []
    contents = []
    for label, kind, data in specs:
        inner = bw - 48
        lines = []
        if kind == "list":
            for it in data:
                for ln in _wrap(d, "✦ " + it, _font(20), inner):
                    lines.append(("li", ln))
        elif kind == "phrase":
            for ln in _wrap(d, data, _font(23, italic=True), inner):
                lines.append(("ph", ln))
        else:
            for ln in _wrap(d, data, _font(22, italic=True), inner):
                lines.append(("q", ln))
        h = 24 + 22 + 16 + len(lines) * (32 if kind == "phrase" else 28) + 22
        heights.append(h); contents.append(lines)
    bh = max(heights)
    cx = x
    for (label, kind, data), lines in zip(specs, contents):
        _rounded(img, [cx, y, cx + bw, y + bh], 14, CARD_A); d = ImageDraw.Draw(img)
        _tracked(d, label, lbl_f, cx + 24, y + 24, BORDO, sp=1.5)
        ty = y + 24 + 36
        for kind2, ln in lines:
            if kind2 == "li":
                d.text((cx + 24, ty), ln, font=_font(20), fill=INK); ty += 28
            elif kind2 == "ph":
                d.text((cx + 24, ty), ln, font=_font(23, italic=True), fill=INK); ty += 32
            else:
                d.text((cx + 24, ty), ln, font=_font(22, italic=True), fill=INK); ty += 28
        cx += bw + gap
    return y + bh + 18


# ----- CLI prototype -----
if __name__ == "__main__":
    import sys
    portrait = open(sys.argv[1], "rb").read()
    dist = sys.argv[2] if len(sys.argv) > 2 else "4210000012"
    data = render_profile(portrait, dist)
    open("/tmp/profile_render.png", "wb").write(data)
    print("rendered /tmp/profile_render.png", len(data) // 1024, "KB")
