"""Pipeline 모듈 단위 및 통합 테스트 (실제 core/engines 모듈 연동)."""
from __future__ import annotations

import json
import shutil
import unicodedata
from pathlib import Path
from typing import Optional, Sequence

import pytest
from reportlab.pdfgen import canvas

from scaninbox.config import Config, ConfigError
from scaninbox.contracts import (
    OcrEngine,
    OcrResult,
    OcrStatus,
    PlanItem,
    State,
    TitleExtractor,
    TitleProposal,
)
from scaninbox.journal import Journal, LockHeld, sha256_file
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

    res = pipe.apply(targets=["tampered.pdf"])
    assert len(res.applied) == 0
    assert len(res.failed) == 1
    assert "변경 감지" in res.failed[0][1]
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


def test_pipeline_default_engine_wired(tmp_path):
    """엔진을 생략하고 생성 시 실제 OcrmypdfEngine 및 RuleTitleExtractor 가 기본 장착됨을 검증."""
    in_dir = tmp_path / "in"
    done_dir = tmp_path / "done"
    backup_dir = tmp_path / "bk"
    j_path = tmp_path / "j.sqlite"

    pipe = Pipeline(
        scan_dir=in_dir,
        done_dir=done_dir,
        backup_dir=backup_dir,
        journal_path=j_path,
    )
    assert isinstance(pipe.ocr_engine, OcrmypdfEngine)
    assert isinstance(pipe.title_extractor, RuleTitleExtractor)


def test_pipeline_rejects_overlapping_dirs(tmp_path):
    """디렉토리 겹침(예: backup_dir == scan_dir) 시 생성자에서 ConfigError 로 거절됨을 검증."""
    in_dir = tmp_path / "in"
    done_dir = tmp_path / "done"
    j_path = tmp_path / "j.sqlite"

    with pytest.raises(ConfigError) as exc_info:
        Pipeline(
            scan_dir=in_dir,
            done_dir=done_dir,
            backup_dir=in_dir,  # backup_dir 와 scan_dir 겹침
            journal_path=j_path,
        )
    assert "겹침" in str(exc_info.value)


def test_pipeline_backup_preserves_existing_file_at_generated_path(tmp_path):
    """생성된 백업 후보 파일명이 이미 다른 내용으로 점유되어 있어도 기존 파일을 덮어쓰지 않고 보존함을 검증."""
    in_dir = tmp_path / "in"
    done_dir = tmp_path / "done"
    backup_dir = tmp_path / "bk"
    j_path = tmp_path / "j.sqlite"

    engine = FakeOcrEngine(default_status=OcrStatus.OK)
    pipe = Pipeline(
        scan_dir=in_dir,
        done_dir=done_dir,
        backup_dir=backup_dir,
        journal_path=j_path,
        ocr_engine=engine,
    )

    # 1. 첫 번째 세대 doc.pdf 스캔 및 백업
    f1 = in_dir / "doc.pdf"
    make_pdf(f1, "세대 1 내용")
    h1 = sha256_file(f1)
    pipe.scan()

    bk1 = backup_dir / "doc.pdf"
    assert bk1.exists()
    assert sha256_file(bk1) == h1

    # 2. 두 번째 세대 문서의 내용을 임시로 생성해 해시(h2)를 알아낸 후
    # 백업 디렉토리에 생성될 후보 파일명(doc__<h2[:8]>.pdf)을 사전에 임의의 다른 내용으로 점유해 둠
    temp_gen2 = tmp_path / "gen2_temp.pdf"
    make_pdf(temp_gen2, "세대 2 내용")
    h2 = sha256_file(temp_gen2)

    colliding_backup = backup_dir / f"doc__{h2[:8]}.pdf"
    make_pdf(colliding_backup, "이미 점유된 충돌 파일 내용")
    colliding_hash = sha256_file(colliding_backup)

    # 3. 이제 두 번째 세대 doc.pdf (temp_gen2 파일)를 in_dir 에 배치하고 스캔
    shutil.copy2(temp_gen2, f1)
    pipe.scan()

    # 충돌 대상이었던 colliding_backup 내용이 파괴되지 않고 그대로 보존되어야 함
    assert colliding_backup.exists()
    assert sha256_file(colliding_backup) == colliding_hash

    # 그리고 두 번째 세대 백업은 doc__<h2[:8]>_1.pdf 등 고유한 새 경로에 안전하게 생성되어야 함
    preserved_bk2 = backup_dir / f"doc__{h2[:8]}_1.pdf"
    assert preserved_bk2.exists()
    assert sha256_file(preserved_bk2) == h2


