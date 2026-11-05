"""G0 시험용 접합 스크립트: organize 2단계 JSONL 출력과 작업 폴더 상태에서 reconcile 용 ledger 를 만든다.

organize 는 입력↔최종본 매핑 기록(ledger)을 남기지 않고 완료된 입력도 정리하지 않는다. 이 스크립트가 그 빈틈을 메우는
"확장" 부분이며, 대상 도구가 스스로 못 하는 일을 보여 주려고 별도 파일로 둔다.

    python make_ledger.py <G0RUN> <manifest.json> <stage2.jsonl> -o ledger.json [--cleanup-input]

규칙: work/ 의 파일명은 입력 SHA-256 이다(1단계 설정). 2단계 JSONL 의 `Move to <dest>` 메시지로 최종 경로를 얻는다.
  rule titled -> completed, rule untitled-to-review -> held, 해시 이름의 work 사본이 없는 입력 -> failed.
--cleanup-input: 완료 건 중 originals/ 백업 해시가 일치하는 것만 입력 폴더에서 삭제한다(되돌리기 불가 동작이므로 시험용 복사본에서만).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import unicodedata
from pathlib import Path


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run")
    ap.add_argument("manifest")
    ap.add_argument("jsonl")
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--cleanup-input", action="store_true")
    a = ap.parse_args()
    run = Path(a.run)
    manifest = json.loads(Path(a.manifest).read_text(encoding="utf-8"))
    by_hash = {e["sha256"]: e["file"] for e in manifest["files"]}
    dest_of: dict[str, tuple[str, str]] = {}
    for line in Path(a.jsonl).read_text(encoding="utf-8").splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "MSG" and ev.get("sender") == "move" and ev.get("msg", "").startswith("Move to "):
            h = Path(ev["path"]).stem
            dest_of[h] = (ev["msg"][len("Move to "):], ev["rule_name"])  # 충돌 시 마지막(실제) 목적지가 남는다
    entries = []
    for h, rel in by_hash.items():
        if h in dest_of:
            dest, rule = dest_of[h]
            dp = Path(dest)
            if rule == "titled":
                entries.append({"input": rel, "status": "completed", "original_sha256": h,
                                "final_path": dp.relative_to(run / "done").as_posix(), "final_sha256": sha(dp)})
            else:
                entries.append({"input": rel, "status": "held", "reason": "제목 규칙 불일치(review 폴더로 이동)"})
        else:
            entries.append({"input": rel, "status": "failed", "reason": "OCR 사본 없음(ocrmypdf 오류)"})
    if a.cleanup_input:
        for e in entries:
            if e["status"] != "completed":
                continue
            key = unicodedata.normalize("NFC", e["input"])
            src = next((p for p in (run / "inbox").rglob("*") if p.is_file()
                        and unicodedata.normalize("NFC", p.relative_to(run / "inbox").as_posix()) == key), None)
            bak = run / "originals" / f"{e['original_sha256']}.pdf"
            if src and bak.exists() and sha(bak) == e["original_sha256"] == sha(src):
                src.unlink()
    Path(a.out).write_text(json.dumps({"entries": entries}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"ledger {len(entries)}건 기록")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
