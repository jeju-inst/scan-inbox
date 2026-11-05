"""입력 무변경 검증: 폴더의 이름·크기·SHA-256·mtime 스냅샷을 만들고 전후를 대조한다.

    python -m scaninbox_corpus.snapshot create corpus_out/input -o before.json
    ... (미리보기 등 시험 대상 동작) ...
    python -m scaninbox_corpus.snapshot verify corpus_out/input before.json

PILOT.md 필수 통과 조건 "무변경 preview 에서 파일명·본문·mtime·위치가 전후 동일"을 프로그램으로 확인한다.
파일을 읽는 것만으로는 mtime 이 바뀌지 않는다. 차이가 있으면 종료 코드 1.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import unicodedata
from pathlib import Path


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


def take(root: Path) -> dict:
    """root 아래 모든 파일·폴더·심볼릭 링크를 기록한다(링크는 따라가지 않음)."""
    root = Path(root)
    files: dict[str, dict] = {}
    dirs: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dp = Path(dirpath)
        for d in sorted(dirnames):
            p = dp / d
            rel = p.relative_to(root).as_posix()
            if p.is_symlink():  # 폴더를 가리키는 링크: 따라가지 않고 대상 문자열만 기록
                files[rel] = {"symlink": os.readlink(p), "dir_link": True}
            else:
                dirs.append(rel)
        for fn in sorted(filenames):
            p = dp / fn
            rel = p.relative_to(root).as_posix()
            if p.is_symlink():
                files[rel] = {"symlink": os.readlink(p)}
                continue
            st = p.stat()
            files[rel] = {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "sha256": _sha256(p)}
    return {"version": 1, "files": files, "dirs": sorted(dirs)}


def compare(before: dict, after: dict) -> dict:
    """차이 보고. 모든 목록이 비어 있으면 무변경."""
    bf, af = before["files"], after["files"]
    removed = sorted(set(bf) - set(af))
    added = sorted(set(af) - set(bf))
    # 같은 내용이 다른 이름으로 나타났으면 이름 변경·이동으로 따로 분류
    renamed = []
    for r in list(removed):
        h = bf[r].get("sha256")
        match = next((a for a in added if h and af[a].get("sha256") == h), None)
        if match:
            renamed.append({"from": r, "to": match, "normalization_only": nfc(r) == nfc(match)})
            removed.remove(r)
            added.remove(match)
    modified, mtime_changed = [], []
    for r in sorted(set(bf) & set(af)):
        b, a = bf[r], af[r]
        if b.get("sha256") != a.get("sha256") or b.get("symlink") != a.get("symlink") or b.get("size") != a.get("size"):
            modified.append(r)
        elif b.get("mtime_ns") != a.get("mtime_ns"):
            mtime_changed.append(r)
    dirs_removed = sorted(set(before["dirs"]) - set(after["dirs"]))
    dirs_added = sorted(set(after["dirs"]) - set(before["dirs"]))
    return {"removed": removed, "added": added, "renamed": renamed, "modified": modified,
            "mtime_changed": mtime_changed, "dirs_removed": dirs_removed, "dirs_added": dirs_added,
            "unchanged": len(set(bf) & set(af)) - len(modified) - len(mtime_changed)}


def is_clean(diff: dict) -> bool:
    return not any(diff[k] for k in ("removed", "added", "renamed", "modified", "mtime_changed", "dirs_removed", "dirs_added"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="입력 폴더 무변경 검증(이름·해시·mtime)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create", help="스냅샷 생성")
    c.add_argument("dir")
    c.add_argument("-o", "--out", required=True)
    v = sub.add_parser("verify", help="스냅샷과 현재 상태 대조")
    v.add_argument("dir")
    v.add_argument("snapshot")
    v.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "create":
        snap = take(Path(a.dir))
        Path(a.out).write_text(json.dumps(snap, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        print(f"스냅샷 기록: 파일 {len(snap['files'])}건, 폴더 {len(snap['dirs'])}건 -> {a.out}")
        return 0
    before = json.loads(Path(a.snapshot).read_text(encoding="utf-8"))
    diff = compare(before, take(Path(a.dir)))
    if a.json:
        print(json.dumps(diff, ensure_ascii=False, indent=1))
    else:
        if is_clean(diff):
            print(f"무변경 확인: 파일 {diff['unchanged']}건 이름·크기·해시·mtime 동일")
        else:
            print("입력 변경 감지:")
            for k in ("removed", "added", "renamed", "modified", "mtime_changed", "dirs_removed", "dirs_added"):
                for item in diff[k]:
                    print(f"  [{k}] {item}")
    return 0 if is_clean(diff) else 1


if __name__ == "__main__":
    sys.exit(main())
