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

from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageEnhance

from profile_image import _font, _wrap, _tracked, _tracked_w

W, H = 1000, 1500
PAD = 96
TOP = 210            # верх контент-зоны
BOT = H - 320        # низ контент-зоны (над призывом+футером)
CONTENT_W = W - 2 * PAD

# Дефолтный призыв по воронке (если у единицы нет своего cta) — на КАЖДОМ пине.
DEFAULT_CTA = "Узнай свою Тень — пройди тест на kydaidy.com"

# Тест Тени живёт на САЙТЕ (не в боте). Это адрес назначения (destination link)
# пина — Алёна вставляет его в поле ссылки при публикации. В тексте описания
# Pinterest ссылки некликабельны, поэтому активная ссылка = именно это поле.
SHADOW_TEST_URL = "https://kydaidy.com/shadow"


def pin_link(ext_id: str | None = None, source: str = "pinterest") -> str:
    """Готовая ссылка назначения пина на тест Тени + UTM-метка для аналитики."""
    utm = f"?utm_source={source}&utm_medium=pin"
    if ext_id:
        utm += f"&utm_content={ext_id}"
    return SHADOW_TEST_URL + utm

# Палитра — тёплый тёмный
BG_TOP = (32, 27, 23)
BG_BOT = (18, 15, 12)
CREAM = (240, 232, 214)
CREAM2 = (197, 185, 162)
GOLD = (190, 150, 86)
MUTE = (138, 120, 100)      # приглушённый текст «лжи»
BORDO = (150, 78, 78)


def _bg() -> Image.Image:
    # 1. тёплый вертикальный градиент
    base = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(base)
    top, bot = (42, 32, 26), (15, 12, 10)
    for yy in range(H):
        t = yy / H
        d.line([(0, yy), (W, yy)],
               fill=tuple(int(top[i] + (bot[i] - top[i]) * t) for i in range(3)))
    # 2. тёплое свечение (свеча) в верхней трети — глубина
    glow = Image.new("L", (W, H), 0)
    ImageDraw.Draw(glow).ellipse(
        [int(W * 0.04), int(-H * 0.18), int(W * 0.96), int(H * 0.52)], fill=80)
    glow = glow.filter(ImageFilter.GaussianBlur(180))
    glow = glow.point(lambda v: int(v * 0.55))
    base = Image.composite(Image.new("RGB", (W, H), (152, 110, 60)), base, glow)
    # 3. виньетка — затемняем края
    vig = Image.new("L", (W, H), 0)
    ImageDraw.Draw(vig).ellipse(
        [int(-W * 0.15), int(-H * 0.08), int(W * 1.15), int(H * 1.08)], fill=255)
    vig = vig.filter(ImageFilter.GaussianBlur(230))
    base = Image.composite(base, Image.new("RGB", (W, H), (8, 6, 5)), vig)
    # 4. зерно плёнки — убирает «цифровую плоскость»
    grain = Image.effect_noise((W, H), 26).convert("RGB")
    base = Image.blend(base, grain, 0.05)
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


# ── призыв по воронке (на каждом пине) ───────────────────────────────────────

def _strip_bot(cta: str) -> str:
    """Убрать хвост «→ @kydaidy_bot» из cta — @kydaidy_bot и так в футере."""
    t = (cta or "").strip()
    for tail in ("→ @kydaidy_bot", "@kydaidy_bot", "→ @kydaidy", "→"):
        if t.endswith(tail):
            t = t[: -len(tail)].rstrip(" ·—-→")
    return t.strip()


def _cta(img, cta: str | None):
    """Призыв по воронке золотом, по центру, в зоне над футером (≤2 строки)."""
    text = _strip_bot(cta) if cta else DEFAULT_CTA
    if not text:
        text = DEFAULT_CTA
    d = ImageDraw.Draw(img)
    band_top, band_bot = H - 300, H - 210
    f, lines, lh = _fit(d, text, CONTENT_W, band_bot - band_top,
                        italic=True, weight="SemiBold", hi=40, lo=26)
    if len(lines) > 2:                       # держим компактно
        lines = lines[:2]
    total = len(lines) * lh
    y = band_top + (band_bot - band_top - total) / 2
    # маленькая золотая точка-метка над призывом (рисуем, не эмодзи — шрифт без emoji)
    r = 5
    d.ellipse([W / 2 - r, y - 24, W / 2 + r, y - 24 + 2 * r], fill=GOLD)
    for ln in lines:
        w = d.textlength(ln, font=f)
        d.text((W / 2 - w / 2, y), ln, font=f, fill=GOLD)
        y += lh


