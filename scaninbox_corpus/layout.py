"""페이지 요소 → 배치(프리미티브) → 래스터(Pillow) / 벡터 텍스트(reportlab).

한 번의 배치 결과를 두 백엔드가 공유하므로, 이미지 전용 페이지와 텍스트층 페이지가
같은 모양이 된다. 좌표 단위는 pt, 원점은 좌상단, A4(595x842).

페이지 요소(elements):
  ("org", 문자열)            좌상단 작은 기관 표기
  ("title", 문자열)          가운데 정렬 큰 제목 (제목 없는 서식에는 쓰지 않는다)
  ("meta", 문자열)           우측 정렬 문서번호·날짜 등
  ("para", 문자열)           본문 단락(자동 줄바꿈)
  ("table", [[셀,...],...])  격자 표
  ("stamp", 문자열)          우하단 도장 모양 사각형
  ("footer", 문자열)         하단 가운데 쪽번호 등
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

PAGE_W, PAGE_H = 595.0, 842.0
MARGIN = 56.0
PDF_FONT_NAME = "ScanInboxKR"


@lru_cache(maxsize=None)
def _font(path: str, size: float) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size=max(1, int(round(size * 8))))  # 8배 해상도로 측정


def text_width(path: Path, size: float, s: str) -> float:
    return _font(str(path), size).getlength(s) / 8.0


def wrap(path: Path, size: float, s: str, width: float) -> list[str]:
    lines: list[str] = []
    cur = ""
    for ch in s:
        if text_width(path, size, cur + ch) > width and cur:
            # 영문 단어 중간은 가능하면 공백에서 끊는다
            if ch != " " and " " in cur and not cur.endswith(" "):
                head, _, tail = cur.rpartition(" ")
                lines.append(head)
                cur = tail + ch
            else:
                lines.append(cur)
                cur = ch.lstrip()
        else:
            cur += ch
    if cur:
        lines.append(cur)
    return lines


def layout(elements: list, font: Path) -> list[tuple]:
    """프리미티브 목록: ("text", x, y_baseline, size, s) / ("rect", x, y, w, h) / ("line", x1, y1, x2, y2)."""
    prims: list[tuple] = []
    y = MARGIN
    cw = PAGE_W - 2 * MARGIN
    for el in elements:
        kind = el[0]
        if kind == "org":
            prims.append(("text", MARGIN, y + 9, 9.0, el[1]))
            y += 20
        elif kind == "title":
            y += 26
            for ln in wrap(font, 22.0, el[1], cw):
                w = text_width(font, 22.0, ln)
                prims.append(("text", (PAGE_W - w) / 2, y + 22, 22.0, ln))
                y += 32
            y += 10
            prims.append(("line", MARGIN, y, PAGE_W - MARGIN, y))
            y += 22
        elif kind == "meta":
            w = text_width(font, 10.0, el[1])
            prims.append(("text", PAGE_W - MARGIN - w, y + 10, 10.0, el[1]))
            y += 20
        elif kind == "para":
            for ln in wrap(font, 11.0, el[1], cw):
                prims.append(("text", MARGIN, y + 11, 11.0, ln))
                y += 19
            y += 9
        elif kind == "table":
            rows = el[1]
            ncol = max(len(r) for r in rows)
            colw = cw / ncol
            rh = 28.0
            for ri, row in enumerate(rows):
                for ci in range(ncol):
                    x = MARGIN + ci * colw
                    prims.append(("rect", x, y, colw, rh))
                    if ci < len(row) and row[ci]:
                        prims.append(("text", x + 6, y + 18, 10.0, row[ci]))
                y += rh
            y += 14
        elif kind == "stamp":
            sx, sy = PAGE_W - MARGIN - 90, max(y + 20, PAGE_H - 190)
            prims.append(("rect", sx, sy, 80, 80))
            w = text_width(font, 11.0, el[1])
            prims.append(("text", sx + (80 - w) / 2, sy + 46, 11.0, el[1]))
        elif kind == "footer":
            w = text_width(font, 9.0, el[1])
            prims.append(("text", (PAGE_W - w) / 2, PAGE_H - 36, 9.0, el[1]))
        else:  # pragma: no cover
            raise ValueError(f"unknown element {kind}")
    return prims


def render_raster(prims: list[tuple], font: Path, dpi: int) -> Image.Image:
    """흑백 래스터 페이지(L 모드)."""
    sc = dpi / 72.0
    img = Image.new("L", (round(PAGE_W * sc), round(PAGE_H * sc)), 255)
    d = ImageDraw.Draw(img)
    lw = max(1, round(sc * 0.8))
    for p in prims:
        if p[0] == "text":
            _, x, yb, size, s = p
            f = ImageFont.truetype(str(font), size=max(1, round(size * sc)))
            d.text((x * sc, yb * sc), s, font=f, fill=20, anchor="ls")
        elif p[0] == "rect":
            _, x, y, w, h = p
            d.rectangle([x * sc, y * sc, (x + w) * sc, (y + h) * sc], outline=30, width=lw)
        elif p[0] == "line":
            _, x1, y1, x2, y2 = p
            d.line([x1 * sc, y1 * sc, x2 * sc, y2 * sc], fill=30, width=lw)
    return img


def draw_vector(canvas, prims: list[tuple]) -> None:
    """reportlab canvas 에 벡터 텍스트(텍스트층 있음)로 그린다. 폰트는 PDF_FONT_NAME 으로 등록돼 있어야 한다."""
    canvas.setLineWidth(0.8)
    for p in prims:
        if p[0] == "text":
            _, x, yb, size, s = p
            canvas.setFont(PDF_FONT_NAME, size)
            canvas.drawString(x, PAGE_H - yb, s)
        elif p[0] == "rect":
            _, x, y, w, h = p
            canvas.rect(x, PAGE_H - y - h, w, h, stroke=1, fill=0)
        elif p[0] == "line":
            _, x1, y1, x2, y2 = p
            canvas.line(x1, PAGE_H - y1, x2, PAGE_H - y2)