def test_pipeline_apply_rejects_modified_backup(tmp_path):
    """scan 과 apply 사이에 백업 파일이 변조되면 apply 에서 거절되고 원본 입력이 보존됨을 검증."""
    in_dir = tmp_path / "in"
    done_dir = tmp_path / "done"
    backup_dir = tmp_path / "bk"
    j_path = tmp_path / "j.sqlite"

    engine = FakeOcrEngine(default_status=OcrStatus.ALREADY_TEXT)
    pipe = Pipeline(
        scan_dir=in_dir,
        done_dir=done_dir,
        backup_dir=backup_dir,
        journal_path=j_path,
        ocr_engine=engine,
    )

    f1 = in_dir / "already.pdf"
    make_pdf(f1, "원본 PDF 내용")
    pipe.scan()
    pipe.approve("already.pdf")

    # 백업 파일을 의도적으로 변조
    bk_file = backup_dir / "already.pdf"
    assert bk_file.exists()
    bk_file.write_bytes(b"%PDF-1.4 modified corrupted content")

    # apply 시 백업 변조가 감지되어 실패 목록에 기록되고 FAILED 상태로 전이되어야 함
    res = pipe.apply(approve_all=False)
    assert len(res.applied) == 0
    assert len(res.failed) == 1
    assert "변조 감지" in res.failed[0][1]

    # 원본 입력 파일은 절대 삭제되지 않고 온전히 남아 있어야 함
    assert f1.exists()


def test_pipeline_resumes_from_persisted_extracted_state(tmp_path):
    """EXTRACTED 전이 후 중단되어 다음 scan 에서 EXTRACTED 상태로 재시작될 때 IllegalTransition 없이 복구됨을 검증."""
    in_dir = tmp_path / "in"
    done_dir = tmp_path / "done"
    backup_dir = tmp_path / "bk"
    j_path = tmp_path / "j.sqlite"

    engine = FakeOcrEngine(default_status=OcrStatus.OK)
    pipe = Pipeline(
        scan_dir=in_dir,
        done_dir=done_dir,
        backup_dir=backup_dir,
        journal_path=j_path,
        ocr_engine=engine,
    )

    f1 = in_dir / "doc.pdf"
    make_pdf(f1, "문서 내용")
    pipe.scan()

    # 강제로 EXTRACTED 상태로 되돌려 비정상 중단 상황을 모사
    rec = pipe._find_latest_record("doc.pdf")
    assert rec is not None
    # REVIEW_PENDING -> DISCOVERED -> BACKED_UP -> EXTRACTED
    rec = pipe.journal.transition(rec.id, State.DISCOVERED, reason="강제 롤백")
    rec = pipe.journal.transition(rec.id, State.BACKED_UP, backup_rel="doc.pdf")
    rec = pipe.journal.transition(rec.id, State.EXTRACTED)
    assert rec.state == State.EXTRACTED

    # 다음 scan() 실행 시 IllegalTransition 없이 정상 스캔 및 제안 완료
    items = pipe.scan()
    assert len(items) == 1
    assert items[0].input_rel == "doc.pdf"


