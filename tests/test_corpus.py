import hashlib
import json
import unicodedata
from pathlib import Path

import jsonschema
import pytest
from pypdf import PdfReader
from pypdf.errors import PyPdfError

from scaninbox_corpus.generate import generate

SCHEMA = Path(__file__).parents[1] / "scaninbox_corpus" / "schema" / "manifest.schema.json"
REQUIRED_CATEGORIES = {"titled_scan_ko", "untitled_form", "english_scan", "mixed_lang_scan", "text_layer_pdf",
                       "mixed_pdf", "rotated_blank", "multi_doc_bundle", "name_collision", "corrupt_pdf",
                       "encrypted_pdf"}


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def disk_path(inp: Path, rel: str) -> Path:
    """NFC 키로 실제 파일 찾기(NFD 파일명 포함)."""
    for p in inp.rglob("*"):
        if p.is_file() and unicodedata.normalize("NFC", p.relative_to(inp).as_posix()) == rel:
            return p
    raise AssertionError(rel)


def test_manifest_schema_and_categories(corpus):
    out, manifest = corpus
    jsonschema.validate(manifest, json.loads(SCHEMA.read_text(encoding="utf-8")))
    assert json.loads((out / "manifest.json").read_text(encoding="utf-8")) == manifest
    assert {f["category"] for f in manifest["files"]} >= REQUIRED_CATEGORIES
    assert manifest["font"]["license"] and "path" not in manifest["font"]


def test_hashes_and_pages_match_files(corpus):
    out, manifest = corpus
    for e in manifest["files"]:
        p = disk_path(out / "input", e["file"])
        assert sha(p) == e["sha256"], e["file"]
        assert p.stat().st_size == e["size"]
        if e["category"] == "corrupt_pdf":
            with pytest.raises(PyPdfError):
                PdfReader(p, strict=True).pages[0].extract_text()
            continue
        r = PdfReader(p)
        if e.get("encrypted"):
            assert r.is_encrypted
            continue
        assert len(r.pages) == e["pages"], e["file"]


def test_text_layer_and_rotation_flags_match(corpus):
    out, manifest = corpus
    for e in manifest["files"]:
        if e["category"] in ("corrupt_pdf", "encrypted_pdf"):
            continue
        r = PdfReader(disk_path(out / "input", e["file"]))
        for info in e["page_info"]:
            page = r.pages[info["index"] - 1]
            text = page.extract_text().strip()
            assert bool(text) == info["has_text_layer"], (e["file"], info["index"])
            assert (page.get("/Rotate", 0) or 0) == info["pdf_rotate"]
    # 텍스트층 PDF 는 제목이 실제로 추출된다
    for e in manifest["files"]:
        if e["category"] == "text_layer_pdf":
            r = PdfReader(disk_path(out / "input", e["file"]))
            assert e["expected_title"].replace(" ", "") in r.pages[0].extract_text().replace(" ", "")


def test_special_cases_present(corpus):
    out, manifest = corpus
    by = {}
    for e in manifest["files"]:
        by.setdefault(e["category"], []).append(e)
    # 제목 없는 서식: 기대 제목 null, 날짜 null, 검토 대상
    assert all(e["expected_title"] is None and e["expected_date"] is None and e["expected_outcome"] == "review"
               for e in by["untitled_form"])
    # 다중 문서 묶음: 페이지 범위가 전체를 빈틈없이 덮음
    b = by["multi_doc_bundle"][0]
    assert b["documents"][0]["page_start"] == 1 and b["documents"][-1]["page_end"] == b["pages"]
    # 회전·빈 페이지
    r = by["rotated_blank"][0]
    assert any(i["content"] == "blank" for i in r["page_info"])
    assert any(i["physical_rotation"] == 90 for i in r["page_info"])
    assert any(i["pdf_rotate"] == 90 for i in r["page_info"])
    # 혼합 PDF
    m = by["mixed_pdf"][0]
    assert {i["has_text_layer"] for i in m["page_info"]} == {True, False}
    # 이름 충돌: 같은 제목·날짜 쌍, 같은 파일명 다른 폴더, NFD 이름, 사전 존재 파일
    nc = by["name_collision"]
    same = [e for e in nc if e["collision"]["kind"] == "same_title"]
    assert len(same) == 2 and same[0]["expected_title"] == same[1]["expected_title"]
    assert same[0]["sha256"] != same[1]["sha256"]
    sub = [e for e in nc if e["collision"]["kind"] == "same_filename_other_dir"]
    assert len(sub) == 2 and Path(sub[0]["file"]).name == Path(sub[1]["file"]).name
    nfd = [e for e in nc if e["collision"]["kind"] == "nfd_nfc"][0]
    assert any(p.name != unicodedata.normalize("NFC", p.name) for p in (out / "input").iterdir())
    assert nfd["file"] == unicodedata.normalize("NFC", nfd["file"])
    pre = manifest["preexisting"][0]
    assert (out / pre["file"]).is_file() and sha(out / pre["file"]) == pre["sha256"]


def test_no_real_paths_or_home_in_manifest(corpus):
    out, _ = corpus
    text = (out / "manifest.json").read_text(encoding="utf-8")
    assert str(Path.home()) not in text and "/Users/" not in text


def test_generation_is_deterministic(tmp_path, font_available):
    a = generate(tmp_path / "a", dpi=60)
    b = generate(tmp_path / "b", dpi=60)
    assert [(f["file"], f["sha256"]) for f in a["files"]] == [(f["file"], f["sha256"]) for f in b["files"]]


def test_refuses_nonempty_output_without_force(tmp_path, font_available):
    generate(tmp_path / "o", dpi=60)
    with pytest.raises(SystemExit):
        generate(tmp_path / "o", dpi=60)
