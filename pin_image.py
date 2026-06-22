"""PIL-рендер брендового вертикального пина (Pinterest) 1000×1500.

render_pin(thesis, fmt=None, ext_id=None) -> PNG bytes.

Тёмный тёплый фон (визуал-бриф батча: «тёмный фон»), тёплый кремовый serif, золотой
акцент и подпись «kydaidy · куда иди?» + @kydaidy_bot снизу (вход в воронку выживает
ре-пин). Самодостаточно: Pillow + бандл шрифтов, переиспользует хелперы profile_image.

Раскладки (под форматы curator_data):
- quote  — пин-тезис/механизм/карта/Тень: одна крупная фраза по центру;
- list   — пин-список («лид: a · b · c»): заголовок + пункты с ✦;
- anti   — анти-цитатник («❌ … → ✅ …»): зачёркнутая ложь → золотая правка.
"""

from __future__ import annotations

import io
import os

from PIL import Image, ImageDraw, ImageFont

from profile_image import _font, _wrap, _tracked, _tracked_w

W, H = 1000, 1500
PAD = 96
TOP = 210            # верх контент-зоны
BOT = H - 230        # низ контент-зоны (над футером)
CONTENT_W = W - 2 * PAD

# Палитра — тёплый тёмный
BG_TOP = (32, 27, 23)
BG_BOT = (18, 15, 12)
CREAM = (240, 232, 214)
CREAM2 = (197, 185, 162)
GOLD = (190, 150, 86)
MUTE = (138, 120, 100)      # приглушённый текст «лжи»
BORDO = (150, 78, 78)


def _bg() -> Image.Image:
    base = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(base)
    for yy in range(H):
        t = yy / H
        d.line([(0, yy), (W, yy)],
               fill=tuple(int(BG_TOP[i] + (BG_BOT[i] - BG_TOP[i]) * t) for i in range(3)))
    # лёгкая виньетка снизу для глубины
    return base.convert("RGBA")


def _fit(draw, text, max_w, max_h, italic=False, weight="SemiBold", hi=96, lo=40):
    """Подобрать максимальный кегль, при котором текст влезает в (max_w × max_h)."""
    for size in range(hi, lo - 1, -2):
        f = _font(size, weight, italic=italic)
        lines = _wrap(draw, text, f, max_w)
        lh = int(size * 1.22)
        if len(lines) * lh <= max_h:
            return f, lines, lh
    f = _font(lo, weight, italic=italic)
    return f, _wrap(draw, text, f, max_w), int(lo * 1.22)


def _draw_lines(d, lines, font, cx, y, lh, fill, strike=False):
    for ln in lines:
        w = d.textlength(ln, font=font)
        x = cx - w / 2
        d.text((x, y), ln, font=font, fill=fill)
        if strike:
            my = y + lh * 0.42
            d.line([(x, my), (x + w, my)], fill=fill, width=3)
        y += lh
    return y


def _classify(thesis: str, fmt: str | None) -> str:
    if "→" in thesis and ("❌" in thesis or "✅" in thesis):
        return "anti"
    if " · " in thesis or (fmt and "список" in fmt):
        return "list"
    return "quote"


# ── раскладки: каждая считает высоту блока и рисует, центрируясь по вертикали ──

def _layout_quote(img, thesis):
    d = ImageDraw.Draw(img)
    avail = BOT - TOP
    font, lines, lh = _fit(d, thesis, CONTENT_W, avail, italic=True, weight="Medium")
    total = len(lines) * lh
    y = TOP + (avail - total) / 2
    _draw_lines(d, lines, font, W / 2, y, lh, CREAM)


def _layout_anti(img, thesis):
    d = ImageDraw.Draw(img)
    left, right = thesis.split("→", 1)
    left = left.replace("❌", "").strip()
    right = right.replace("✅", "").strip()
    avail = BOT - TOP
    half = avail / 2 - 60
    lf, llines, llh = _fit(d, left, CONTENT_W, half, italic=True, weight="Regular", hi=72)
    rf, rlines, rlh = _fit(d, right, CONTENT_W, half, italic=True, weight="SemiBold", hi=80)
    lh_block = len(llines) * llh
    rh_block = len(rlines) * rlh
    gap = 96
    total = lh_block + gap + rh_block
    y = TOP + (avail - total) / 2
    y = _draw_lines(d, llines, lf, W / 2, y, llh, MUTE, strike=True)
    # стрелка-разделитель
    ay = y + gap / 2 - 18
    af = _font(56, "Regular")
    aw = d.textlength("↓", font=af)
    d.text((W / 2 - aw / 2, ay), "↓", font=af, fill=GOLD)
    y += gap
    _draw_lines(d, rlines, rf, W / 2, y, rlh, GOLD)


