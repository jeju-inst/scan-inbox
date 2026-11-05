import hashlib
import json
import os
import shutil
import unicodedata
from pathlib import Path

import pytest

from scaninbox_corpus import reconcile, snapshot
from scaninbox_corpus.pathutil import extended


@pytest.fixture()
def work(corpus, tmp_path):
    """생성 코퍼스를 시험마다 새 복사본으로 쓴다."""
    out, manifest = corpus
    root = extended(tmp_path / "w")
    shutil.copytree(out, root)
    return root, manifest


def ledger_for(manifest, root, done, complete=(), held=(), failed=()):
    entries = []
    by = {e["file"]: e for e in manifest["files"]}
    for k in complete:
        src = next(p for p in (root / "input").rglob("*") if p.is_file()
                   and unicodedata.normalize("NFC", p.relative_to(root / "input").as_posix()) == k)
        dst_rel = f"final/{abs(hash(k)) % 10**8}.pdf"
        dst = done / dst_rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        src.unlink()
        entries.append({"input": k, "status": "completed", "original_sha256": by[k]["sha256"],
                        "final_path": dst_rel, "final_sha256": by[k]["sha256"]})
    entries += [{"input": k, "status": "held", "reason": "제목 불명"} for k in held]
    entries += [{"input": k, "status": "failed", "reason": "OCR 실패"} for k in failed]
    return {"entries": entries}


def run(manifest, ledger, root, done):
    return reconcile.reconcile(manifest, ledger, root / "input", done)


def test_reconcile_ok_with_all_four_buckets(work, tmp_path):
    root, manifest = work
    done = root / "done_preexisting"
    files = [e["file"] for e in manifest["files"]]
    ok = [e["file"] for e in manifest["files"] if e["expected_outcome"] == "complete"][:3]
    held = [e["file"] for e in manifest["files"] if e["expected_outcome"] == "review"][:1]
    failed = [e["file"] for e in manifest["files"] if e["expected_outcome"] == "fail"][:1]
    res = run(manifest, ledger_for(manifest, root, done, ok, held, failed), root, done)
    assert res["errors"] == [] and res["ok"]
    c = res["counts"]
    assert (c["completed"], c["held"], c["failed"]) == (3, 1, 1)
    assert c["completed"] + c["held"] + c["failed"] + c["unstarted"] == c["registered"] == len(files)


def test_reconcile_detects_lost_input(work):
    root, manifest = work
    done = root / "done_preexisting"
    victim = manifest["files"][0]["file"]
    (root / "input" / victim).unlink()
    res = run(manifest, {"entries": []}, root, done)
    assert not res["ok"] and any("입력 소실" in e for e in res["errors"])


def test_reconcile_detects_modified_input_and_unregistered(work):
    root, manifest = work
    done = root / "done_preexisting"
    (root / "input" / manifest["files"][0]["file"]).write_bytes(b"changed")
    (root / "input" / "new.pdf").write_bytes(b"x")
    res = run(manifest, {"entries": []}, root, done)
    assert any("입력 변경" in e for e in res["errors"]) and any("미등록 파일" in e for e in res["errors"])


def test_reconcile_detects_silent_success_overwrite_and_missing_reason(work):
    root, manifest = work
    done = root / "done_preexisting"
    bad = next(e["file"] for e in manifest["files"] if e["expected_outcome"] == "fail")
    led = ledger_for(manifest, root, done, complete=[bad])
    assert any("실패 위장" in e for e in run(manifest, led, root, done)["errors"])
    # 기존 완료 파일 덮어쓰기
    pre = manifest["preexisting"][0]
    target = manifest["files"][1]["file"]
    led = ledger_for(manifest, root, done, complete=[target])
    led["entries"][0]["final_path"] = Path(pre["file"]).name
    shutil.copy2(root / "done_preexisting" / "final" / os.listdir(root / "done_preexisting" / "final")[0],
                 root / "done_preexisting" / Path(pre["file"]).name)
    led["entries"][0]["final_sha256"] = manifest["files"][1]["sha256"]
    assert any("덮어쓰기" in e for e in run(manifest, led, root, done)["errors"])
    # 보류인데 사유 없음, 중복·미등록 기록
    k = manifest["files"][2]["file"]
    res = run(manifest, {"entries": [{"input": k, "status": "held"}, {"input": k, "status": "held", "reason": "x"},
                                      {"input": "nope.pdf", "status": "failed", "reason": "x"}]}, root, done)
    joined = "\n".join(res["errors"])
    assert "사유 없음" in joined and "중복 기록" in joined and "미등록 입력" in joined