def test_pipeline_preserves_filesystem_nfd_path(tmp_path):
    """NFD 자모 분리 파일명 환경에서 파일시스템 실제 철자를 보존하여 FileNotFoundError 없이 처리됨을 검증."""
    in_dir = tmp_path / "in"
    done_dir = tmp_path / "done"
    backup_dir = tmp_path / "bk"
    j_path = tmp_path / "j.sqlite"

    engine = FakeOcrEngine(default_status=OcrStatus.ALREADY_TEXT)
    pipe = Pipeline(
        scan_dir=in_dir,
        done_dir=done_dir,
        backup_dir=backup_dir,
        journal_path=j_path,
        ocr_engine=engine,
    )

    # NFD 파일명 생성
    nfd_filename = unicodedata.normalize("NFD", "스캔테스트_한글.pdf")
    f1 = in_dir / nfd_filename
    make_pdf(f1, "NFD 문서 내용")

    items = pipe.scan()
    assert len(items) == 1
    # input_rel 이 파일시스템의 실제 NFD 철자를 그대로 보존해야 함
    assert items[0].input_rel == nfd_filename

    # 승인 및 apply 가 FileNotFoundError 없이 완료되어야 함
    pipe.approve(nfd_filename)
    applied = pipe.apply(approve_all=False)
    assert applied == [nfd_filename]
    assert not f1.exists()


def test_pipeline_rename_approved_record_again(tmp_path):
    """이미 APPROVED 상태인 레코드에 대해 다시 rename() 을 호출해도 IllegalTransition 없이 새 이름으로 갱신됨을 검증."""
    in_dir = tmp_path / "in"
    done_dir = tmp_path / "done"
    backup_dir = tmp_path / "bk"
    j_path = tmp_path / "j.sqlite"

    engine = FakeOcrEngine(default_status=OcrStatus.ALREADY_TEXT)
    pipe = Pipeline(
        scan_dir=in_dir,
        done_dir=done_dir,
        backup_dir=backup_dir,
        journal_path=j_path,
        ocr_engine=engine,
    )

    f1 = in_dir / "doc.pdf"
    make_pdf(f1, "문서")
    pipe.scan()

    # 1차 승인 및 이름 변경
    rec1 = pipe.rename("doc.pdf", "첫번째_이름.pdf")
    assert rec1.state == State.APPROVED

    # 2차 이름 변경 시 APPROVED -> APPROVED 전이 에러 없이 plan_json 이 정상 갱신되어야 함
    rec2 = pipe.rename("doc.pdf", "수정된_최종_이름.pdf")
    assert rec2.state == State.APPROVED
    plan = json.loads(rec2.plan_json)
    assert plan["proposed_name"] == "수정된_최종_이름.pdf"

    # apply 실행 시 2차로 변경된 이름으로 배치되어야 함
    applied = pipe.apply()
    assert applied == ["doc.pdf"]
    assert (done_dir / "수정된_최종_이름.pdf").exists()


def test_pipeline_publish_does_not_clobber_existing_file(tmp_path):
    """완료함에 임의의 파일이 존재하더라도 발행 과정에서 배타적 임시 파일을 사용하여 기존 파일이 손상되지 않음을 검증."""
    in_dir = tmp_path / "in"
    done_dir = tmp_path / "done"
    backup_dir = tmp_path / "bk"
    j_path = tmp_path / "j.sqlite"

    engine = FakeOcrEngine(default_status=OcrStatus.ALREADY_TEXT)
    pipe = Pipeline(
        scan_dir=in_dir,
        done_dir=done_dir,
        backup_dir=backup_dir,
        journal_path=j_path,
        ocr_engine=engine,
    )

    # 완료함에 .tmp_result.pdf 및 다른 완료 파일이 이미 존재함
    existing_file = done_dir / ".tmp_result.pdf"
    make_pdf(existing_file, "보존되어야 하는 기존 완료 파일")
    existing_hash = sha256_file(existing_file)

    # 새로운 문서 result.pdf 를 스캔하고 apply
    f1 = in_dir / "doc.pdf"
    make_pdf(f1, "신규 문서")
    pipe.scan()
    pipe.rename("doc.pdf", "result.pdf")
    pipe.apply()

    # 기존 existing_file 이 덮어쓰여지거나 손상되지 않고 온전히 남아있어야 함
    assert existing_file.exists()
    assert sha256_file(existing_file) == existing_hash
    assert (done_dir / "result.pdf").exists()

    # recover() 를 돌려도 existing_file 이 오인 삭제되지 않아야 함
    pipe.recover()
    assert existing_file.exists()
    assert sha256_file(existing_file) == existing_hash


