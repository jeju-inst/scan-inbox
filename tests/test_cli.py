"""CLI 모듈 서브커맨드 및 출력 형식 테스트."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from reportlab.pdfgen import canvas

from scaninbox import cli
from scaninbox.contracts import OcrResult, OcrStatus, TitleProposal
from scaninbox.journal import sha256_file
from scaninbox.pipeline import Pipeline


def make_pdf(path: Path, text: str = "스캔 문서 테스트") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(path))
    c.drawString(100, 750, text)
    c.save()
    return path


class FakeCliOcrEngine:
    def run(self, src: Path, dst_dir: Path, *, langs: str = "kor+eng", timeout_s: int = 600) -> OcrResult:
        out = dst_dir / f"ocr_{src.name}"
        out.write_bytes(src.read_bytes())
        long_body = "이것은 절대 CLI 출력에 나와서는 안 되는 긴 OCR 본문 전체 텍스트입니다. " * 10
        return OcrResult(
            status=OcrStatus.OK,
            output=out,
            page_count_in=1,
            page_count_out=1,
            text_by_page=[long_body],
            engine="fake-ocr",
        )


class FakeCliTitleExtractor:
    def propose(self, pdf: Path, text_by_page: Sequence[str]) -> TitleProposal:
        return TitleProposal(
            title="테스트문서",
            evidence_page=1,
            evidence_snippet="제목 근거 발췌문 요약",
            doc_date="2026-11-05",
        )


@pytest.fixture
def cli_env(tmp_path: Path):
    in_dir = tmp_path / "in"
    done_dir = tmp_path / "out"
    backup_dir = tmp_path / "bak"
    j_path = tmp_path / "journal.sqlite"
    in_dir.mkdir()
    done_dir.mkdir()
    backup_dir.mkdir()
    return in_dir, done_dir, backup_dir, j_path


def test_cli_init(tmp_path, capsys):
    in_dir = tmp_path / "new_in"
    done_dir = tmp_path / "new_done"
    bak_dir = tmp_path / "new_bak"

    ret = cli.main(["--input-dir", str(in_dir), "--done-dir", str(done_dir), "--backup-dir", str(bak_dir), "init"])
    assert ret == 0
    assert in_dir.exists() and done_dir.exists() and bak_dir.exists()
    out = capsys.readouterr().out
    assert "Scan Inbox 초기화 완료" in out


def test_cli_scan_and_plan_text_and_json(cli_env, capsys):
    in_dir, done_dir, backup_dir, j_path = cli_env
    doc = make_pdf(in_dir / "sample_doc.pdf", "본문 텍스트")

    pipe = Pipeline(
        scan_dir=in_dir,
        done_dir=done_dir,
        backup_dir=backup_dir,
        journal_path=j_path,
        ocr_engine=FakeCliOcrEngine(),
        title_extractor=FakeCliTitleExtractor(),
    )

    # 1. scan
    parser = cli.build_parser()
    args_scan = parser.parse_args(["--input-dir", str(in_dir), "--done-dir", str(done_dir), "--backup-dir", str(backup_dir), "scan"])
    ret_scan = cli.cmd_scan(args_scan, pipeline=pipe)
    assert ret_scan == 0
    scan_out = capsys.readouterr().out
    assert "스캔 완료" in scan_out

    # 2. plan (사람이 읽는 출력)
    args_plan = parser.parse_args(["--input-dir", str(in_dir), "--done-dir", str(done_dir), "--backup-dir", str(backup_dir), "plan"])
    ret_plan = cli.cmd_plan(args_plan, pipeline=pipe)
    assert ret_plan == 0
    plan_out = capsys.readouterr().out

    # 중요: 발췌문만 허용, 본문 전체 출력 금지
    assert "제목 근거 발췌문 요약" in plan_out
    assert "절대 CLI 출력에 나와서는 안 되는 긴 OCR 본문 전체 텍스트" not in plan_out
    assert "sample_doc.pdf" in plan_out

    # 3. plan --json
    args_json = parser.parse_args(["--input-dir", str(in_dir), "--done-dir", str(done_dir), "--backup-dir", str(backup_dir), "plan", "--json"])
    ret_json = cli.cmd_plan(args_json, pipeline=pipe)
    assert ret_json == 0
    json_out = capsys.readouterr().out
    data = json.loads(json_out)
    assert len(data) == 1
    assert data[0]["input"] == "sample_doc.pdf"
    assert data[0]["evidence_snippet"] == "제목 근거 발췌문 요약"


def test_cli_apply_rename_hold(cli_env, capsys):
    in_dir, done_dir, backup_dir, j_path = cli_env
    make_pdf(in_dir / "doc1.pdf", "문서 1")
    make_pdf(in_dir / "doc2.pdf", "문서 2")
    make_pdf(in_dir / "doc3.pdf", "문서 3")

    pipe = Pipeline(
        scan_dir=in_dir,
        done_dir=done_dir,
        backup_dir=backup_dir,
        journal_path=j_path,
        ocr_engine=FakeCliOcrEngine(),
        title_extractor=FakeCliTitleExtractor(),
    )
    pipe.scan()

    parser = cli.build_parser()

    # 1. rename
    args_rename = parser.parse_args([
        "--input-dir", str(in_dir), "--done-dir", str(done_dir), "--backup-dir", str(backup_dir),
        "rename", "doc1.pdf", "2026-11-05_새이름.pdf",
    ])
    ret_rename = cli.cmd_rename(args_rename, pipeline=pipe)
    assert ret_rename == 0
    capsys.readouterr()

    # 2. hold
    args_hold = parser.parse_args([
        "--input-dir", str(in_dir), "--done-dir", str(done_dir), "--backup-dir", str(backup_dir),
        "hold", "doc2.pdf", "--reason", "추후 재검토",
    ])
    ret_hold = cli.cmd_hold(args_hold, pipeline=pipe)
    assert ret_hold == 0
    capsys.readouterr()

    # 3. apply
    args_apply = parser.parse_args([
        "--input-dir", str(in_dir), "--done-dir", str(done_dir), "--backup-dir", str(backup_dir),
        "apply", "doc1.pdf",
    ])
    ret_apply = cli.cmd_apply(args_apply, pipeline=pipe)
    assert ret_apply == 0
    apply_out = capsys.readouterr().out
    assert "doc1.pdf" in apply_out
    assert not (in_dir / "doc1.pdf").exists()
    assert (done_dir / "2026-11-05_새이름.pdf").exists()

    # 4. status
    args_status = parser.parse_args([
        "--input-dir", str(in_dir), "--done-dir", str(done_dir), "--backup-dir", str(backup_dir),
        "status",
    ])
    ret_status = cli.cmd_status(args_status, pipeline=pipe)
    assert ret_status == 0
    status_out = capsys.readouterr().out
    assert "completed: 1건" in status_out
    assert "held: 1건" in status_out

    # 5. apply --approve-all
    args_all = parser.parse_args([
        "--input-dir", str(in_dir), "--done-dir", str(done_dir), "--backup-dir", str(backup_dir),
        "apply", "--approve-all",
    ])
    ret_all = cli.cmd_apply(args_all, pipeline=pipe)
    assert ret_all == 0
    assert not (in_dir / "doc3.pdf").exists()


def test_cli_undo_and_recover(cli_env, capsys):
    in_dir, done_dir, backup_dir, j_path = cli_env
    doc = make_pdf(in_dir / "undo_cli.pdf", "되돌리기")

    pipe = Pipeline(
        scan_dir=in_dir,
        done_dir=done_dir,
        backup_dir=backup_dir,
        journal_path=j_path,
        ocr_engine=FakeCliOcrEngine(),
        title_extractor=FakeCliTitleExtractor(),
    )
    pipe.scan()
    pipe.apply(approve_all=True)

    parser = cli.build_parser()

    # undo
    args_undo = parser.parse_args([
        "--input-dir", str(in_dir), "--done-dir", str(done_dir), "--backup-dir", str(backup_dir),
        "undo", "undo_cli.pdf",
    ])
    ret_undo = cli.cmd_undo(args_undo, pipeline=pipe)
    assert ret_undo == 0
    assert doc.exists()

    # recover
    args_recover = parser.parse_args([
        "--input-dir", str(in_dir), "--done-dir", str(done_dir), "--backup-dir", str(backup_dir),
        "recover",
    ])
    ret_rec = cli.cmd_recover(args_recover, pipeline=pipe)
    assert ret_rec == 0


def test_cli_export_ledger(cli_env, tmp_path, capsys):
    in_dir, done_dir, backup_dir, j_path = cli_env
    doc = make_pdf(in_dir / "ledger_doc.pdf", "원장 시험")

    pipe = Pipeline(
        scan_dir=in_dir,
        done_dir=done_dir,
        backup_dir=backup_dir,
        journal_path=j_path,
        ocr_engine=FakeCliOcrEngine(),
        title_extractor=FakeCliTitleExtractor(),
    )
    pipe.scan()
    pipe.apply(approve_all=True)

    parser = cli.build_parser()

    # stdout export
    args_stdout = parser.parse_args([
        "--input-dir", str(in_dir), "--done-dir", str(done_dir), "--backup-dir", str(backup_dir),
        "export-ledger",
    ])
    ret_stdout = cli.cmd_export_ledger(args_stdout, pipeline=pipe)
    assert ret_stdout == 0
    stdout_txt = capsys.readouterr().out
    data = json.loads(stdout_txt)
    assert "entries" in data
    assert data["entries"][0]["status"] == "completed"
    assert data["entries"][0]["approved_by_user"] is True

    # file export (-o)
    ledger_file = tmp_path / "ledger_out.json"
    args_file = parser.parse_args([
        "--input-dir", str(in_dir), "--done-dir", str(done_dir), "--backup-dir", str(backup_dir),
        "export-ledger", "-o", str(ledger_file),
    ])
    ret_file = cli.cmd_export_ledger(args_file, pipeline=pipe)
    assert ret_file == 0
    assert ledger_file.exists()
    file_data = json.loads(ledger_file.read_text(encoding="utf-8"))
    assert file_data == data


def test_cli_scopes_journal_per_workspace(tmp_path):
    """서로 다른 input-dir 에 대해 각각 격리된 journal_path 가 할당되어 충돌하지 않음을 검증."""
    in_dir1 = tmp_path / "ws1_in"
    in_dir2 = tmp_path / "ws2_in"
    in_dir1.mkdir()
    in_dir2.mkdir()

    parser = cli.build_parser()
    args1 = parser.parse_args(["--input-dir", str(in_dir1), "init"])
    pipe1 = cli._create_pipeline(args1)

    args2 = parser.parse_args(["--input-dir", str(in_dir2), "init"])
    pipe2 = cli._create_pipeline(args2)

    assert pipe1.journal.path != pipe2.journal.path

