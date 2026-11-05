"""Pipeline 모듈 단위 및 통합 테스트 (실제 core/engines 모듈 연동)."""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Optional, Sequence

import pytest
from reportlab.pdfgen import canvas

from scaninbox.config import Config
from scaninbox.contracts import (
    OcrEngine,
    OcrResult,
    OcrStatus,
    PlanItem,
    State,
    TitleExtractor,
    TitleProposal,
)
from scaninbox.journal import Journal, sha256_file
from scaninbox.ocr import OcrmypdfEngine
from scaninbox.pipeline import Pipeline
from scaninbox.title import RuleTitleExtractor
from scaninbox_corpus import reconcile, snapshot
from scaninbox_corpus.pathutil import extended


@pytest.fixture()
def work(corpus, tmp_path):
    """생성 코퍼스를 시험마다 새 복사본으로 쓴다."""
    out, manifest = corpus
    root = extended(tmp_path / "w")
    shutil.copytree(out, root)
    return root, manifest


def make_pdf(path: Path, text: str = "테스트 스캔 문서 내용") -> Path:
    """테스트용 유효한 단일 페이지 PDF 생성."""
    path.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(path))
    c.drawString(100, 750, text)
    c.save()
    return path


class FakeOcrEngine:
    """테스트용 가짜 OCR 엔진."""

    def __init__(self, default_status: OcrStatus = OcrStatus.OK) -> None:
        self.default_status = default_status
        self.file_status_map: dict[str, OcrStatus] = {}

    def set_status(self, filename: str, status: OcrStatus) -> None:
        self.file_status_map[filename] = status

    def run(
        self,
        src: Path,
        dst_dir: Path,
        *,
        langs: str = "kor+eng",
        timeout_s: int = 600,
    ) -> OcrResult:
        status = self.file_status_map.get(src.name, self.default_status)
        if "encrypted" in src.name.lower():
            status = OcrStatus.ENCRYPTED
        elif "corrupt" in src.name.lower():
            status = OcrStatus.CORRUPT
        elif "failed" in src.name.lower():
            status = OcrStatus.FAILED
        elif "partial" in src.name.lower():
            status = OcrStatus.PARTIAL

        if status == OcrStatus.ENCRYPTED:
            return OcrResult(
                status=OcrStatus.ENCRYPTED,
                reason="암호화된 PDF (비밀번호 필요)",
                engine="fake-ocr",
            )
        elif status == OcrStatus.CORRUPT:
            return OcrResult(
                status=OcrStatus.CORRUPT,
                reason="손상된 PDF 구조",
                engine="fake-ocr",
            )
        elif status == OcrStatus.FAILED:
            return OcrResult(
                status=OcrStatus.FAILED,
                reason="엔진 타임아웃 오류",
                engine="fake-ocr",
            )

        out_pdf = dst_dir / f"ocr_{src.name}"
        shutil.copy2(src, out_pdf)

        if status == OcrStatus.PARTIAL:
            return OcrResult(
                status=OcrStatus.PARTIAL,
                output=out_pdf,
                page_count_in=2,
                page_count_out=2,
                text_by_page=["1페이지 텍스트", ""],
                empty_pages=[2],
                engine="fake-ocr",
            )

        return OcrResult(
            status=status,
            output=out_pdf,
            page_count_in=1,
            page_count_out=1,
            text_by_page=["정상 텍스트 본문"],
            empty_pages=[],
            engine="fake-ocr",
        )


class FakeTitleExtractor:
    """테스트용 가짜 제목 추출기."""

    def __init__(
        self,
        title: Optional[str] = "문서제목",
        doc_date: Optional[str] = "2026-11-05",
        review_reasons: Optional[list[str]] = None,
    ) -> None:
        self.default_title = title
        self.default_date = doc_date
        self.default_review_reasons = review_reasons or []

    def propose(self, pdf: Path, text_by_page: Sequence[str]) -> TitleProposal:
        name = pdf.name
        if "unknown_title" in name:
            return TitleProposal(
                title=None,
                evidence_page=None,
                evidence_snippet="",
                doc_date=self.default_date,
                review_reasons=["제목을 식별할 수 없음"],
            )
        if "unknown_date" in name:
            return TitleProposal(
                title=self.default_title,
                evidence_page=1,
                evidence_snippet="발췌 내용",
                doc_date=None,
                review_reasons=["문서 날짜 불명"],
            )

        return TitleProposal(
            title=self.default_title,
            evidence_page=1,
            evidence_snippet="추출된 제목 발췌문",
            doc_date=self.default_date,
            review_reasons=list(self.default_review_reasons),
        )