# ── шапка/футер бренда ────────────────────────────────────────────────────────

def _chrome(img):
    d = ImageDraw.Draw(img)
    # тонкая рамка-кант (двойная линия) — «дизайн-карточка», а не плоский текст
    m = 46
    d.rectangle([m, m, W - m, H - m], outline=(120, 92, 52), width=2)
    d.rectangle([m + 7, m + 7, W - m - 7, H - m - 7], outline=(70, 56, 38), width=1)
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


def render_pin(thesis: str, fmt: str | None = None, ext_id: str | None = None,
               cta: str | None = None) -> bytes:
    """Типографский пин (тёмный фон). cta — призыв по воронке, рисуется на пине."""
    img = _bg()
    kind = _classify(thesis, fmt)
    if kind == "anti":
        _layout_anti(img, thesis)
    elif kind == "list":
        _layout_list(img, thesis, fmt)
    else:
        _layout_quote(img, thesis)
    _cta(img, cta)
    _chrome(img)
    out = io.BytesIO()
    img.convert("RGB").save(out, "PNG")
    return out.getvalue()


# ── Атмосферный фото-пин (фон Nano Banana + текст поверх) ─────────────────────

def bg_prompt(visual_brief: str | None) -> str:
    """Промпт атмосферного ФОНА для пина из визуал-брифа. Текст НЕ просим
    (накладываем сами). Бренд: тёмно-тёплая кинематографичная сцена, без лиц."""
    brief = (visual_brief or "").strip()
    base = (
        "Create a SINGLE vertical background image, aspect ratio 2:3 (portrait), "
        "atmospheric and cinematic. STYLE: dark warm moody palette — charcoal, deep "
        "browns, oxblood, muted candle-gold; soft film grain, gentle vignette, "
        "painterly/photographic, calm and a little melancholic, dignified. "
        "NO people faces, NO portraits, NO glamour, NO bright/pink/cute, NOT horror. "
        "Keep the CENTER calm and uncluttered for text overlay; interest in top/bottom "
        "thirds. ABSOLUTELY NO text, NO letters, NO words, NO watermark, NO frame."
    )
    if brief:
        base += f"\n\nSCENE (interpret atmospherically, no literal text): {brief}."
    return base


def photo_query(visual_brief: str | None, thesis: str | None = None) -> str:
    """Бриф (рус) → англоязычный запрос к стоку, атмосферно/монохромно (стиль Алёны)."""
    b = (visual_brief or "").lower()
    table = [
        (("силуэт", "тень", "тёмн", "темн"), "dark moody silhouette shadow aesthetic"),
        (("дорога", "путь", "развилк", "карт"), "empty road fog cinematic moody"),
        (("струна", "натян", "ткан"), "dark fabric texture moody minimal"),
        (("окно", "дождь"), "rain window melancholic dark"),
        (("свеч", "свет"), "candle warm dim light dark interior"),
        (("колод", "карт переп"), "tarot cards candlelight dark aesthetic"),
        (("туман", "лес"), "foggy forest moody dark"),
        (("зеркал",), "blurred mirror reflection moody dark"),
        (("струк", "фактур", "бумаг"), "dark textured paper grain aesthetic"),
    ]
    for keys, q in table:
        if any(k in b for k in keys):
            return q
    # дефолт — киношная тёмно-тёплая эстетика (как доминанта её ленты)
    return "dark moody aesthetic warm cinematic minimal"