def test_reconcile_flags_review_auto_completed_and_fail_disguised(work):
    root, manifest = work
    done = root / "done_preexisting"
    by = {}
    for e in manifest["files"]:
        by.setdefault(e["category"], e["file"])
    # 기대 review(제목 없는 서식·다중 문서 묶음)가 승인 기록 없이 완료되면 위반
    for cat in ("untitled_form", "multi_doc_bundle"):
        led = ledger_for(manifest, root, done, complete=[by[cat]])
        errs = run(manifest, led, root, done)["errors"]
        assert any("검토 누락" in x and by[cat] in x for x in errs), cat
        # 사용자 승인 기록이 있으면 통과
        led["entries"][0]["approved_by_user"] = True
        assert not any("검토 누락" in x for x in run(manifest, led, root, done)["errors"])
    # 암호화·손상 PDF 가 완료로 위장되면 위반(종류가 메시지에 드러난다)
    for cat, kind in (("encrypted_pdf", "암호화"), ("corrupt_pdf", "손상")):
        led = ledger_for(manifest, root, done, complete=[by[cat]])
        errs = run(manifest, led, root, done)["errors"]
        assert any("실패 위장" in x and kind in x for x in errs), cat
    # 최종본 기록이 비어 있어도 위장은 잡힌다
    led = {"entries": [{"input": by["encrypted_pdf"], "status": "completed",
                        "original_sha256": next(e["sha256"] for e in manifest["files"] if e["file"] == by["encrypted_pdf"])}]}
    errs = run(manifest, led, root, done)["errors"]
    assert any("실패 위장" in x for x in errs) and any("완료 기록 부족" in x for x in errs)


def test_reconcile_no_false_positive_for_review_held_and_fail_failed(work):
    root, manifest = work
    done = root / "done_preexisting"
    by = {}
    for e in manifest["files"]:
        by.setdefault(e["category"], e["file"])
    # 정상 경로(실패는 failed, 검토는 held)는 위반 없음
    led = ledger_for(manifest, root, done, held=[by["untitled_form"], by["multi_doc_bundle"]],
                     failed=[by["encrypted_pdf"], by["corrupt_pdf"]])
    errs = run(manifest, led, root, done)["errors"]
    assert not any(("검토 누락" in x or "실패 위장" in x) for x in errs)