@pytest.fixture
def env_dirs(tmp_path: Path):
    in_dir = tmp_path / "input"
    done_dir = tmp_path / "done"
    backup_dir = tmp_path / "backup"
    j_path = tmp_path / "journal.sqlite"
    in_dir.mkdir()
    done_dir.mkdir()
    backup_dir.mkdir()
    return in_dir, done_dir, backup_dir, j_path


def test_pipeline_normal_flow(env_dirs):
    in_dir, done_dir, backup_dir, j_path = env_dirs
    doc = make_pdf(in_dir / "sample1.pdf", "정상 문서 텍스트")
    orig_sha256 = sha256_file(doc)

    pipe = Pipeline(
        scan_dir=in_dir,
        done_dir=done_dir,
        backup_dir=backup_dir,
        journal_path=j_path,
        ocr_engine=FakeOcrEngine(OcrStatus.OK),
        title_extractor=FakeTitleExtractor(title="업무보고서", doc_date="2026-11-05"),
    )

    items = pipe.scan()
    assert len(items) == 1
    assert items[0].proposed_name == f"2026-11-05_업무보고서__{orig_sha256[:8]}.pdf"
    assert not items[0].needs_review

    applied = pipe.apply(approve_all=True)
    assert len(applied) == 1
    assert not doc.exists()
    assert (done_dir / f"2026-11-05_업무보고서__{orig_sha256[:8]}.pdf").exists()
    assert (backup_dir / "sample1.pdf").exists()


def test_pipeline_review_paths(env_dirs):
    in_dir, done_dir, backup_dir, j_path = env_dirs
    make_pdf(in_dir / "partial_doc.pdf")
    make_pdf(in_dir / "unknown_title.pdf")

    pipe = Pipeline(
        scan_dir=in_dir,
        done_dir=done_dir,
        backup_dir=backup_dir,
        journal_path=j_path,
        ocr_engine=FakeOcrEngine(),
        title_extractor=FakeTitleExtractor(),
    )

    items = pipe.scan()
    assert len(items) == 2
    for it in items:
        assert it.needs_review is True

    pipe.rename("unknown_title.pdf", "2026-11-05_수동지정이름__12345678.pdf")
    applied = pipe.apply(targets=["unknown_title.pdf"])
    assert applied == ["unknown_title.pdf"]
    assert (done_dir / "2026-11-05_수동지정이름__12345678.pdf").exists()


def test_pipeline_held_and_failed_paths(env_dirs):
    in_dir, done_dir, backup_dir, j_path = env_dirs
    f_enc = make_pdf(in_dir / "encrypted_file.pdf")
    f_cor = make_pdf(in_dir / "corrupt_file.pdf")
    f_fail = make_pdf(in_dir / "failed_ocr.pdf")

    pipe = Pipeline(
        scan_dir=in_dir,
        done_dir=done_dir,
        backup_dir=backup_dir,
        journal_path=j_path,
        ocr_engine=FakeOcrEngine(),
        title_extractor=FakeTitleExtractor(),
    )

    items = pipe.scan()
    assert len(items) == 0

    assert f_enc.exists()
    assert f_cor.exists()
    assert f_fail.exists()

    rec_enc = pipe._find_latest_record("encrypted_file.pdf")
    assert rec_enc.state == State.HELD
    rec_cor = pipe._find_latest_record("corrupt_file.pdf")
    assert rec_cor.state == State.FAILED


def test_pipeline_input_hash_change_rejected(env_dirs):
    """승인 후 입력 파일 해시 변경 시 확정 거절 회귀 테스트."""
    in_dir, done_dir, backup_dir, j_path = env_dirs
    doc = make_pdf(in_dir / "tampered.pdf", "최초 내용")

    pipe = Pipeline(
        scan_dir=in_dir,
        done_dir=done_dir,
        backup_dir=backup_dir,
        journal_path=j_path,
        ocr_engine=FakeOcrEngine(),
        title_extractor=FakeTitleExtractor(),
    )
    pipe.scan()
    pipe.approve("tampered.pdf")

    doc.write_bytes(b"%PDF-1.4 tampered bytes")

    with pytest.raises(ValueError, match="해시와 불일치"):
        pipe.apply(targets=["tampered.pdf"])

    assert doc.exists()


