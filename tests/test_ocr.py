"""ocr.OcrmypdfEngine 시험.

- 단위 시험: 가짜 ``ocrmypdf``·``tesseract`` 로 종료코드 매핑, 쪽별 검증, 시간 초과·취소 시 프로세스 트리 종료를
  OS 와 무관하게 시험한다(OCRmyPDF 설치 불필요).
- 통합 시험: 실제 OCRmyPDF 17.x + Tesseract(kor+eng)가 있을 때만 돈다(없으면 skip).
  환경은 ``SCANINBOX_TESSDATA``(kor 포함 tessdata 폴더), ``SCANINBOX_TESSERACT_DIR``(tesseract 폴더, 선택)로 지정한다.
"""
from __future__ import annotations

import difflib
import os
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
from pathlib import Path

import pytest
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas

from scaninbox.contracts import OcrStatus
from scaninbox.ocr import (DEFAULT_ROTATE_THRESHOLD, OcrEnvironmentError, OcrmypdfEngine, page_text_quality,
                           suspect_pages)

# ---- 가짜 외부 명령 ------------------------------------------------------------------------------

FAKE_OCRMYPDF = r'''
import os, subprocess, sys, time
from pypdf import PdfReader
from reportlab.pdfgen import canvas
args = sys.argv[1:]
if "--version" in args:
    print("17.13.0"); sys.exit(0)
mode = os.environ.get("FAKE_MODE", "ok")
src, dst = args[-2], args[-1]
open(os.environ["FAKE_ARGS_FILE"], "w", encoding="utf-8").write("\n".join(args)) if os.environ.get("FAKE_ARGS_FILE") else None
if mode.startswith("rc"):
    sys.stderr.write("simulated failure for " + src + "\n"); sys.exit(int(mode[2:]))
if mode == "hang":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    open(os.environ["FAKE_PID_FILE"], "w").write(str(child.pid))
    time.sleep(120); sys.exit(0)
if mode == "nofile":
    sys.exit(0)
n = len(PdfReader(src).pages)
blank = {int(x) for x in os.environ.get("FAKE_BLANK", "").split(",") if x}
if mode == "pagecount":
    n += 1
thr = args[args.index("--rotate-pages-threshold") + 1] if "--rotate-pages-threshold" in args else "?"
if os.environ.get("FAKE_CALLS_FILE"):
    open(os.environ["FAKE_CALLS_FILE"], "a").write(thr + "\n")
c = canvas.Canvas(dst)
for i in range(1, n + 1):
    if i not in blank:
        c.drawString(72, 700, f"Recognized text on page {i} thr={thr} with enough characters here.")
    c.showPage()
c.save()
'''

FAKE_TESSERACT = r'''
import os, sys
if "--list-langs" in sys.argv:
    d = os.environ.get("FAKE_TESSDATA", "/tmp/none")
    print('List of available languages in "%s/" (3):' % d)
    for l in os.environ.get("FAKE_LANGS", "eng osd").split(): print(l)
elif "--version" in sys.argv:
    print("tesseract 5.5.3")
'''


def _launcher(dirpath: Path, name: str, source: str) -> Path:
    py = dirpath / f"{name}_impl.py"
    py.write_text(source, encoding="utf-8")
    if os.name == "nt":
        exe = dirpath / f"{name}.cmd"
        exe.write_text(f'@echo off\r\n"{sys.executable}" "{py}" %*\r\n', encoding="utf-8")
    else:
        exe = dirpath / name
        exe.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{py}" "$@"\n', encoding="utf-8")
        exe.chmod(0o755)
    return py


