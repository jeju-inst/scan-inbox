"""스캔 느낌 효과: 약한 기울기, 종이 노이즈, 흐림, JPEG 압축. 전부 seed 로 결정적이다."""
from __future__ import annotations

import io
import random

from PIL import Image, ImageChops, ImageFilter


def scan_effect(page: Image.Image, rng: random.Random, *, max_skew_deg: float = 0.6,
                noise: float = 1.0, jpeg_quality: int = 72) -> bytes:
    """깨끗한 흑백 페이지를 스캔 느낌 JPEG 바이트로 바꾼다."""
    skew = rng.uniform(-max_skew_deg, max_skew_deg)
    img = page.rotate(skew, resample=Image.BICUBIC, expand=False, fillcolor=255)
    img = img.filter(ImageFilter.GaussianBlur(radius=0.6))
    w, h = img.size
    # 종이 노이즈: 235..254 범위 값을 곱해 약하게 어둡게 만든다
    raw = rng.randbytes(w * h)
    n = Image.frombytes("L", (w, h), raw).point(lambda v: 255 - int((v / 255.0) * 20 * noise))
    img = ImageChops.multiply(img, n)
    # 가끔 먼지 점
    from PIL import ImageDraw
    d = ImageDraw.Draw(img)
    for _ in range(rng.randint(3, 9)):
        x, y = rng.randrange(w), rng.randrange(h)
        r = rng.choice([1, 1, 2])
        d.ellipse([x - r, y - r, x + r, y + r], fill=rng.randint(90, 170))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=jpeg_quality, optimize=False)
    return buf.getvalue()


def blank_scan(w: int, h: int, rng: random.Random, **kw) -> bytes:
    """빈 페이지(노이즈와 먼지만 있는 흰 종이)."""
    return scan_effect(Image.new("L", (w, h), 255), rng, **kw)