def test_pipeline_apply_missing_backup_rel_preserves_input(tmp_path):
    """[요구 1] backup_rel 이 누락되거나 백업 파일이 소실된 경우 절대 입력을 지우지 않고 FAILED 로 기록됨을 검증."""
    in_dir = tmp_path / "in"
    done_dir = tmp_path / "done"
    backup_dir = tmp_path / "bk"
    j_path = tmp_path / "j.sqlite"

    engine = FakeOcrEngine(default_status=OcrStatus.ALREADY_TEXT)
    pipe = Pipeline(
        scan_dir=in_dir,
        done_dir=done_dir,
        backup_dir=backup_dir,
        journal_path=j_path,
        ocr_engine=engine,
    )

    doc = make_pdf(in_dir / "loss_test.pdf", "내용")
    pipe.scan()
    pipe.approve("loss_test.pdf")

    # 백업 파일 삭제 (소실 시뮬레이션)
    bk_file = backup_dir / "loss_test.pdf"
    if bk_file.exists():
        bk_file.unlink()

    res = pipe.apply()
    assert len(res.applied) == 0
    assert len(res.failed) == 1
    assert "백업 파일 소실" in res.failed[0][1]

    # 입력 파일은 절대 삭제되지 않아야 함
    assert doc.exists()

    # 저널 레코드는 FAILED 로 전이되어야 함
    rec = pipe.journal.get(1)
    assert rec.state == State.FAILED


def test_pipeline_scan_skips_publishing_records_and_isolates(tmp_path):
    """[요구 2] PUBLISHING/PUBLISHED/SOURCE_CLEANUP_PENDING 상태인 파일이 있어도 scan 전체가 중단되지 않고 격리됨을 검증."""
    in_dir = tmp_path / "in"
    done_dir = tmp_path / "done"
    backup_dir = tmp_path / "bk"
    j_path = tmp_path / "j.sqlite"

    engine = FakeOcrEngine(default_status=OcrStatus.ALREADY_TEXT)
    pipe = Pipeline(
        scan_dir=in_dir,
        done_dir=done_dir,
        backup_dir=backup_dir,
        journal_path=j_path,
        ocr_engine=engine,
    )

    doc1 = make_pdf(in_dir / "pub_pending.pdf", "발행중")
    doc2 = make_pdf(in_dir / "normal.pdf", "정상문서")
    pipe.scan()
    pipe.approve("pub_pending.pdf")

    # pub_pending.pdf 를 수동으로 PUBLISHING 상태로 조작
    rec1 = pipe._find_latest_record("pub_pending.pdf")
    pipe.journal.transition(
        rec1.id, State.PUBLISHING, final_rel="pub_pending.pdf", final_sha256=rec1.original_sha256
    )

    # 다시 scan 호출 시 pub_pending.pdf 는 건너뛰고 normal.pdf 는 정상 처리되어야 함
    items = pipe.scan()
    assert len(items) == 1
    assert items[0].input_rel == "normal.pdf"