def test_pipeline_no_ocr_engine_fails_closed(env_dirs):
    """OCR 엔진 미설정 시 가짜 성공 금지 및 FAILED 처리 회귀 테스트."""
    in_dir, done_dir, backup_dir, j_path = env_dirs
    doc = make_pdf(in_dir / "no_engine.pdf")

    pipe = Pipeline(
        scan_dir=in_dir,
        done_dir=done_dir,
        backup_dir=backup_dir,
        journal_path=j_path,
        ocr_engine=None,
        title_extractor=FakeTitleExtractor(),
    )
    items = pipe.scan()
    assert len(items) == 0
    rec = pipe._find_latest_record("no_engine.pdf")
    assert rec.state == State.FAILED
    assert "OCR 엔진 미설정" in rec.reason


def test_pipeline_recover_after_publishing_crash(env_dirs):
    """배치 후 입력 정리 전 중단 시 recover 가 중복 최종본 없이 입력 정리 및 COMPLETED 처리."""
    in_dir, done_dir, backup_dir, j_path = env_dirs
    doc = make_pdf(in_dir / "crash_doc.pdf", "중단 시험")
    orig_sha256 = sha256_file(doc)

    pipe = Pipeline(
        scan_dir=in_dir,
        done_dir=done_dir,
        backup_dir=backup_dir,
        journal_path=j_path,
        ocr_engine=FakeOcrEngine(),
        title_extractor=FakeTitleExtractor(),
    )
    pipe.scan()

    final_name = f"2026-11-05_최종본__{orig_sha256[:8]}.pdf"
    final_p = done_dir / final_name
    shutil.copy2(doc, final_p)
    final_sha = sha256_file(final_p)

    rec = pipe._find_latest_record("crash_doc.pdf")
    pipe.journal.transition(
        rec.id, State.APPROVED, approved_by_user=True
    )
    pipe.journal.transition(
        rec.id, State.PUBLISHING, final_rel=final_name, final_sha256=final_sha
    )
    pipe.journal.transition(rec.id, State.PUBLISHED)
    pipe.journal.transition(rec.id, State.SOURCE_CLEANUP_PENDING)

    recovered = pipe.recover()
    assert any("crash_doc.pdf" in r for r in recovered)
    assert not doc.exists()
    assert final_p.exists()
    assert len(list(done_dir.glob("*.pdf"))) == 1
    assert pipe.journal.get(rec.id).state == State.COMPLETED


def test_pipeline_plan_leaves_input_folder_intact(env_dirs):
    """scan 및 plan 실행 시 입력 폴더의 파일명·크기·해시·mtime 불변 검증."""
    in_dir, done_dir, backup_dir, j_path = env_dirs
    make_pdf(in_dir / "sub" / "doc1.pdf", "문서 1")
    make_pdf(in_dir / "doc2.pdf", "문서 2")

    before = snapshot.take(in_dir)

    pipe = Pipeline(
        scan_dir=in_dir,
        done_dir=done_dir,
        backup_dir=backup_dir,
        journal_path=j_path,
        ocr_engine=FakeOcrEngine(),
        title_extractor=FakeTitleExtractor(),
    )
    pipe.scan()
    pipe.plan()

    after = snapshot.take(in_dir)
    diff = snapshot.compare(before, after)
    assert snapshot.is_clean(diff)


def test_pipeline_undo_success_and_conflict(env_dirs):
    """되돌리기 정상 동작 및 타 파일 충돌 방지 회귀 테스트."""
    in_dir, done_dir, backup_dir, j_path = env_dirs
    doc = make_pdf(in_dir / "undo_test.pdf", "되돌리기")
    orig_sha256 = sha256_file(doc)

    pipe = Pipeline(
        scan_dir=in_dir,
        done_dir=done_dir,
        backup_dir=backup_dir,
        journal_path=j_path,
        ocr_engine=FakeOcrEngine(),
        title_extractor=FakeTitleExtractor(),
    )
    pipe.scan()
    pipe.apply(approve_all=True)

    assert not doc.exists()

    undone = pipe.undo("undo_test.pdf")
    assert undone == ["undo_test.pdf"]
    assert doc.exists()
    assert sha256_file(doc) == orig_sha256
    assert pipe._find_latest_record("undo_test.pdf").state == State.DISCOVERED