def _layout_list(img, thesis, fmt):
    d = ImageDraw.Draw(img)
    parts = [p.strip() for p in thesis.split(" · ")]
    head = None
    first = parts[0]
    if ":" in first:
        head, rest = first.split(":", 1)
        head = head.strip()
        parts[0] = rest.strip()
    items = [p for p in parts if p]
    avail = BOT - TOP

    # заголовок (если есть)
    head_lines, hf, hlh, head_h = [], None, 0, 0
    if head:
        hf, head_lines, hlh = _fit(d, head, CONTENT_W, avail * 0.35,
                                   italic=False, weight="SemiBold", hi=64, lo=34)
        head_h = len(head_lines) * hlh + 56

    # пункты центрированы, между ними — золотой разделитель «·»
    isize = 54
    while isize >= 32:
        itf = _font(isize, "Regular", italic=True)
        ilh = int(isize * 1.18)
        wrapped = [_wrap(d, it, itf, CONTENT_W) for it in items]
        sep_h = int(isize * 1.15)
        body_h = sum(len(w) * ilh for w in wrapped) + (len(items) - 1) * sep_h
        if head_h + body_h <= avail:
            break
        isize -= 2
    itf = _font(isize, "Regular", italic=True)
    ilh = int(isize * 1.18)
    sep_h = int(isize * 1.15)
    sep_f = _font(int(isize * 0.8), "Regular")
    body_h = sum(len(w) * ilh for w in wrapped) + (len(items) - 1) * sep_h

    total = head_h + body_h
    y = TOP + (avail - total) / 2
    if head:
        y = _draw_lines(d, head_lines, hf, W / 2, y, hlh, GOLD) + 56
    for i, w in enumerate(wrapped):
        y = _draw_lines(d, w, itf, W / 2, y, ilh, CREAM)
        if i < len(wrapped) - 1:
            dot = "·"
            dw = d.textlength(dot, font=sep_f)
            d.text((W / 2 - dw / 2, y + sep_h * 0.1), dot, font=sep_f, fill=GOLD)
            y += sep_h


# ── шапка/футер бренда ────────────────────────────────────────────────────────

def _chrome(img):
    d = ImageDraw.Draw(img)
    # верхняя короткая черта
    d.line([(W / 2 - 56, 150), (W / 2 + 56, 150)], fill=GOLD, width=2)
    # футер
    d.line([(W / 2 - 56, H - 196), (W / 2 + 56, H - 196)], fill=GOLD, width=2)
    sig = "kydaidy · куда иди?"
    sf = _font(34, "SemiBold")
    sw = _tracked_w(d, sig, sf, sp=2.5, upper=False)
    _tracked(d, sig, sf, W / 2 - sw / 2, H - 162, CREAM, sp=2.5, upper=False)
    bf = _font(26, "Regular")
    bw = _tracked_w(d, "@kydaidy_bot", bf, sp=2.0, upper=False)
    _tracked(d, "@kydaidy_bot", bf, W / 2 - bw / 2, H - 116, GOLD, sp=2.0, upper=False)


def render_pin(thesis: str, fmt: str | None = None, ext_id: str | None = None) -> bytes:
    img = _bg()
    kind = _classify(thesis, fmt)
    if kind == "anti":
        _layout_anti(img, thesis)
    elif kind == "list":
        _layout_list(img, thesis, fmt)
    else:
        _layout_quote(img, thesis)
    _chrome(img)
    out = io.BytesIO()
    img.convert("RGB").save(out, "PNG")
    return out.getvalue()


# ----- CLI prototype -----
if __name__ == "__main__":
    samples = [
        ("P01", "пин-тезис",
         "«Полюби себя» не сработало, потому что это не навык воли. Это режим нервной системы."),
        ("P02", "пин-список",
         "Почему ты выбираешь недоступных: знакомое = безопасное · тревожная привязанность · надежда вместо контакта."),
        ("P03", "анти-цитатник",
         "❌ «Ты — богиня» → ✅ «Ты — человек, которому годами продавали, что этого мало.»"),
    ]
    for ext_id, fmt, thesis in samples:
        data = render_pin(thesis, fmt, ext_id)
        path = f"/tmp/pin_{ext_id}.png"
        open(path, "wb").write(data)
        print(f"rendered {path} {len(data)//1024} KB ({fmt})")
