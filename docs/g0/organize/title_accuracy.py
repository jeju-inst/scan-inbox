"""G0 시험용: ledger 의 완료 건 최종 파일명(제목 부분)을 manifest 정답 제목과 비교한다.

    python title_accuracy.py <manifest.json> <ledger.json>

판정(공백 무시): exact=정답과 같음, wrong=다른 이름으로 완료됨(오답), review_ok=검토 대상이 완료로 처리되지 않고 보류/실패에 남음,
review_missed=검토 대상(제목 없음·묶음)인데 자동 완료됨.
"""
from __future__ import annotations

import json
import re
import sys
import unicodedata
from pathlib import Path


def n(s: str) -> str:
    return "".join(unicodedata.normalize("NFC", s).split())


def main() -> int:
    manifest = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    ledger = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
    exp = {e["file"]: e for e in manifest["files"]}
    counts: dict[str, int] = {}
    for ent in ledger["entries"]:
        e = exp[unicodedata.normalize("NFC", ent["input"])]
        if ent["status"] == "completed":
            stem = Path(ent["final_path"]).stem
            if e["expected_outcome"] == "review":
                verdict = "review_missed"
            elif e["expected_outcome"] == "fail":
                verdict = "fail_masked"
            elif e["expected_title"] and n(stem) == n(e["expected_title"]):
                verdict = "exact"
            elif e["expected_title"] and re.fullmatch(re.escape(n(e["expected_title"])) + r"\d+", n(stem)):
                verdict = "exact"  # 충돌 숫자 접미사(" 2")만 붙은 경우. 임의 문자열이 붙으면 wrong
            else:
                verdict = "wrong"
            print(f"{verdict:14s} {e['category']:18s} 기대={e['expected_title']!r} 결과={stem!r}")
        else:
            verdict = "review_ok" if e["expected_outcome"] in ("review", "fail") else "wrongly_held"
            print(f"{verdict:14s} {e['category']:18s} status={ent['status']}")
        counts[verdict] = counts.get(verdict, 0) + 1
    print(counts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