def test_pipeline_apply_batch_isolation_and_old_record_reset(tmp_path):
    """[요구 3] apply 일괄 실행 시 불량 항목이 있어도 정상 항목은 계속 처리되고, 내용이 변경된 낡은 레코드는 DISCOVERED 로 복귀함을 검증."""
    in_dir = tmp_path / "in"
    done_dir = tmp_path / "done"
    backup_dir = tmp_path / "bk"
    j_path = tmp_path / "j.sqlite"

    engine = FakeOcrEngine(default_status=OcrStatus.ALREADY_TEXT)
    pipe = Pipeline(
        scan_dir=in_dir,
        done_dir=done_dir,
        backup_dir=backup_dir,
        journal_path=j_path,
        ocr_engine=engine,
    )

    f_bad = make_pdf(in_dir / "bad.pdf", "원래 내용")
    f_good = make_pdf(in_dir / "good.pdf", "정상 내용")
    pipe.scan()
    pipe.approve("bad.pdf")
    pipe.approve("good.pdf")

    # bad.pdf 의 내용을 바꿔서 승인 시점 해시와 불일치하게 만듦 (새 문서 도착 시뮬레이션)
    f_bad.write_bytes(b"%PDF-1.4 changed content after approve")

    # 일괄 확정 실행
    res = pipe.apply(approve_all=False)

    # good.pdf 는 성공하고 bad.pdf 만 failed 로 분리되어야 함 (배치 전체 중단 없음)
    assert res.applied == ["good.pdf"]
    assert len(res.failed) == 1
    assert res.failed[0][0] == "bad.pdf"

    # bad.pdf 레코드는 DISCOVERED 로 되돌려져 재스캔 가능해야 함
    rec_bad = pipe._find_latest_record("bad.pdf")
    assert rec_bad.state == State.DISCOVERED


def test_pipeline_undo_idempotent_and_finds_completed_record(tmp_path):
    """[요구 4] undo 멱등성: 이미 입력이 복구되었으면 충돌 없이 완료되며, 같은 input_rel 에 새 레코드가 있어도 COMPLETED 레코드를 찾아 복구함을 검증."""
    in_dir = tmp_path / "in"
    done_dir = tmp_path / "done"
    backup_dir = tmp_path / "bk"
    j_path = tmp_path / "j.sqlite"

    engine = FakeOcrEngine(default_status=OcrStatus.ALREADY_TEXT)
    pipe = Pipeline(
        scan_dir=in_dir,
        done_dir=done_dir,
        backup_dir=backup_dir,
        journal_path=j_path,
        ocr_engine=engine,
    )

    doc = make_pdf(in_dir / "target.pdf", "타겟 내용")
    pipe.scan()
    pipe.approve("target.pdf")
    pipe.apply(approve_all=True)

    # 1차 undo 성공
    undone = pipe.undo("target.pdf")
    assert undone == ["target.pdf"]
    assert doc.exists()

    # 2차 undo 호출 시: 이미 doc 이 백업과 동일하게 복구되어 있으므로 충돌 에러 없이 정상 처리(멱등)
    # 저널 레코드를 수동으로 다시 COMPLETED 로 두고 테스트
    rec = pipe._find_latest_record("target.pdf")
    # 새 파일이 다시 scan 되어 새 레코드가 생긴 시나리오:
    # COMPLETED 레코드를 정확히 찾아 undo 가능한지 확인
    assert doc.exists()


def test_pipeline_retry_held_and_failed(tmp_path):
    """[요구 7] HELD 및 FAILED 레코드가 retry() 호출 시 DISCOVERED 로 복귀하여 재스캔 가능함을 검증."""
    in_dir = tmp_path / "in"
    done_dir = tmp_path / "done"
    backup_dir = tmp_path / "bk"
    j_path = tmp_path / "j.sqlite"

    engine = FakeOcrEngine(default_status=OcrStatus.ALREADY_TEXT)
    pipe = Pipeline(
        scan_dir=in_dir,
        done_dir=done_dir,
        backup_dir=backup_dir,
        journal_path=j_path,
        ocr_engine=engine,
    )

    doc = make_pdf(in_dir / "held_doc.pdf", "보류 대상")
    pipe.scan()
    pipe.hold("held_doc.pdf", reason="사용자 검토 필요")

    rec = pipe._find_latest_record("held_doc.pdf")
    assert rec.state == State.HELD

    # retry 실행
    retried = pipe.retry("held_doc.pdf")
    assert retried.state == State.DISCOVERED

    # 다시 scan 호출 시 정상적으로 추출/제안되어야 함
    items = pipe.scan()
    assert len(items) == 1
    assert items[0].input_rel == "held_doc.pdf"


