"""G0 시험용: OCRmyPDF 를 합성 코퍼스 입력에 돌리고 manifest 정답과 대조한다.

    OCRMYPDF=~/scan-inbox-g0/venv/bin/ocrmypdf TESSDATA_PREFIX=~/scan-inbox-g0/tessdata \
      python docs/g0/eval_ocr.py <corpus_out> <결과 출력 디렉터리> [-- 추가 ocrmypdf 옵션]

입력(<corpus_out>/input)은 읽기만 하고 결과는 별도 디렉터리에 만든다.
측정: 종료 코드, 소요 시간, 최대 메모리(RSS), 용량 변화, 페이지 수 보존, 제목·본문 텍스트 검색 가능 여부.
"""
from __future__ import annotations

import difflib
import json
import os
import resource
import subprocess
import sys
import time
import unicodedata
from pathlib import Path

from pypdf import PdfReader


def norm(s: str) -> str:
    return "".join(unicodedata.normalize("NFC", s).replace("\x00", "").split())


def coverage(title: str, text: str) -> float:
    """제목 글자가 OCR 텍스트에 순서대로 얼마나 들어 있는지(0~1). 완전 일치면 1.0."""
    sm = difflib.SequenceMatcher(None, title, text, autojunk=False)
    return round(sum(b.size for b in sm.get_matching_blocks()) / max(1, len(title)), 2)


def main() -> int:
    argv = sys.argv[1:]
    extra: list[str] = []
    if "--" in argv:
        i = argv.index("--")
        argv, extra = argv[:i], argv[i + 1:]
    corpus, outdir = Path(argv[0]), Path(argv[1])
    manifest = json.loads((corpus / "manifest.json").read_text(encoding="utf-8"))
    ocr = os.environ.get("OCRMYPDF", "ocrmypdf")
    outdir.mkdir(parents=True, exist_ok=True)
    rows = []
    for e in manifest["files"]:
        src = next(p for p in (corpus / "input").rglob("*") if p.is_file()
                   and unicodedata.normalize("NFC", p.relative_to(corpus / "input").as_posix()) == e["file"])
        dst = outdir / (e["file"].replace("/", "__"))
        cmd = [ocr, "-l", "kor+eng", "--skip-text", "--rotate-pages", *extra, str(src), str(dst)]
        before = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
        t0 = time.time()
        p = subprocess.run(cmd, capture_output=True, text=True)
        dt = time.time() - t0
        rss = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss  # macOS: bytes, 누적 최대
        row = {"file": e["file"], "category": e["category"], "exit": p.returncode, "sec": round(dt, 1),
               "in_kb": round(src.stat().st_size / 1024), "stderr_tail": p.stderr.strip().splitlines()[-1:] }
        if p.returncode == 0 and dst.exists():
            r = PdfReader(dst)
            row["out_kb"] = round(dst.stat().st_size / 1024)
            row["pages_ok"] = len(r.pages) == e["pages"]
            texts = [pg.extract_text() or "" for pg in r.pages]
            row["title_found"] = (norm(e["expected_title"]) in norm(" ".join(texts))) if e["expected_title"] else None
            # 이미지 쪽에 텍스트가 실제로 생겼는지
            row["title_coverage"] = coverage(norm(e["expected_title"]), norm(" ".join(texts))) if e["expected_title"] else None
            row["ocr_pages_with_text"] = [i["index"] for i in e["page_info"]
                                         if i["ocr_needed"] and norm(texts[i["index"] - 1])]
            row["ocr_pages_expected"] = [i["index"] for i in e["page_info"] if i["ocr_needed"]]
            row["text_sample"] = norm(texts[0])[:60] if texts else ""
        rows.append(row)
    (outdir / "eval.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    for r in rows:
        print(json.dumps(r, ensure_ascii=False))
    print("peak child RSS(MB)", round(resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss / 1024 / 1024))
    return 0


if __name__ == "__main__":
    sys.exit(main())