def _cover(img: Image.Image, w: int, h: int) -> Image.Image:
    """Масштаб «cover» + центр-кроп под (w×h)."""
    iw, ih = img.size
    scale = max(w / iw, h / ih)
    nw, nh = int(iw * scale + 0.5), int(ih * scale + 0.5)
    img = img.resize((nw, nh), Image.LANCZOS)
    left, top = (nw - w) // 2, (nh - h) // 2
    return img.crop((left, top, left + w, top + h))


def _scrim() -> Image.Image:
    """Тёмная вуаль для читаемости текста: плоский слой + усиление сверху/снизу."""
    overlay = Image.new("RGBA", (W, H), (10, 8, 6, 120))
    d = ImageDraw.Draw(overlay)
    for yy in range(H):
        edge = 0
        if yy < 320:
            edge = int(90 * (1 - yy / 320))
        elif yy > H - 380:
            edge = int(120 * ((yy - (H - 380)) / 380))
        if edge:
            d.line([(0, yy), (W, yy)], fill=(10, 8, 6, edge))
    return overlay


# ── Стиль Алёны: белый моноширинный текст по центру (см. pinterest-style-guide) ──

_MONO = os.path.join(os.path.dirname(__file__), "assets", "fonts")
_MONO_REG = os.path.join(_MONO, "JetBrainsMono-Regular.ttf")
_MONO_MED = os.path.join(_MONO, "JetBrainsMono-Medium.ttf")
WHITE = (245, 243, 240)


def _mfont(size: int, medium: bool = False):
    return ImageFont.truetype(_MONO_MED if medium else _MONO_REG, size)


def _mlen(d, s, font, tr):
    return d.textlength(s, font=font) + tr * max(0, len(s) - 1)


def _mwrap(d, text, font, max_w, tr):
    out, cur = [], ""
    for w in text.split():
        t = (cur + " " + w).strip()
        if _mlen(d, t, font, tr) <= max_w:
            cur = t
        else:
            if cur:
                out.append(cur)
            cur = w
    if cur:
        out.append(cur)
    return out


def _mfit(d, text, max_w, max_h, medium=False, hi=58, lo=30):
    for size in range(hi, lo - 1, -2):
        f = _mfont(size, medium)
        tr = size * 0.12
        lines = _mwrap(d, text, f, max_w, tr)
        lh = int(size * 1.55)
        if len(lines) * lh <= max_h:
            return f, lines, lh, tr
    f = _mfont(lo, medium)
    return f, _mwrap(d, text, f, max_w, lo * 0.12), int(lo * 1.55), lo * 0.12


def _draw_mono(d, line, font, cx, y, tr, fill, shadow=True, left=None):
    """Строка моноширинно с разрядкой; по центру (cx) или от left."""
    w = _mlen(d, line, font, tr)
    x = (cx - w / 2) if left is None else left
    for ch in line:
        if shadow:
            d.text((x + 2, y + 2), ch, font=font, fill=(0, 0, 0))
        d.text((x, y), ch, font=font, fill=fill)
        x += d.textlength(ch, font=font) + tr
    return w


def _photo_scrim(strong=False, band=(TOP, BOT)) -> Image.Image:
    """Вуаль для читаемости: общий тон + усиление снизу (призыв) + мягкая
    тёмная полоса под основным текстом (band) — гарантирует читаемость на любом фото.
    strong — для светлых фото."""
    a = 110 if strong else 78
    overlay = Image.new("RGBA", (W, H), (8, 7, 6, a))
    d = ImageDraw.Draw(overlay)
    bt, bb = band
    bmax = 120 if strong else 95          # доп. затемнение в зоне текста
    feather = 160
    for yy in range(H):
        extra = 0
        # центральная полоса под текстом — мягкие края (feather)
        if bt - feather <= yy <= bb + feather:
            if yy < bt:
                extra = int(bmax * (yy - (bt - feather)) / feather)
            elif yy > bb:
                extra = int(bmax * (1 - (yy - bb) / feather))
            else:
                extra = bmax
        edge = 0
        if yy > H - 360:                  # низ — под призыв
            edge = int(110 * ((yy - (H - 360)) / 360))
        val = max(extra, edge)
        if val:
            d.line([(0, yy), (W, yy)], fill=(8, 7, 6, val))
    return overlay