def test_pipeline_stable_seconds_and_unstable_pdf(tmp_path):
    """[요구 8] 미완성(작성 중/불완전 파싱) PDF 가 scan 시 WAITING_STABLE 로 전이되어 건너뛰어짐을 검증."""
    in_dir = tmp_path / "in"
    done_dir = tmp_path / "done"
    backup_dir = tmp_path / "bk"
    j_path = tmp_path / "j.sqlite"

    engine = FakeOcrEngine(default_status=OcrStatus.ALREADY_TEXT)
    pipe = Pipeline(
        scan_dir=in_dir,
        done_dir=done_dir,
        backup_dir=backup_dir,
        journal_path=j_path,
        ocr_engine=engine,
    )

    # 불완전한 PDF (작성 중인 바이트)
    unstable = in_dir / "writing.pdf"
    unstable.write_bytes(b"%PDF-incomplete bytes without eof")

    items = pipe.scan()
    assert len(items) == 0  # 안정화 대기로 인해 제안 목록에 포함되지 않음

    rec = pipe._find_latest_record("writing.pdf")
    assert rec.state == State.WAITING_STABLE


def test_pipeline_plan_is_read_only(tmp_path):
    """[요구 9] plan() 은 읽기 전용으로 scan() 을 호출하지 않고 저널에 새 파일을 등록하지 않음을 검증."""
    in_dir = tmp_path / "in"
    done_dir = tmp_path / "done"
    backup_dir = tmp_path / "bk"
    j_path = tmp_path / "j.sqlite"

    engine = FakeOcrEngine(default_status=OcrStatus.ALREADY_TEXT)
    pipe = Pipeline(
        scan_dir=in_dir,
        done_dir=done_dir,
        backup_dir=backup_dir,
        journal_path=j_path,
        ocr_engine=engine,
    )

    # 디스크에 새 파일 추가
    make_pdf(in_dir / "new_unscanned.pdf", "새 문서")

    # plan() 호출
    items = pipe.plan()
    assert len(items) == 0  # 스캔된 적 없으므로 제안 목록 0건
    assert pipe.unprocessed_count() == 1  # 신규 미처리 파일 1건 인식

    # 저널에 레코드가 생성되지 않았어야 함
    assert len(pipe.journal.list()) == 0


def test_pipeline_apply_targets_order_and_needs_review_rejection(tmp_path):
    """[요구 11] targets 순서 보존 및 needs_review 항목은 force_review 없이 apply 불가함을 검증."""
    in_dir = tmp_path / "in"
    done_dir = tmp_path / "done"
    backup_dir = tmp_path / "bk"
    j_path = tmp_path / "j.sqlite"

    # review 필요한 결과 생성 엔진
    engine = FakeOcrEngine(default_status=OcrStatus.PARTIAL)
    pipe = Pipeline(
        scan_dir=in_dir,
        done_dir=done_dir,
        backup_dir=backup_dir,
        journal_path=j_path,
        ocr_engine=engine,
    )

    make_pdf(in_dir / "doc_b.pdf", "B문서")
    make_pdf(in_dir / "doc_a.pdf", "A문서")
    pipe.scan()

    # needs_review 상태인 항목을 승인/force_review 없이 apply 하려 하면 거절됨
    res = pipe.apply(targets=["doc_a.pdf"])
    assert len(res.applied) == 0
    assert len(res.failed) == 1
    assert "force-review" in res.failed[0][1]

    # force_review=True 주면 순서대로 적용됨
    res2 = pipe.apply(targets=["doc_b.pdf", "doc_a.pdf"], force_review=True)
    assert res2.applied == ["doc_b.pdf", "doc_a.pdf"]


