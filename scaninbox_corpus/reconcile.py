"""`입력 등록 = 완료 + 보류 + 실패 + 미착수` 대조.

    python -m scaninbox_corpus.reconcile --manifest corpus_out/manifest.json \
        --ledger run_ledger.json --input-dir corpus_out/input --done-dir done/

대상 도구(OSS 조합 또는 자체 앱)가 남긴 실행 기록(ledger)을 정답 manifest 와 실제 폴더 상태에 대조한다.
기록 형식은 schema/ledger.schema.json 을 따른다:
  {"entries": [{"input": "<입력 상대경로>", "status": "completed|held|failed",
                "original_sha256": "...", "final_path": "<완료 폴더 상대경로>", "final_sha256": "...", "reason": "..."}]}
ledger 에 없는 입력은 `미착수`로 센다. 위반이 하나라도 있으면 종료 코드 1.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import unicodedata
from pathlib import Path

STATUSES = ("completed", "held", "failed")


def _nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _index(root: Path) -> dict[str, Path]:
    """NFC 정규화한 상대경로 -> 실제 경로 (NFD 파일명도 같은 키로 찾기 위함)."""
    out: dict[str, Path] = {}
    if root and root.is_dir():
        for p in root.rglob("*"):
            if p.is_file() and not p.is_symlink():
                out[_nfc(p.relative_to(root).as_posix())] = p
    return out


def reconcile(manifest: dict, ledger: dict, input_dir: Path, done_dir: Path | None) -> dict:
    registered = {e["file"]: e for e in manifest["files"]}
    errors: list[str] = []
    entries: dict[str, dict] = {}
    for ent in ledger.get("entries", []):
        key = _nfc(ent.get("input", ""))
        if ent.get("status") not in STATUSES:
            errors.append(f"[상태 오류] {key}: status={ent.get('status')!r} (completed|held|failed 만 허용)")
            continue
        if key in entries:
            errors.append(f"[중복 기록] {key}: ledger 에 두 번 이상 나타남")
            continue
        if key not in registered:
            errors.append(f"[미등록 입력] {key}: manifest 에 없는 파일이 처리됨")
            continue
        entries[key] = ent

    groups = {s: [k for k, e in entries.items() if e["status"] == s] for s in STATUSES}
    unstarted = sorted(set(registered) - set(entries))
    counts = {"registered": len(registered), "completed": len(groups["completed"]), "held": len(groups["held"]),
              "failed": len(groups["failed"]), "unstarted": len(unstarted)}
    total = counts["completed"] + counts["held"] + counts["failed"] + counts["unstarted"]
    if total != counts["registered"]:
        errors.append(f"[건수 불일치] 완료+보류+실패+미착수={total} != 입력 등록 {counts['registered']}")

    in_idx = _index(input_dir)
    done_idx = _index(done_dir) if done_dir else {}
    extra = sorted(set(in_idx) - set(registered))
    for k in extra:
        errors.append(f"[입력에 미등록 파일] {k}: manifest 에 없는 파일이 입력 폴더에 있음")

    def present_intact(k: str) -> None:
        p = in_idx.get(k)
        if p is None:
            errors.append(f"[입력 소실] {k}: 완료가 아닌데 입력 폴더에서 사라짐")
        elif _sha256(p) != registered[k]["sha256"]:
            errors.append(f"[입력 변경] {k}: 입력 파일 해시가 원본과 다름")

    pre_hash = {Path(p["file"]).name: p["sha256"] for p in manifest.get("preexisting", [])}
    finals: dict[str, str] = {}
    for k in groups["completed"]:
        e = entries[k]
        if k in in_idx:
            errors.append(f"[입력 미정리] {k}: 완료인데 입력 폴더에 그대로 남아 있음")
        if e.get("original_sha256") != registered[k]["sha256"]:
            errors.append(f"[원본 해시 불일치] {k}: 기록된 원본 해시가 manifest 와 다름")
        fp, fh = e.get("final_path"), e.get("final_sha256")
        if not fp or not fh:
            errors.append(f"[완료 기록 부족] {k}: final_path·final_sha256 필요")
            continue
        if registered[k]["expected_outcome"] == "fail":
            errors.append(f"[실패 위장] {k}: 처리 불가여야 하는 파일이 완료로 기록됨")
        fkey = _nfc(fp)  # 중복 검출과 파일 조회에 같은 정규화 키를 쓴다
        if fkey in finals:
            errors.append(f"[최종본 경로 중복] {fp}: {finals[fkey]} 와 {k} 가 같은 최종 경로 사용(덮어쓰기)")
        finals[fkey] = k
        if done_dir is not None:
            p = done_idx.get(fkey)
            if p is None:
                errors.append(f"[최종본 없음] {k}: {fp} 가 완료 폴더에 없음")
            elif _sha256(p) != fh:
                errors.append(f"[최종본 해시 불일치] {k}: {fp}")
    if done_dir is not None:
        # 완료 폴더에 미리 있던 파일은 ledger 가 언급하는지와 무관하게 항상 원본 해시를 유지해야 한다
        for name, h in pre_hash.items():
            p = done_idx.get(_nfc(name))
            if p is None:
                errors.append(f"[기존 파일 소실] {name}: 완료 폴더에 미리 있던 파일이 사라짐")
            elif _sha256(p) != h:
                errors.append(f"[기존 파일 덮어쓰기] {name}: 완료 폴더에 미리 있던 파일 내용이 바뀜")
    for s in ("held", "failed"):
        for k in groups[s]:
            present_intact(k)
            if not (entries[k].get("reason") or "").strip():
                errors.append(f"[사유 없음] {k}: {s} 건은 이유가 보여야 함")
    for k in unstarted:
        present_intact(k)

    return {"ok": not errors, "counts": counts, "unstarted": unstarted, "errors": errors}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="입력 등록 = 완료+보류+실패+미착수 대조")
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--ledger", required=True)
    ap.add_argument("--input-dir", required=True, help="처리 후 현재 입력(스캔) 폴더")
    ap.add_argument("--done-dir", default=None, help="완료 폴더(최종본 존재·해시 확인용)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    res = reconcile(json.loads(Path(a.manifest).read_text(encoding="utf-8")),
                    json.loads(Path(a.ledger).read_text(encoding="utf-8")),
                    Path(a.input_dir), Path(a.done_dir) if a.done_dir else None)
    if a.json:
        print(json.dumps(res, ensure_ascii=False, indent=1))
    else:
        c = res["counts"]
        print(f"입력 등록 {c['registered']} = 완료 {c['completed']} + 보류 {c['held']} + 실패 {c['failed']} + 미착수 {c['unstarted']}")
        for e in res["errors"]:
            print("  " + e)
        print("대조 통과" if res["ok"] else f"대조 실패: 위반 {len(res['errors'])}건")
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