def test_pipeline_generation_specific_backup_preserved(env_dirs):
    """동일 파일명 새 문서 도착 시 이전 세대 백업 보존 회귀 테스트."""
    in_dir, done_dir, backup_dir, j_path = env_dirs
    doc = make_pdf(in_dir / "reuse.pdf", "1세대 원본")
    sha1 = sha256_file(doc)

    pipe = Pipeline(
        scan_dir=in_dir,
        done_dir=done_dir,
        backup_dir=backup_dir,
        journal_path=j_path,
        ocr_engine=FakeOcrEngine(),
        title_extractor=FakeTitleExtractor(),
    )
    pipe.scan()
    pipe.apply(approve_all=True)

    old_rec = pipe._find_latest_record("reuse.pdf")
    old_bk = backup_dir / old_rec.backup_rel
    assert old_bk.exists() and sha256_file(old_bk) == sha1

    make_pdf(doc, "2세대 새 원본")
    sha2 = sha256_file(doc)

    pipe.scan()
    pipe.apply(approve_all=True)

    # 1세대 백업 보존 확인
    assert old_bk.exists() and sha256_file(old_bk) == sha1

    new_rec = pipe._find_latest_record("reuse.pdf")
    new_bk = backup_dir / new_rec.backup_rel
    assert new_bk.exists() and sha256_file(new_bk) == sha2
    assert old_bk != new_bk


def test_pipeline_export_ledger_and_reconcile_match(work, tmp_path):
    """합성 코퍼스 23건 기준 reconcile 위반 0건 확인."""
    root, manifest = work
    in_dir = root / "input"
    done_dir = root / "done_preexisting"
    backup_dir = root / "backup_test"
    j_path = root / "journal.sqlite"
    backup_dir.mkdir(parents=True, exist_ok=True)

    by_file = {e["file"]: e for e in manifest["files"]}
    ocr = FakeOcrEngine()
    title = FakeTitleExtractor()

    for k, info in by_file.items():
        fname = Path(k).name
        if info["expected_outcome"] == "fail":
            ocr.set_status(fname, OcrStatus.CORRUPT if "corrupt" in fname else OcrStatus.ENCRYPTED)
        elif info["expected_outcome"] == "review":
            ocr.set_status(fname, OcrStatus.PARTIAL)

    pipe = Pipeline(
        scan_dir=in_dir,
        done_dir=done_dir,
        backup_dir=backup_dir,
        journal_path=j_path,
        ocr_engine=ocr,
        title_extractor=title,
    )

    pipe.scan()

    for k, info in by_file.items():
        if info["expected_outcome"] == "complete":
            pipe.approve(k)
        elif info["expected_outcome"] == "review":
            pipe.rename(k, f"승인완료_{Path(k).stem}.pdf")

    pipe.apply(approve_all=False)

    ledger_path = root / "test_ledger.json"
    ledger = pipe.export_ledger(path=ledger_path)

    res = reconcile.reconcile(manifest, ledger, in_dir, done_dir)
    assert res["errors"] == []
    assert res["ok"] is True


def test_pipeline_real_engine_e2e_corpus(work, tmp_path):
    """실제 OcrmypdfEngine 및 RuleTitleExtractor 를 사용한 코퍼스 e2e (환경 가용 시에만)."""
    root, manifest = work
    in_dir = root / "input"
    done_dir = root / "done_preexisting"
    backup_dir = root / "backup_real"
    j_path = root / "journal_real.sqlite"
    backup_dir.mkdir(parents=True, exist_ok=True)

    real_ocr = OcrmypdfEngine()
    try:
        real_ocr.check_environment("kor+eng")
    except Exception as e:
        pytest.skip(f"실제 OCR 환경(kor 언어팩) 미비로 건너뜀: {e}")

    real_title = RuleTitleExtractor()
    pipe = Pipeline(
        scan_dir=in_dir,
        done_dir=done_dir,
        backup_dir=backup_dir,
        journal_path=j_path,
        ocr_engine=real_ocr,
        title_extractor=real_title,
    )

    pipe.scan()

    by_file = {e["file"]: e for e in manifest["files"]}
    for k, info in by_file.items():
        if info["expected_outcome"] == "complete":
            pipe.approve(k)
        elif info["expected_outcome"] == "review":
            pipe.rename(k, f"승인_{Path(k).stem}.pdf")

    pipe.apply(approve_all=False)

    ledger_path = root / "real_ledger.json"
    ledger = pipe.export_ledger(path=ledger_path)

    res = reconcile.reconcile(manifest, ledger, in_dir, done_dir)
    assert res["errors"] == []
    assert res["ok"] is True