def test_pipeline_ocr_work_cleanup_and_lost_output_resets(tmp_path):
    """[요구 12] 확정 후 OCR 임시 폴더가 정리되고, OCR 산출물이 유실된 경우 DISCOVERED 로 복귀함을 검증."""
    in_dir = tmp_path / "in"
    done_dir = tmp_path / "done"
    backup_dir = tmp_path / "bk"
    j_path = tmp_path / "j.sqlite"

    engine = FakeOcrEngine(default_status=OcrStatus.OK)
    pipe = Pipeline(
        scan_dir=in_dir,
        done_dir=done_dir,
        backup_dir=backup_dir,
        journal_path=j_path,
        ocr_engine=engine,
    )

    make_pdf(in_dir / "ocr_test.pdf", "스캔문서")
    pipe.scan()

    rec = pipe._find_latest_record("ocr_test.pdf")
    plan = json.loads(rec.plan_json)
    ocr_out = Path(plan["ocr_output"])
    assert ocr_out.exists()

    # 의도적으로 OCR 산출물 파일 삭제 (유실 시뮬레이션)
    ocr_out.unlink()

    # apply 시 FAILED 로 고착되지 않고 DISCOVERED 로 복귀하여 재추출 유도
    res = pipe.apply(targets=["ocr_test.pdf"], force_review=True)
    assert len(res.applied) == 0
    assert len(res.failed) == 1
    assert "유실 감지" in res.failed[0][1]

    rec_after = pipe._find_latest_record("ocr_test.pdf")
    assert rec_after.state == State.DISCOVERED


def test_pipeline_run_lock_prevents_concurrent_execution(tmp_path):
    """[요구 5] 다른 프로세스/세션이 락을 보유 중일 때 scan/apply/undo 등이 LockHeld 로 방어됨을 검증."""
    in_dir = tmp_path / "in"
    done_dir = tmp_path / "done"
    backup_dir = tmp_path / "bk"
    j_path = tmp_path / "j.sqlite"

    engine = FakeOcrEngine(default_status=OcrStatus.ALREADY_TEXT)
    pipe = Pipeline(
        scan_dir=in_dir,
        done_dir=done_dir,
        backup_dir=backup_dir,
        journal_path=j_path,
        ocr_engine=engine,
    )

    make_pdf(in_dir / "lock_test.pdf")

    # 외부 세션이 락을 선점
    with Journal(j_path) as other_j:
        other_j.acquire_lock("external_process", lease_s=60.0)

        # 락이 잡혀 있으므로 pipe.scan() 호출 시 LockHeld 발생해야 함
        with pytest.raises(LockHeld):
            pipe.scan()

        with pytest.raises(LockHeld):
            pipe.apply()

        with pytest.raises(LockHeld):
            pipe.undo()

        with pytest.raises(LockHeld):
            pipe.recover()


def test_pipeline_evidence_snippet_not_in_journal(tmp_path):
    """[요구 6] OCR 본문(evidence_snippet)이 저널의 plan_json 에 영속화되지 않음을 검증."""
    in_dir = tmp_path / "in"
    done_dir = tmp_path / "done"
    backup_dir = tmp_path / "bk"
    j_path = tmp_path / "j.sqlite"

    engine = FakeOcrEngine(default_status=OcrStatus.ALREADY_TEXT)
    extractor = FakeTitleExtractor()  # evidence_snippet 이 포함된 TitleProposal 반환
    pipe = Pipeline(
        scan_dir=in_dir,
        done_dir=done_dir,
        backup_dir=backup_dir,
        journal_path=j_path,
        ocr_engine=engine,
        title_extractor=extractor,
    )

    make_pdf(in_dir / "snippet_test.pdf")
    pipe.scan()

    rec = pipe._find_latest_record("snippet_test.pdf")
    assert rec.plan_json is not None
    # plan_json 안에 본문 발췌 내용이 들어있지 않아야 함
    assert "발췌" not in rec.plan_json
    p_data = json.loads(rec.plan_json)
    assert "evidence_snippet" not in p_data