@pytest.fixture
def fake_env(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    ocr_py = _launcher(bindir, "fakeocr", FAKE_OCRMYPDF)
    _launcher(bindir, "tesseract", FAKE_TESSERACT)
    monkeypatch.setenv("FAKE_LANGS", "eng kor osd")
    monkeypatch.setenv("FAKE_TESSDATA", str(tmp_path / "tessdata"))
    (tmp_path / "tessdata").mkdir()
    (tmp_path / "tessdata" / "kor.traineddata").write_bytes(b"kor-fake")
    eng = OcrmypdfEngine([sys.executable, str(ocr_py)], tesseract_dir=bindir)
    return eng, tmp_path


def _image_only_pdf(path: Path, pages: int = 2, rotate: dict[int, int] | None = None) -> Path:
    c = canvas.Canvas(str(path))
    for _ in range(pages):
        c.rect(50, 50, 200, 100, fill=1)  # 그림만 있고 텍스트층이 없는 쪽
        c.showPage()
    c.save()
    if rotate:
        r = PdfReader(str(path))
        w = PdfWriter()
        for i, pg in enumerate(r.pages, start=1):
            if i in rotate:
                pg.rotate(rotate[i])
            w.add_page(pg)
        with open(path, "wb") as f:
            w.write(f)
    return path


def _text_pdf(path: Path, pages: int = 1) -> Path:
    c = canvas.Canvas(str(path))
    for i in range(pages):
        c.drawString(72, 700, f"This page already has a long enough text layer number {i}.")
        c.showPage()
    c.save()
    return path


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # 좀비는 살아 있는 것으로 보이지 않게 한다
    r = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(r) and not r.startswith("Z")


# ---- 단위 시험 -----------------------------------------------------------------------------------

def test_ok_verifies_pages_and_records_versions(fake_env, tmp_path):
    eng, tp = fake_env
    src = _image_only_pdf(tp / "in.pdf", pages=3)
    res = eng.run(src, tp / "out")
    assert res.status is OcrStatus.OK
    assert (res.page_count_in, res.page_count_out) == (3, 3)
    assert res.empty_pages == [] and len(res.text_by_page) == 3
    assert res.output and res.output.parent == tp / "out" and res.output.is_file()
    # 엔진·언어팩 버전이 기록된다(언어팩은 tessdata 안의 파일 해시 앞 8자)
    assert "ocrmypdf 17.13.0" in res.engine and "tesseract 5.5.3" in res.engine and "kor+eng" in res.engine
    assert "kor:" in res.engine


def test_default_options_and_forbidden(fake_env, tmp_path, monkeypatch):
    eng, tp = fake_env
    argsf = tp / "args.txt"
    monkeypatch.setenv("FAKE_ARGS_FILE", str(argsf))
    eng.run(_image_only_pdf(tp / "in.pdf"), tp / "out")
    args = argsf.read_text(encoding="utf-8").split("\n")
    assert "--deskew" in args and "--skip-text" in args and "--rotate-pages" in args
    assert args[args.index("--rotate-pages-threshold") + 1] == str(DEFAULT_ROTATE_THRESHOLD)
    assert not any(a.startswith("--oversample") or a.startswith("--tesseract-pagesegmode") for a in args)
    assert "-l" in args and args[args.index("-l") + 1] == "kor+eng"
    with pytest.raises(ValueError):
        OcrmypdfEngine(extra_args=["--oversample", "300"])
    with pytest.raises(ValueError):
        OcrmypdfEngine(extra_args=["--tesseract-pagesegmode=6"])


def test_blank_page_is_partial_and_reported(fake_env, tmp_path, monkeypatch):
    eng, tp = fake_env
    monkeypatch.setenv("FAKE_BLANK", "2,4")
    res = eng.run(_image_only_pdf(tp / "in.pdf", pages=4), tp / "out")
    assert res.status is OcrStatus.PARTIAL
    assert res.empty_pages == [2, 4]
    assert "2,4" in res.reason


def test_rotate_page_with_zero_text_is_partial_even_with_exit_0(fake_env, tmp_path, monkeypatch):
    """/Rotate 쪽이 종료코드 0 인데 텍스트 0자인 G0 사례. 출력 PDF 의 /Rotate 값이 사유에 남는다."""
    eng, tp = fake_env
    monkeypatch.setenv("FAKE_BLANK", "2")
    src = _image_only_pdf(tp / "in.pdf", pages=2, rotate={2: 90})
    # 가짜 OCR 은 출력에 /Rotate 를 복사하지 않으므로, 출력의 /Rotate 를 확인하는 경로는 아래 통합 시험이 맡는다.
    res = eng.run(src, tp / "out")
    assert res.status is OcrStatus.PARTIAL and res.empty_pages == [2]


def test_page_count_change_is_failure_and_output_removed(fake_env, tmp_path, monkeypatch):
    eng, tp = fake_env
    monkeypatch.setenv("FAKE_MODE", "pagecount")
    res = eng.run(_image_only_pdf(tp / "in.pdf", pages=2), tp / "out")
    assert res.status is OcrStatus.FAILED and "쪽 수" in res.reason
    assert res.output is None
    assert not list((tp / "out").glob("*.pdf"))


def test_missing_output_file_is_failure(fake_env, tmp_path, monkeypatch):
    eng, tp = fake_env
    monkeypatch.setenv("FAKE_MODE", "nofile")
    res = eng.run(_image_only_pdf(tp / "in.pdf"), tp / "out")
    assert res.status is OcrStatus.FAILED


@pytest.mark.parametrize("rc,status", [(2, OcrStatus.CORRUPT), (8, OcrStatus.ENCRYPTED),
                                       (9, OcrStatus.FAILED), (3, OcrStatus.FAILED), (15, OcrStatus.FAILED)])
def test_exit_code_mapping(fake_env, tmp_path, monkeypatch, rc, status):
    eng, tp = fake_env
    monkeypatch.setenv("FAKE_MODE", f"rc{rc}")
    res = eng.run(_image_only_pdf(tp / "in.pdf"), tp / "out")
    assert res.status is status and res.output is None
    assert "in.pdf" not in res.reason  # 입력 경로를 사유에 그대로 싣지 않는다(마스킹)


def test_precheck_corrupt_and_encrypted(fake_env, tmp_path):
    eng, tp = fake_env
    good = _image_only_pdf(tp / "good.pdf")
    bad = tp / "bad.pdf"
    bad.write_bytes(good.read_bytes()[:150])
    assert eng.run(bad, tp / "out").status is OcrStatus.CORRUPT
    empty = tp / "empty.pdf"
    empty.write_bytes(b"")
    assert eng.run(empty, tp / "out").status is OcrStatus.CORRUPT
    w = PdfWriter()
    w.append(PdfReader(str(good)))
    w.encrypt("user-pw", "owner-pw")
    enc = tp / "enc.pdf"
    with open(enc, "wb") as f:
        w.write(f)
    res = eng.run(enc, tp / "out")
    assert res.status is OcrStatus.ENCRYPTED and res.output is None
    assert not list((tp / "out").glob("*.pdf")) if (tp / "out").exists() else True


def test_already_text_skips_ocr(fake_env, tmp_path, monkeypatch):
    eng, tp = fake_env
    monkeypatch.setenv("FAKE_MODE", "rc15")  # OCR 이 호출되면 실패하도록 해서 호출 여부를 확인한다
    src = _text_pdf(tp / "t.pdf", pages=2)
    res = eng.run(src, tp / "out")
    assert res.status is OcrStatus.ALREADY_TEXT
    assert res.page_count_in == res.page_count_out == 2 and len(res.text_by_page) == 2
    assert res.output and res.output.read_bytes() == src.read_bytes()


def test_missing_language_pack_gives_install_guidance(fake_env, tmp_path, monkeypatch):
    eng, tp = fake_env
    monkeypatch.setenv("FAKE_LANGS", "eng osd")
    with pytest.raises(OcrEnvironmentError) as ei:
        eng.check_environment("kor+eng")
    msg = str(ei.value)
    assert "kor" in msg and "tessdata_fast" in msg and "TESSDATA_PREFIX" in msg
    # run() 은 예외 대신 FAILED + 같은 안내를 돌려준다(OCR 을 건너뛰고 성공하는 경로 없음)
    res = OcrmypdfEngine(eng._cmd, tesseract_dir=eng._tess_dir).run(_image_only_pdf(tp / "in.pdf"), tp / "out")
    assert res.status is OcrStatus.FAILED and "tessdata_fast" in res.reason


def test_timeout_kills_process_tree(fake_env, tmp_path, monkeypatch):
    eng, tp = fake_env
    pidfile = tp / "child.pid"
    monkeypatch.setenv("FAKE_MODE", "hang")
    monkeypatch.setenv("FAKE_PID_FILE", str(pidfile))
    t0 = time.monotonic()
    res = eng.run(_image_only_pdf(tp / "in.pdf"), tp / "out", timeout_s=3)
    assert time.monotonic() - t0 < 30
    assert res.status is OcrStatus.FAILED and "시간 초과" in res.reason
    child = int(pidfile.read_text())
    deadline = time.monotonic() + 10
    while _pid_alive(child) and time.monotonic() < deadline:
        time.sleep(0.2)
    assert not _pid_alive(child), "자식 프로세스가 고아로 남음"


def test_cancel_kills_process_tree(fake_env, tmp_path, monkeypatch):
    eng, tp = fake_env
    pidfile = tp / "child.pid"
    monkeypatch.setenv("FAKE_MODE", "hang")
    monkeypatch.setenv("FAKE_PID_FILE", str(pidfile))
    ev = threading.Event()
    threading.Timer(2.0, ev.set).start()
    res = eng.run(_image_only_pdf(tp / "in.pdf"), tp / "out", timeout_s=120, cancel=ev)
    assert res.status is OcrStatus.FAILED and res.reason == "취소됨"
    child = int(pidfile.read_text())
    deadline = time.monotonic() + 10
    while _pid_alive(child) and time.monotonic() < deadline:
        time.sleep(0.2)
    assert not _pid_alive(child)


def test_log_has_no_body_text(fake_env, tmp_path, caplog):
    eng, tp = fake_env
    caplog.set_level("DEBUG", logger="scaninbox.ocr")
    eng.run(_image_only_pdf(tp / "in.pdf"), tp / "out")
    assert "Recognized text" not in caplog.text


def test_retry_with_lower_threshold_when_suspect_pages(fake_env, tmp_path, monkeypatch):
    """첫 결과에 의심 쪽이 있으면 낮은 임계값으로 재실행해 문제 쪽이 더 적은 결과를 채택한다."""
    import scaninbox.ocr as ocr_mod
    eng, tp = fake_env
    calls = tp / "calls.txt"
    monkeypatch.setenv("FAKE_CALLS_FILE", str(calls))
    # 임계값 1.2 로 만든 출력은 2쪽이 의심, 0.5 로 만든 출력은 정상이라고 판정하는 가짜 판정기
    monkeypatch.setattr(ocr_mod, "suspect_pages", lambda texts: [2] if any("thr=1.2" in t for t in texts) else [])
    res = eng.run(_image_only_pdf(tp / "in.pdf", pages=2), tp / "out")
    assert calls.read_text().split() == ["1.2", "0.5"]
    assert res.status is OcrStatus.OK and "0.5" in res.reason and "rotate-threshold=0.5" in res.engine
    assert len(list((tp / "out").glob("*.pdf"))) == 1  # 채택되지 않은 첫 출력은 지워진다


def test_retry_not_adopted_when_not_better(fake_env, tmp_path, monkeypatch):
    import scaninbox.ocr as ocr_mod
    eng, tp = fake_env
    calls = tp / "calls.txt"
    monkeypatch.setenv("FAKE_CALLS_FILE", str(calls))
    monkeypatch.setattr(ocr_mod, "suspect_pages", lambda texts: [2])  # 어떤 임계값에서도 의심 쪽 1개
    res = eng.run(_image_only_pdf(tp / "in.pdf", pages=2), tp / "out")
    assert calls.read_text().split() == ["1.2", "0.5"]
    assert res.status is OcrStatus.PARTIAL and "rotate-threshold=1.2" in res.engine
    assert len(list((tp / "out").glob("*.pdf"))) == 1


def test_no_retry_without_suspect_or_when_disabled(fake_env, tmp_path, monkeypatch):
    import scaninbox.ocr as ocr_mod
    eng, tp = fake_env
    calls = tp / "calls.txt"
    monkeypatch.setenv("FAKE_CALLS_FILE", str(calls))
    monkeypatch.setenv("FAKE_BLANK", "2")  # 빈 쪽만 있는 경우는 재시도로 나아지지 않는다
    eng.run(_image_only_pdf(tp / "in.pdf", pages=2), tp / "out")
    assert calls.read_text().split() == ["1.2"]
    calls.unlink()
    monkeypatch.setattr(ocr_mod, "suspect_pages", lambda texts: [1])
    eng2 = OcrmypdfEngine(eng._cmd, tesseract_dir=eng._tess_dir, retry_rotate_thresholds=())
    eng2.run(_image_only_pdf(tp / "in2.pdf", pages=2), tp / "out2")
    assert calls.read_text().split() == ["1.2"]


# ---- 텍스트 품질 판정(쓰레기 쪽) ---------------------------------------------------------------------

# G0 에서 90도 회전된 합성 쪽을 임계값 12 로 읽었을 때 나온 쓰레기 텍스트(내용은 합성 문서의 오인식).
GARBAGE = ("튜 1 아이 kein lolel2 SBR S YOO \n“Sié WSRIS 506 Iclety | \n\x00리\x00미\x00 0 BR BRY SloL BR Bc Stvlolm lo LH ERY SOB) \n"
           "tec lylyilo \n")
GOOD_KO = ("예시시 가상과\n[ 옆으로 AWS 쪽 ] 이 페이지는 가로 방향 종이로 스캔된 것처럼 이미지 자체 가 90 도 회전되어 있음. \n"
           "OCRE SHS 감지하여올바르게읽어야함 . \n")
PAGE1 = "예시시 가상과\n문서번호 가 상 -0001 / 2026. 3. 12. \n추진 배경: 가상의시설운영효율을높이기위하여연간사업일정을 정리함. \n"
EN_PAGE = "Example Facilities Office \nApril 24, 2026 \nVisitor Access Guide \nVisitors must sign in at the front desk. This guide is fictional. \n"


def test_garbage_page_is_flagged_only_when_it_deviates_from_the_document():
    assert suspect_pages([PAGE1, GARBAGE]) == [2]
    assert suspect_pages([PAGE1, GOOD_KO]) == []
    assert suspect_pages([PAGE1, PAGE1, EN_PAGE]) == []  # 한글 문서에 영문 쪽이 섞여도 오탐하지 않음
    assert suspect_pages([GARBAGE]) == []  # 비교 대상이 없는 한 쪽짜리는 절대 지표만 본다
    assert page_text_quality("") == 0.0


# ---- 통합 시험(실제 OCRmyPDF) ------------------------------------------------------------------------

def _real_engine(langs: str = "kor+eng", **kw):
    tess = os.environ.get("SCANINBOX_TESSDATA")
    tdir = os.environ.get("SCANINBOX_TESSERACT_DIR")
    eng = OcrmypdfEngine(tessdata_dir=Path(tess) if tess else None, tesseract_dir=Path(tdir) if tdir else None, **kw)
    try:
        eng.check_environment(langs)
    except OcrEnvironmentError as e:
        pytest.skip(f"실제 OCR 환경 없음: {str(e)[:80]}")
    return eng


@pytest.fixture(scope="module")
def corpus150(tmp_path_factory):
    from scaninbox_corpus.fonts import find_font
    from scaninbox_corpus.generate import generate
    from scaninbox_corpus.pathutil import extended
    try:
        find_font()
    except FileNotFoundError:
        pytest.skip("한글 TTF 폰트 없음(SCANINBOX_FONT 지정 필요)")
    out = extended(tmp_path_factory.mktemp("corpus150") / "out")
    return out, generate(out, dpi=150)


def _by_category(corpus150, cat):
    out, manifest = corpus150
    key = next(f["file"] for f in manifest["files"] if f["category"] == cat)
    return next(p for p in (Path(out) / "input").rglob("*") if p.is_file()
                and unicodedata.normalize("NFC", p.relative_to(Path(out) / "input").as_posix()) == key)


def _norm(s: str) -> str:
    return "".join(unicodedata.normalize("NFC", s).replace("\x00", "").split())


def _recall(truth: str, text: str) -> float:
    t, x = _norm(truth), _norm(text)
    sm = difflib.SequenceMatcher(None, t, x, autojunk=False)
    return sum(b.size for b in sm.get_matching_blocks()) / max(1, len(t))


def _page_truth(doc: dict, page_no: int) -> str:
    return " ".join(str(el[1]) for el in doc["pages"][page_no - 1] if el[0] in ("para", "title"))


def test_real_corpus_statuses(corpus150, tmp_path):
    eng = _real_engine()
    exp = {"titled_scan_ko": OcrStatus.OK, "text_layer_pdf": OcrStatus.ALREADY_TEXT,
           "rotated_blank": OcrStatus.PARTIAL, "corrupt_pdf": OcrStatus.CORRUPT, "encrypted_pdf": OcrStatus.ENCRYPTED}
    for cat, status in exp.items():
        src = _by_category(corpus150, cat)
        res = eng.run(src, tmp_path / "out", timeout_s=300)
        assert res.status is status, (cat, res.status, res.reason)
        if status in (OcrStatus.OK, OcrStatus.ALREADY_TEXT, OcrStatus.PARTIAL):
            assert res.page_count_in == res.page_count_out == len(PdfReader(str(src)).pages)
            assert "tesseract" in res.engine or status is OcrStatus.ALREADY_TEXT
    # 혼합 PDF: 이미지 쪽에도 텍스트가 생겨 전 쪽 OK
    res = eng.run(_by_category(corpus150, "mixed_pdf"), tmp_path / "out", timeout_s=300)
    assert res.status is OcrStatus.OK and res.empty_pages == []
    # 빈 쪽 2개는 텍스트 0자 -> partial 로 드러난다
    res = eng.run(_by_category(corpus150, "rotated_blank"), tmp_path / "out", timeout_s=300)
    assert {2, 5} <= set(res.empty_pages)


def test_rotate_behavior_with_default_engine(corpus150, tmp_path):
    """이미지가 90도 회전된 3쪽이 읽을 수 있게 교정돼야 하고, 빈 쪽(2·5)은 텍스트가 없어야 한다.

    첫 실행 임계값(1.2)만으로 교정되는지는 플랫폼에 따라 다르다(macOS/Tesseract 5.5.3 은 교정, Windows/5.4.0 은
    교정 실패 -> 재시도 임계값 0.5 로 교정). 어느 쪽이든 기본 엔진의 최종 결과는 3쪽이 교정되어야 한다."""
    from scaninbox_corpus.content import doc_rotation_case
    src = _by_category(corpus150, "rotated_blank")
    doc = doc_rotation_case()
    res = _real_engine().run(src, tmp_path / "out", timeout_s=600)
    assert _recall(_page_truth(doc, 3), res.text_by_page[2]) >= 0.6, res.reason
    assert 3 not in suspect_pages(res.text_by_page)
    assert res.text_by_page[1] == "" and res.text_by_page[4] == ""
    # PDF /Rotate 90 쪽(4쪽)은 재시도 후에도 읽혀야 한다(임계값이 너무 낮으면 악화되는 쪽)
    assert _recall(_page_truth(doc, 4), res.text_by_page[3]) >= 0.6
    # 재시도를 끈 엔진도 결과를 낸다. 플랫폼에 따라 3쪽이 의심 쪽으로 드러나거나(Windows) 교정된다(macOS).
    res0 = _real_engine(retry_rotate_thresholds=()).run(src, tmp_path / "out", timeout_s=600)
    assert res0.status is OcrStatus.PARTIAL and {2, 5} <= set(res0.empty_pages)
