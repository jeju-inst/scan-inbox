"""합성 코퍼스 생성기.

    python -m scaninbox_corpus.generate --out corpus_out --seed 20261105

출력(모두 .gitignore 대상 디렉터리에 만든다):
    <out>/input/               스캔 폴더 모사. 시험 입력.
    <out>/done_preexisting/    완료 폴더에 이미 있는 파일(이름 충돌 시험용)
    <out>/manifest.json        정답 manifest (schema/manifest.schema.json)

같은 seed·폰트·라이브러리 버전이면 바이트 단위로 같은 파일이 나온다(결정적).
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import random
import shutil
import sys
import unicodedata
from pathlib import Path

import PIL
import reportlab
from PIL import Image
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas
from reportlab.lib.pdfencrypt import StandardEncryption

from . import __version__, content
from .fonts import find_font, font_info
from .layout import PAGE_H, PAGE_W, PDF_FONT_NAME, draw_vector, layout, render_raster
from .scanfx import blank_scan, scan_effect

SCHEMA_VERSION = 1
DEFAULT_SEED = 20261105


# --------------------------------------------------------------------------- PDF 조립

class PageSpec:
    """kind: 'image'(스캔 래스터) | 'text'(텍스트층) | 'blank'(빈 스캔 페이지)."""

    def __init__(self, kind: str, elements: list | None = None, *, physical_rotation: int = 0, pdf_rotate: int = 0):
        self.kind = kind
        self.elements = elements
        self.physical_rotation = physical_rotation
        self.pdf_rotate = pdf_rotate


def _register_font(font: Path) -> None:
    pdfmetrics.registerFont(TTFont(PDF_FONT_NAME, str(font)))


def build_pdf(pages: list[PageSpec], font: Path, dpi: int, rng: random.Random, *, encrypt: bool = False) -> tuple[bytes, list[dict]]:
    """PageSpec 목록으로 PDF 바이트와 페이지 정보를 만든다."""
    _register_font(font)
    buf = io.BytesIO()
    kw = {}
    if encrypt:
        kw["encrypt"] = StandardEncryption("synthetic-user", ownerPassword="synthetic-owner", canPrint=1)
    c = Canvas(buf, pagesize=(PAGE_W, PAGE_H), invariant=1, pageCompression=1, **kw)
    infos: list[dict] = []
    for i, ps in enumerate(pages, start=1):
        w_pt, h_pt = PAGE_W, PAGE_H
        if ps.kind == "text":
            c.setPageSize((PAGE_W, PAGE_H))
            draw_vector(c, layout(ps.elements, font))
            content_kind, has_text = "text", True
        else:
            if ps.kind == "blank":
                img_bytes = blank_scan(round(PAGE_W * dpi / 72), round(PAGE_H * dpi / 72), rng)
                content_kind = "blank"
            else:
                raster = render_raster(layout(ps.elements, font), font, dpi)
                if ps.physical_rotation:
                    raster = raster.rotate(ps.physical_rotation, expand=True, fillcolor=255)
                    w_pt, h_pt = PAGE_H, PAGE_W
                img_bytes = scan_effect(raster, rng)
                content_kind = "image"
            has_text = False
            c.setPageSize((w_pt, h_pt))
            c.drawImage(ImageReader(io.BytesIO(img_bytes)), 0, 0, width=w_pt, height=h_pt)
        c.setPageRotation(ps.pdf_rotate)  # reportlab 은 값을 다음 쪽으로 이어가므로 항상 지정
        c.showPage()
        infos.append({
            "index": i,
            "content": content_kind,
            "has_text_layer": has_text,
            "ocr_needed": content_kind == "image",
            "physical_rotation": ps.physical_rotation,
            "pdf_rotate": ps.pdf_rotate,
        })
    c.save()
    return buf.getvalue(), infos


def _pages_from_doc(doc: dict, kind: str) -> list[PageSpec]:
    return [PageSpec("blank") if els is None else PageSpec(kind, els) for els in doc["pages"]]


# --------------------------------------------------------------------------- 케이스 정의

def _doc_entries(docs: list[dict], start_pages: list[int]) -> list[dict]:
    out = []
    for d, s in zip(docs, start_pages):
        out.append({"title": d["title"], "type": d["type"], "date": d["date"],
                    "page_start": s, "page_end": s + len(d["pages"]) - 1})
    return out


def _single(doc: dict, kind: str) -> dict:
    return dict(pages=_pages_from_doc(doc, kind), docs=[doc], expected_title=doc["title"], expected_type=doc["type"],
                expected_date=doc["date"], lang=doc["lang"])


def build_cases(font: Path) -> list[dict]:
    """각 케이스: rel(입력 폴더 기준 경로), category, outcome, notes, + pages/docs 정보."""
    cases: list[dict] = []
    counter = [0]

    def scan_name() -> str:
        counter[0] += 1
        n = counter[0]
        return f"SCAN_20261105_{90100 + n * 7:06d}_{n:04d}.pdf"

    def add(category, outcome, notes, base, rel=None, **extra):
        cases.append(dict(rel=rel or scan_name(), category=category, outcome=outcome, notes=notes, **base, **extra))

    # 1. 본문 제목 있는 이미지 전용 한글 스캔
    for d, note in [(content.doc_plan(), "2쪽 한글 계획서, 표 포함"), (content.doc_meeting(), "1쪽 회의록"),
                    (content.doc_purchase(), "서식형, 제목은 본문에 있음. 완료 폴더에 같은 이름 후보가 이미 존재"),
                    (content.doc_notice(), "공문")]:
        add("titled_scan_ko", "complete", note, _single(d, "image"))
    # 2. 제목 없는 서식
    for v in (1, 2):
        add("untitled_form", "review", "본문에 제목이 없고 날짜가 없거나 애매함: 날짜·제목을 지어내면 안 됨",
            _single(content.doc_untitled_form(v), "image"))
    # 3. 영문, 4. 한영 혼합
    add("english_scan", "complete", "영문 스캔", _single(content.doc_english_summary(), "image"))
    add("english_scan", "complete", "영문 스캔(짧은 서신)", _single(content.doc_english_notice(), "image"))
    add("mixed_lang_scan", "complete", "한영 혼합 제목", _single(content.doc_mixed(1), "image"))
    add("mixed_lang_scan", "complete", "한영 혼합 제목(영문 우선)", _single(content.doc_mixed(2), "image"))
    # 5. 텍스트층 있는 PDF
    add("text_layer_pdf", "complete", "전 페이지 텍스트층. OCR 불필요(이미 있는 텍스트층을 확인해야 함)",
        _single(content.doc_budget_text(), "text"))
    add("text_layer_pdf", "complete", "영문 텍스트층 PDF", _single(content.doc_english_text(), "text"))
    # 6. 혼합 PDF (1·3쪽 텍스트, 2·4쪽 스캔)
    o = content.doc_outsourcing()
    pages = [PageSpec("text" if i in (1, 3) else "image", els) for i, els in enumerate(o["pages"], start=1)]
    add("mixed_pdf", "complete", "1·3쪽 텍스트층, 2·4쪽 이미지: 필요한 쪽만 OCR 해야 함",
        dict(pages=pages, docs=[o], expected_title=o["title"], expected_type=o["type"],
             expected_date=o["date"], lang=o["lang"]))
    # 7. 회전·빈 페이지
    r = content.doc_rotation_case()
    pages = [PageSpec("image", r["pages"][0]), PageSpec("blank"),
             PageSpec("image", r["pages"][2], physical_rotation=90),
             PageSpec("image", r["pages"][3], pdf_rotate=90), PageSpec("blank")]
    add("rotated_blank", "complete", "2·5쪽 빈 페이지, 3쪽 이미지 90도 회전, 4쪽 PDF /Rotate 90. 페이지 수 보존",
        dict(pages=pages, docs=[r], expected_title=r["title"], expected_type=r["type"],
             expected_date=r["date"], lang=r["lang"]))
    # 8. 다중 문서 묶음 (2+1+2쪽)
    docs = content.doc_bundle()
    pages = []
    for d in docs:
        pages += _pages_from_doc(d, "image")
    add("multi_doc_bundle", "review", "한 PDF 에 서로 다른 문서 3건: 합성 제목으로 조용히 확정하면 안 되고 검토 대상이어야 함",
        dict(pages=pages, docs=docs, expected_title=None, expected_type="묶음(문서 3건)", expected_date=None,
             lang="ko+en", documents=_doc_entries(docs, [1, 3, 4])))
    # 9. 이름 충돌
    d1 = content.doc_meeting("회의 결과 보고", "2026-07-14", "예산")
    d2 = content.doc_meeting("회의 결과 보고", "2026-07-14", "일정")
    add("name_collision", "complete", "다른 내용인데 제목·날짜가 같음: 제안 이름이 충돌하므로 덮어쓰지 말고 구분해야 함",
        _single(d1, "image"), collision={"kind": "same_title", "group": "same-title-1"})
    add("name_collision", "complete", "위 파일과 제목·날짜 동일, 내용 다름", _single(d2, "image"),
        collision={"kind": "same_title", "group": "same-title-1"})
    for sub, seed_text in (("sub_a", "가"), ("sub_b", "나")):
        add("name_collision", "complete", "서로 다른 하위 폴더에 같은 원래 파일명, 내용 다름",
            _single(content.doc_meeting(f"하위 폴더 시험 문서 {seed_text}", "2026-08-1" + ("1" if sub == "sub_a" else "2"), seed_text), "image"),
            rel=f"collide/{sub}/scan.pdf", collision={"kind": "same_filename_other_dir", "group": "same-name-1"})
    cases[2]["collision"] = {"kind": "preexisting_target", "group": "preexisting-1"}
    # NFD 파일명(맥에서 흔한 자모 분리) / 긴 한글 경로
    nfd = unicodedata.normalize("NFD", "한글 파일명 정규화 시험.pdf")
    add("name_collision", "complete", "디스크 상 파일명이 NFD(자모 분리). manifest 키는 NFC 로 기록",
        _single(content.doc_meeting("정규화 시험 문서", "2026-09-09", "정규화"), "image"),
        rel=nfd, name_normalization="NFD", collision={"kind": "nfd_nfc", "group": "nfd-1"})
    long_dir = "/".join(["긴한글폴더이름" * 8] * 3)
    add("name_collision", "complete", "긴 한글 경로(상대경로 약 170자)",
        _single(content.doc_meeting("긴 경로 시험 문서", "2026-09-10", "경로"), "image"),
        rel=f"{long_dir}/scan.pdf", collision={"kind": "long_path", "group": "long-1"})
    # 10. 손상·암호화
    add("corrupt_pdf", "fail", "끝부분이 잘린 손상 PDF: 성공으로 위장하면 안 되며 입력에 남아 이유가 보여야 함",
        _single(content.doc_notice(), "image") | {"expected_title": None, "expected_type": "손상", "expected_date": None,
                                                 "lang": "ko"}, corrupt=True)
    add("encrypted_pdf", "fail", "열기 암호가 걸린 PDF: 처리 불가로 분리 검토",
        _single(content.doc_english_notice(), "image") | {"expected_title": None, "expected_type": "암호화",
                                                         "expected_date": None, "lang": "en"}, encrypt=True)
    return cases


def case_sensitive_fs(probe_dir: Path) -> bool:
    """probe_dir 이 대소문자를 구분하는 파일시스템인지 판정한다."""
    probe_dir.mkdir(parents=True, exist_ok=True)
    a, b = probe_dir / "CaseProbe.tmp", probe_dir / "caseprobe.tmp"
    a.write_bytes(b"A")
    try:
        if b.exists() and b.read_bytes() == b"A":
            return False
        b.write_bytes(b"B")
        return a.read_bytes() == b"A"
    finally:
        for p in (a, b):
            if p.exists():
                p.unlink()


# --------------------------------------------------------------------------- 생성

def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def generate(out: Path, *, seed: int = DEFAULT_SEED, dpi: int = 150, font_path: str | None = None, force: bool = False) -> dict:
    out = Path(out)
    font = find_font(font_path)
    if out.exists() and any(out.iterdir()):
        if not force:
            raise SystemExit(f"출력 디렉터리가 비어 있지 않습니다: {out} (--force 로 덮어쓰기)")
        shutil.rmtree(out)
    inp = out / "input"
    inp.mkdir(parents=True)
    rng = random.Random(seed)
    files = []
    skipped = []

    for case in build_cases(font):
        data, infos = build_pdf(case["pages"], font, dpi, rng, encrypt=case.get("encrypt", False))
        if case.get("corrupt"):
            data = data[: int(len(data) * 0.55)]
            infos = []
        path = inp / case["rel"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        page_count = len(case["pages"])
        entry = {
            "file": unicodedata.normalize("NFC", case["rel"]),
            "category": case["category"],
            "expected_title": case["expected_title"],
            "expected_type": case["expected_type"],
            "expected_date": case["expected_date"],
            "expected_lang": case["lang"],
            "expected_outcome": case["outcome"],
            "pages": page_count,
            "sha256": _sha256(data),
            "size": len(data),
            "page_info": infos,
            "notes": case["notes"],
        }
        if case.get("documents"):
            entry["documents"] = case["documents"]
        if case.get("collision"):
            entry["collision"] = case["collision"]
        if case.get("name_normalization"):
            entry["name_normalization"] = case["name_normalization"]
        if case.get("corrupt"):
            entry["pages_readable"] = False
        if case.get("encrypt"):
            entry["encrypted"] = True
        files.append(entry)

    # 대소문자만 다른 이름 쌍: 대소문자 비구분 파일시스템에서는 만들 수 없으므로 판정 후 생략 기록
    probe = out / ".case_probe"
    if case_sensitive_fs(probe):
        for nm, t in (("Case_Pair.pdf", "대소문자 시험 문서 대"), ("case_pair.pdf", "대소문자 시험 문서 소")):
            d = content.doc_meeting(t, "2026-09-11", "대소문자")
            data, infos = build_pdf(_pages_from_doc(d, "image"), font, dpi, rng)
            (inp / nm).write_bytes(data)
            files.append({
                "file": nm, "category": "name_collision", "expected_title": d["title"], "expected_type": d["type"],
                "expected_date": d["date"], "expected_lang": "ko", "expected_outcome": "complete",
                "pages": 1, "sha256": _sha256(data), "size": len(data), "page_info": infos,
                "notes": "대소문자만 다른 파일명(대소문자 구분 파일시스템에서만 생성)",
                "collision": {"kind": "case_only", "group": "case-1"},
            })
    else:
        skipped.append({"case": "case_only_collision",
                        "reason": "이 파일시스템은 대소문자를 구분하지 않아 대소문자만 다른 두 파일을 만들 수 없음"})
    shutil.rmtree(probe, ignore_errors=True)

    # 완료 폴더에 이미 존재하는 파일 (물품 구매 요청서가 제안받을 이름과 충돌 시험)
    pre_dir = out / "done_preexisting"
    pre_dir.mkdir()
    pre_doc = {"id": "pre", "title": "기존 완료 파일", "type": "기존", "date": None, "lang": "ko", "pages": [
        [("org", content.ORG), ("title", "기존 완료 파일"), ("para", "완료 폴더에 이미 있는 가상 파일. 덮어쓰면 안 됨.")]]}
    pre_name = "2026-11-03_물품 구매 요청서.pdf"
    pdata, _ = build_pdf(_pages_from_doc(pre_doc, "text"), font, dpi, rng)
    (pre_dir / pre_name).write_bytes(pdata)

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "synthetic": True,
        "generator": {"name": "scaninbox_corpus", "version": __version__, "seed": seed, "dpi": dpi,
                      "pillow": PIL.__version__, "reportlab": reportlab.Version},
        "font": font_info(font),
        "files": sorted(files, key=lambda e: e["file"]),
        "preexisting": [{"file": f"done_preexisting/{pre_name}", "sha256": _sha256(pdata),
                         "collides_with": next(e["file"] for e in files if e["expected_title"] == "물품 구매 요청서"),
                         "notes": "제안 이름 `YYYY-MM-DD_제목` 이 이 이름과 같아질 수 있음: 덮어쓰면 실패"}],
        "skipped": skipped,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="합성 코퍼스 생성기(완전 가상 문서)")
    ap.add_argument("--out", default="corpus_out", help="출력 디렉터리(.gitignore 대상이어야 함)")
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--dpi", type=int, default=150)
    ap.add_argument("--font", default=None, help="한글 TTF 경로(기본: 나눔바른고딕 탐색)")
    ap.add_argument("--force", action="store_true", help="기존 출력 디렉터리를 지우고 다시 생성")
    a = ap.parse_args(argv)
    m = generate(Path(a.out), seed=a.seed, dpi=a.dpi, font_path=a.font, force=a.force)
    print(f"생성 완료: 입력 파일 {len(m['files'])}건, manifest={Path(a.out) / 'manifest.json'}")
    for s in m["skipped"]:
        print(f"생략: {s['case']} - {s['reason']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