def test_snapshot_detects_changes(work):
    root, _ = work
    inp = root / "input"
    snap = snapshot.take(inp)
    assert snapshot.is_clean(snapshot.compare(snap, snapshot.take(inp)))
    files = sorted(p for p in inp.iterdir() if p.is_file() and p.suffix == ".pdf" and p.name.startswith("SCAN"))
    # mtime 만 변경
    st = files[0].stat()
    os.utime(files[0], ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
    d = snapshot.compare(snap, snapshot.take(inp))
    assert d["mtime_changed"] == [files[0].name] and not snapshot.is_clean(d)
    os.utime(files[0], ns=(st.st_atime_ns, st.st_mtime_ns))
    # 이름 변경 + 내용 수정 + 삭제
    files[1].rename(inp / "renamed.pdf")
    files[2].write_bytes(b"x")
    files[3].unlink()
    d = snapshot.compare(snap, snapshot.take(inp))
    assert d["renamed"][0]["to"] == "renamed.pdf" and d["modified"] == [files[2].name] and d["removed"] == [files[3].name]


def test_snapshot_cli_roundtrip(work, tmp_path):
    root, _ = work
    s = tmp_path / "s.json"
    assert snapshot.main(["create", str(root / "input"), "-o", str(s)]) == 0
    assert snapshot.main(["verify", str(root / "input"), str(s)]) == 0
    next(p for p in (root / "input").iterdir() if p.name.startswith("SCAN")).unlink()
    assert snapshot.main(["verify", str(root / "input"), str(s)]) == 1


def test_reconcile_cli(work, tmp_path):
    root, manifest = work
    led = tmp_path / "l.json"
    led.write_text(json.dumps({"entries": []}), encoding="utf-8")
    argv = ["--manifest", str(root / "manifest.json"), "--ledger", str(led), "--input-dir", str(root / "input")]
    assert reconcile.main(argv) == 0  # 전부 미착수, 입력 무변경


def test_reconcile_detects_unrecorded_preexisting_overwrite(work):
    root, manifest = work
    done = root / "done_preexisting"
    pre_name = Path(manifest["preexisting"][0]["file"]).name
    (done / pre_name).write_bytes(b"overwritten without ledger entry")
    res = run(manifest, {"entries": []}, root, done)
    assert any("기존 파일 덮어쓰기" in e for e in res["errors"])
    (done / pre_name).unlink()
    assert any("기존 파일 소실" in e for e in run(manifest, {"entries": []}, root, done)["errors"])


def test_reconcile_detects_nfc_nfd_final_path_collision(work):
    root, manifest = work
    done = root / "done_preexisting"
    a, b = manifest["files"][0], manifest["files"][1]
    # 같은 최종 파일을 NFC/NFD 두 철자로 가리키는 두 완료 기록
    final_nfc = "최종/한글 결과.pdf"
    final_nfd = unicodedata.normalize("NFD", final_nfc)
    (done / "최종").mkdir()
    (done / final_nfc).write_bytes(b"one output")
    h = hashlib.sha256(b"one output").hexdigest()
    for ent in (a, b):
        (root / "input" / ent["file"]).unlink()
    led = {"entries": [
        {"input": a["file"], "status": "completed", "original_sha256": a["sha256"], "final_path": final_nfc, "final_sha256": h},
        {"input": b["file"], "status": "completed", "original_sha256": b["sha256"], "final_path": final_nfd, "final_sha256": h},
    ]}
    assert any("최종본 경로 중복" in e for e in run(manifest, led, root, done)["errors"])


def test_snapshot_detects_directory_symlink_changes(tmp_path):
    d = tmp_path / "in"
    (d / "a").mkdir(parents=True)
    (d / "b").mkdir()
    (d / "link").symlink_to(d / "a", target_is_directory=True)
    snap = snapshot.take(d)
    assert snap["files"]["link"]["symlink"] == str(d / "a")
    assert snapshot.is_clean(snapshot.compare(snap, snapshot.take(d)))
    (d / "link").unlink()
    (d / "link").symlink_to(d / "b", target_is_directory=True)  # 다른 폴더로 재지정
    assert snapshot.compare(snap, snapshot.take(d))["modified"] == ["link"]
    (d / "link").unlink()
    (d / "link").mkdir()  # 링크를 실제 빈 폴더로 교체
    assert not snapshot.is_clean(snapshot.compare(snap, snapshot.take(d)))


def test_snapshot_normalizes_extended_prefix_in_symlink_target(tmp_path, monkeypatch):
    """Windows 의 readlink 가 `\\\\?\\` 접두를 붙여 돌려줘도 일반 표기로 기록한다."""
    from scaninbox_corpus import pathutil
    assert pathutil.strip_extended_str("\\\\?\\C:\\a\\b") == "C:\\a\\b"
    assert pathutil.strip_extended_str("\\\\?\\UNC\\srv\\share\\x") == "\\\\srv\\share\\x"
    assert pathutil.strip_extended_str("C:\\a") == "C:\\a" and pathutil.strip_extended_str("/posix/a") == "/posix/a"
    d = tmp_path / "in"
    d.mkdir()
    (d / "l").symlink_to(d, target_is_directory=True)
    monkeypatch.setattr(snapshot.os, "readlink", lambda p: "\\\\?\\C:\\x\\y")
    assert snapshot.take(d)["files"]["l"]["symlink"] == "C:\\x\\y"


def test_extended_path_helpers():
    from scaninbox_corpus import pathutil
    assert pathutil.to_extended_str("C:\\a\\b") == "\\\\?\\C:\\a\\b"
    assert pathutil.to_extended_str("\\\\srv\\share\\x") == "\\\\?\\UNC\\srv\\share\\x"
    assert pathutil.to_extended_str("\\\\?\\C:\\a") == "\\\\?\\C:\\a"  # 이미 확장 경로면 그대로
    assert pathutil.to_extended_str("C:\\a\\..\\b\\.\\c") == "\\\\?\\C:\\b\\c"  # `\\?\` 경로는 .. 를 해석하지 않으므로 미리 정규화
    if __import__("os").name != "nt":  # 비 Windows 에서는 경로를 건드리지 않는다
        assert str(pathutil.extended("/tmp/x")) == "/tmp/x"


def test_font_license_not_misreported_as_ofl(tmp_path):
    from scaninbox_corpus.fonts import FONT_LICENSE_OFL, font_info
    f = tmp_path / "malgun.ttf"
    f.write_bytes(b"x")
    assert font_info(f)["license"] != FONT_LICENSE_OFL
    g = tmp_path / "Custom.ttf"
    g.write_bytes(b"x")
    assert font_info(g)["license"] != FONT_LICENSE_OFL
    n = tmp_path / "NanumBarunGothic.ttf"
    n.write_bytes(b"x")
    assert font_info(n)["license"] == FONT_LICENSE_OFL


def test_font_candidates_cover_windows_and_prefer_ofl(monkeypatch):
    from scaninbox_corpus import fonts
    monkeypatch.setenv("WINDIR", "C:\\Windows")
    monkeypatch.setenv("LOCALAPPDATA", "C:\\Users\\u\\AppData\\Local")
    names = [str(p).replace("\\", "/") for p in fonts.candidates()]
    assert any(n.endswith("Windows/Fonts/NanumBarunGothic.ttf") for n in names)
    assert any("Microsoft/Windows/Fonts/NanumGothic.ttf" in n for n in names)
    assert any(n.endswith("Windows/Fonts/malgun.ttf") for n in names)
    # OFL 폰트 후보가 모두 시스템 폰트(맑은 고딕)보다 앞에 온다
    last_ofl = max(i for i, n in enumerate(names) if not n.endswith("malgun.ttf"))
    first_sys = min(i for i, n in enumerate(names) if n.endswith("malgun.ttf"))
    assert last_ofl < first_sys
