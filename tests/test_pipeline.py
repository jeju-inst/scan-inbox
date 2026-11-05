"""Pipeline 모듈 단위 및 통합 테스트."""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Optional, Sequence

import pytest
from reportlab.pdfgen import canvas

from scaninbox.contracts import (
    OcrEngine,
    OcrResult,
    OcrStatus,
    PlanItem,
    State,
    TitleExtractor,
    TitleProposal,
)
from scaninbox.pipeline import InMemoryJournal, Pipeline, compute_sha256
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
        if "multi_doc" in name:
            return TitleProposal(
                title="다중문서묶음",
                evidence_page=1,
                evidence_snippet="다중 문서 의심",
                doc_date=self.default_date,
                review_reasons=["다중 문서 묶음 의심 (쪽마다 제목 반복)"],
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
    in_dir.mkdir()
    done_dir.mkdir()
    backup_dir.mkdir()
    return in_dir, done_dir, backup_dir


def test_pipeline_normal_flow(env_dirs):
    in_dir, done_dir, backup_dir = env_dirs
    doc = make_pdf(in_dir / "sample1.pdf", "정상 문서 텍스트")
    orig_sha256 = compute_sha256(doc)

    ocr = FakeOcrEngine(OcrStatus.OK)
    title = FakeTitleExtractor(title="업무보고서", doc_date="2026-11-05")
    pipe = Pipeline(
        input_dir=in_dir,
        done_dir=done_dir,
        backup_dir=backup_dir,
        ocr_engine=ocr,
        title_extractor=title,
    )

    # 1. scan
    items = pipe.scan()
    assert len(items) == 1
    item = items[0]
    assert item.ocr_status == OcrStatus.OK
    assert item.proposed_name == f"2026-11-05_업무보고서__{orig_sha256[:8]}.pdf"
    assert not item.needs_review

    # 2. plan
    plan_items = pipe.plan()
    assert len(plan_items) == 1

    # 3. apply
    applied = pipe.apply(approve_all=True)
    assert len(applied) == 1
    assert applied[0] == "sample1.pdf"

    # 검증: 입력 파일 삭제됨, 완료 파일 배치됨, 백업 파일 보존됨
    assert not doc.exists()
    final_file = done_dir / f"2026-11-05_업무보고서__{orig_sha256[:8]}.pdf"
    assert final_file.exists()
    backup_file = backup_dir / "sample1.pdf"
    assert backup_file.exists()
    assert compute_sha256(backup_file) == orig_sha256

    # 저널 엔트리 확인
    entry = pipe.journal.get_entry("sample1.pdf")
    assert entry is not None
    assert entry.state == State.COMPLETED
    assert entry.approved_by_user is True


def test_pipeline_review_paths(env_dirs):
    in_dir, done_dir, backup_dir = env_dirs
    f1 = make_pdf(in_dir / "partial_doc.pdf")
    f2 = make_pdf(in_dir / "unknown_title.pdf")
    f3 = make_pdf(in_dir / "unknown_date.pdf")

    ocr = FakeOcrEngine()
    title = FakeTitleExtractor()
    pipe = Pipeline(
        input_dir=in_dir,
        done_dir=done_dir,
        backup_dir=backup_dir,
        ocr_engine=ocr,
        title_extractor=title,
    )

    items = pipe.scan()
    assert len(items) == 3
    for it in items:
        assert it.needs_review is True

    # 검토 항목 중 하나에 이름 지정(rename) 후 승인 및 적용
    pipe.rename("unknown_title.pdf", "2026-11-05_수동지정이름__12345678.pdf")
    applied = pipe.apply(targets=["unknown_title.pdf"])
    assert applied == ["unknown_title.pdf"]
    assert not f2.exists()
    assert (done_dir / "2026-11-05_수동지정이름__12345678.pdf").exists()


def test_pipeline_held_and_failed_paths(env_dirs):
    in_dir, done_dir, backup_dir = env_dirs
    f_enc = make_pdf(in_dir / "encrypted_file.pdf")
    f_cor = make_pdf(in_dir / "corrupt_file.pdf")
    f_fail = make_pdf(in_dir / "failed_ocr.pdf")

    ocr = FakeOcrEngine()
    title = FakeTitleExtractor()
    pipe = Pipeline(
        input_dir=in_dir,
        done_dir=done_dir,
        backup_dir=backup_dir,
        ocr_engine=ocr,
        title_extractor=title,
    )

    items = pipe.scan()
    # held/failed 항목은 planItems 에 들어가지 않고 저널에만 기록됨
    assert len(items) == 0

    e_enc = pipe.journal.get_entry("encrypted_file.pdf")
    assert e_enc is not None and e_enc.state == State.HELD
    assert "암호화" in e_enc.reason

    e_cor = pipe.journal.get_entry("corrupt_file.pdf")
    assert e_cor is not None and e_cor.state == State.FAILED

    e_fail = pipe.journal.get_entry("failed_ocr.pdf")
    assert e_fail is not None and e_fail.state == State.FAILED

    # 중요: 오류/보류 파일은 입력 폴더에 그대로 남아있어야 함
    assert f_enc.exists()
    assert f_cor.exists()
    assert f_fail.exists()


def test_pipeline_input_hash_change_rejected(env_dirs):
    in_dir, done_dir, backup_dir = env_dirs
    doc = make_pdf(in_dir / "tampered.pdf", "처음 내용")

    ocr = FakeOcrEngine()
    title = FakeTitleExtractor()
    pipe = Pipeline(
        input_dir=in_dir,
        done_dir=done_dir,
        backup_dir=backup_dir,
        ocr_engine=ocr,
        title_extractor=title,
    )

    pipe.scan()
    pipe.approve("tampered.pdf")

    # 승인 후 입력 파일 내용 변조
    doc.write_bytes(b"%PDF-1.4 modified bytes unexpected")

    # apply 시도 시 해시 불일치로 거부되어야 함
    with pytest.raises(ValueError, match="해시와 불일치"):
        pipe.apply(targets=["tampered.pdf"])

    # 입력 파일이 삭제되지 않고 남아있어야 함
    assert doc.exists()
    # 완료 폴더에 최종본이 생기지 않음
    assert len(list(done_dir.glob("*.pdf"))) == 0


def test_pipeline_crash_injection_and_recovery(env_dirs):
    in_dir, done_dir, backup_dir = env_dirs
    doc = make_pdf(in_dir / "crash_test.pdf", "장애 복구 시험")
    orig_sha256 = compute_sha256(doc)

    pipe = Pipeline(
        input_dir=in_dir,
        done_dir=done_dir,
        backup_dir=backup_dir,
        ocr_engine=FakeOcrEngine(),
        title_extractor=FakeTitleExtractor(),
    )
    pipe.scan()

    # 장애 시나리오 1: 백업 후 중단 (State.BACKED_UP)
    entry = pipe.journal.get_entry("crash_test.pdf")
    entry.state = State.BACKED_UP
    recovered = pipe.recover()
    assert "crash_test.pdf" in recovered

    # 장애 시나리오 2: 최종 배치 완료 후 입력 정리 전 중단
    # done_dir 에 최종본 파일이 이미 존재하는 상황 모의
    final_name = "2026-11-05_최종본__" + orig_sha256[:8] + ".pdf"
    final_p = done_dir / final_name
    shutil.copy2(doc, final_p)
    final_sha256 = compute_sha256(final_p)

    entry.state = State.PUBLISHED
    entry.final_path = final_name
    entry.final_sha256 = final_sha256

    # recover 실행: 중복 최종본 생성 없이 입력 파일만 정리되고 completed 전이되어야 함
    rec = pipe.recover()
    assert "crash_test.pdf" in rec
    assert not doc.exists()  # 입력 파일 정리됨
    assert final_p.exists()  # 최종본 1개만 존재
    assert len(list(done_dir.glob("*.pdf"))) == 1
    assert entry.state == State.COMPLETED


def test_pipeline_plan_leaves_input_folder_intact(env_dirs):
    in_dir, done_dir, backup_dir = env_dirs
    make_pdf(in_dir / "sub" / "doc1.pdf", "문서 1")
    make_pdf(in_dir / "doc2.pdf", "문서 2")

    # 사전 스냅샷
    before = snapshot.take(in_dir)

    pipe = Pipeline(
        input_dir=in_dir,
        done_dir=done_dir,
        backup_dir=backup_dir,
        ocr_engine=FakeOcrEngine(),
        title_extractor=FakeTitleExtractor(),
    )

    # scan 및 plan 수행
    items = pipe.scan()
    assert len(items) == 2
    plan_items = pipe.plan()
    assert len(plan_items) == 2

    # 사후 스냅샷
    after = snapshot.take(in_dir)
    diff = snapshot.compare(before, after)
    assert snapshot.is_clean(diff)  # 이름, 크기, 해시, mtime 100% 동일 확인!


def test_pipeline_undo_success_and_conflict(env_dirs):
    in_dir, done_dir, backup_dir = env_dirs
    doc = make_pdf(in_dir / "undo_test.pdf", "되돌리기 시험")
    orig_sha256 = compute_sha256(doc)

    pipe = Pipeline(
        input_dir=in_dir,
        done_dir=done_dir,
        backup_dir=backup_dir,
        ocr_engine=FakeOcrEngine(),
        title_extractor=FakeTitleExtractor(),
    )
    pipe.scan()
    pipe.apply(approve_all=True)

    assert not doc.exists()
    final_files = list(done_dir.glob("*.pdf"))
    assert len(final_files) == 1

    # 1. 정상 되돌리기
    undone = pipe.undo("undo_test.pdf")
    assert undone == ["undo_test.pdf"]
    assert doc.exists()
    assert compute_sha256(doc) == orig_sha256
    assert len(list(done_dir.glob("*.pdf"))) == 0

    # 2. 충돌 케이스: 원본 경로에 이미 다른 파일이 존재할 때
    # 다시 apply 수행
    pipe.scan()
    pipe.apply(approve_all=True)
    assert not doc.exists()

    # 입력 경로에 타 파일이 생김
    make_pdf(doc, "방해 파일")
    with pytest.raises(FileExistsError, match="되돌리기 충돌"):
        pipe.undo("undo_test.pdf")

    # 타 파일이 지워지지 않고 보존됨
    assert doc.exists()


def test_pipeline_export_ledger_and_reconcile_match(work, tmp_path):
    root, manifest = work
    in_dir = root / "input"
    done_dir = root / "done_preexisting"
    backup_dir = root / "backup_test"
    backup_dir.mkdir(parents=True, exist_ok=True)

    # manifest 의 기대 결과(expected_outcome)에 맞춰 fake 엔진 설정
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
        input_dir=in_dir,
        done_dir=done_dir,
        backup_dir=backup_dir,
        ocr_engine=ocr,
        title_extractor=title,
    )

    # 1. 스캔
    pipe.scan()

    # 2. complete 대상 및 review 대상 처리
    # review 건은 사용자가 명시 승인(rename)하여 완료 가능하게 함
    for k, info in by_file.items():
        if info["expected_outcome"] == "complete":
            pipe.approve(k)
        elif info["expected_outcome"] == "review":
            # review 건 명시 승인
            pipe.rename(k, f"승인완료_{Path(k).stem}.pdf")

    # 3. 승인된 항목 확정 적용
    pipe.apply(approve_all=False)

    # 4. ledger 내보내기
    ledger = pipe.export_ledger()

    # 5. reconcile 로 검증 (위반 0건이어야 함)
    res = reconcile.reconcile(manifest, ledger, in_dir, done_dir)
    assert res["errors"] == []
    assert res["ok"] is True