def _avg_luma(img: Image.Image, box) -> float:
    crop = img.convert("L").crop(box).resize((24, 24))
    px = list(crop.getdata())
    return sum(px) / len(px)


def render_pin_photo(thesis: str, bg_bytes: bytes, fmt: str | None = None,
                     ext_id: str | None = None, cta: str | None = None) -> bytes:
    """Пин в стиле Алёны: фото-фон (сток/Nano Banana) + белый моноширинный текст по центру.

    Поддержка списка: «заголовок: a · b · c» → заголовок + пункты с «•»."""
    bg = Image.open(io.BytesIO(bg_bytes)).convert("RGB")
    base = _cover(bg, W, H)
    # тёмно-моховая обработка (её лента): приглушаем цвет + притемняем → белый текст
    # стабильно читается на любом фото.
    base = ImageEnhance.Color(base).enhance(0.72)
    base = ImageEnhance.Brightness(base).enhance(0.60)
    img = base.convert("RGBA")
    strong = _avg_luma(img, (PAD, TOP, W - PAD, BOT)) > 120
    img = Image.alpha_composite(img, _photo_scrim(strong))

    d = ImageDraw.Draw(img)
    avail = BOT - TOP
    is_list = " · " in thesis or (fmt and "список" in fmt)

    if is_list:
        parts = [p.strip() for p in thesis.split(" · ")]
        head = None
        if ":" in parts[0]:
            head, rest = parts[0].split(":", 1)
            head, parts[0] = head.strip(), rest.strip()
        items = [p for p in parts if p]
        hf, hlines, hlh, htr = (None, [], 0, 0)
        if head:
            hf, hlines, hlh, htr = _mfit(d, head, CONTENT_W, avail * 0.4, medium=True, hi=48, lo=28)
        itf, ilh, itr, isize = None, 0, 0, 44
        while isize >= 26:
            itf = _mfont(isize); itr = isize * 0.12; ilh = int(isize * 1.5)
            wrapped = [_mwrap(d, "•  " + it, itf, CONTENT_W, itr) for it in items]
            body_h = sum(len(w) * ilh for w in wrapped) + (len(items) - 1) * int(isize * 0.5)
            if len(hlines) * hlh + 40 + body_h <= avail:
                break
            isize -= 2
        body_h = sum(len(w) * ilh for w in wrapped) + (len(items) - 1) * int(isize * 0.5)
        total = (len(hlines) * hlh + 40 if head else 0) + body_h
        y = TOP + (avail - total) / 2
        for ln in hlines:
            _draw_mono(d, ln, hf, W / 2, y, htr, WHITE); y += hlh
        if head:
            y += 40
        for w in wrapped:
            for ln in w:
                _draw_mono(d, ln, itf, W / 2, y, itr, WHITE); y += ilh
            y += int(isize * 0.5)
    else:
        text = thesis.replace("❌", "").replace("✅", "").replace("→", "—")
        f, lines, lh, tr = _mfit(d, text, CONTENT_W, avail, medium=False, hi=56, lo=30)
        total = len(lines) * lh
        y = TOP + (avail - total) / 2
        for ln in lines:
            _draw_mono(d, ln, f, W / 2, y, tr, WHITE); y += lh

    _cta_mono(d, cta)
    out = io.BytesIO()
    img.convert("RGB").save(out, "PNG")
    return out.getvalue()


def _cta_mono(d, cta: str | None):
    """Сдержанный призыв по воронке снизу (моно, белый) + @kydaidy_bot."""
    text = _strip_bot(cta) if cta else DEFAULT_CTA
    if not text:
        text = DEFAULT_CTA
    f, lines, lh, tr = _mfit(d, text, CONTENT_W, 120, medium=False, hi=30, lo=22)
    lines = lines[:2]
    y = H - 150 - len(lines) * lh
    for ln in lines:
        _draw_mono(d, ln, f, W / 2, y, tr, WHITE); y += lh
    # подпись = САЙТ (тест Тени живёт на kydaidy.com, не в боте)
    bf = _mfont(26, medium=True)
    _draw_mono(d, "kydaidy.com", bf, W / 2, H - 96, 26 * 0.18, WHITE)


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
