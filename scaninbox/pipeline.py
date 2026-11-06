"""Scan Inbox 파이프라인 (트랙 C).

core(pathsafe, config, journal)와 engines(ocr, title)의 실제 구현 모듈을 조합하여
발견 → 등록 → 보호 → 추출 → 제안 → 승인 → 확정 → 입력정리 → 완료의 수직 절편을 수행합니다.

pipeline 규약 (docs/G1-DESIGN.md 및 scaninbox.journal docstring):
1. Undo: 원본을 입력에 되돌린 뒤 COMPLETED → DISCOVERED 로만 재등록 (UNDONE 상태 없음).
2. 사용자 승인: APPROVED 및 COMPLETED 전이 시 approved_by_user=True 명시.
3. recover: journal.recover() 는 입력을 지우지 않고 needs_source_cleanup 으로 알리므로,
   입력 파일 해시 검증 후 삭제 및 COMPLETED 전이는 pipeline 이 수행.
4. 기록 순서: BACKED_UP 에 backup_rel 기록. PUBLISHING 전에 final_rel/final_sha256 기록 후
   pathsafe.move_no_overwrite 로 원자적 배치하고 PUBLISHED 전이.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence

from scaninbox.config import Config, validate
from scaninbox.contracts import (
    OcrEngine,
    OcrResult,
    OcrStatus,
    PlanItem,
    State,
    TitleExtractor,
    TitleProposal,
)
from scaninbox.journal import (
    FileRecord,
    IllegalTransition,
    Journal,
    JournalError,
    LockHeld,
    LockLost,
    RecoveryAction,
    sha256_file,
)
from scaninbox.ocr import OcrmypdfEngine
from scaninbox.pathsafe import (
    ensure_within,
    is_safe_component,
    move_no_overwrite,
    nfc,
    proposed_name,
    unique_name,
)
from scaninbox.title import RuleTitleExtractor
from pypdf import PdfReader

log = logging.getLogger("scaninbox.pipeline")


_UNSET = object()


@dataclass
class ApplyResult(Sequence[str]):
    """apply() 실행 결과: 성공한 파일 목록과 실패/제외된 목록(항목 격리)."""

    applied: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.applied)

    def __getitem__(self, i: int) -> str:
        return self.applied[i]

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, ApplyResult):
            return self.applied == other.applied and self.failed == other.failed
        if isinstance(other, list):
            return self.applied == other
        return False


class Pipeline:
    """Scan Inbox 핵심 파이프라인."""

    def __init__(
        self,
        config: Optional[Config] = None,
        *,
        scan_dir: Optional[Path | str] = None,
        done_dir: Optional[Path | str] = None,
        backup_dir: Optional[Path | str] = None,
        journal_path: Optional[Path | str] = None,
        work_dir: Optional[Path | str] = None,
        ocr_engine: Any = _UNSET,
        title_extractor: Optional[TitleExtractor] = None,
        journal: Optional[Journal] = None,
        langs: str = "kor+eng",
        create_dirs: bool = True,
    ) -> None:
        if config is not None:
            self.config = config
        elif scan_dir is not None:
            self.config = Config.with_defaults(
                scan_dir=scan_dir,
                done_dir=done_dir,
                backup_dir=backup_dir,
                journal_path=journal_path,
                ocr_langs=langs,
            )
        else:
            raise ValueError("config 또는 scan_dir 가 필요합니다.")

        # 폴더 생성 전 설정 및 경로 유효성(겹침, 심링크 등) 검증
        validate(self.config, require_scan_exists=False)

        self.scan_dir = Path(self.config.scan_dir).resolve()
        self.done_dir = Path(self.config.done_dir).resolve()
        self.backup_dir = Path(self.config.backup_dir).resolve()
        self.work_dir = (
            Path(work_dir).resolve() if work_dir else self.backup_dir / ".tmp"
        )
        self.langs = self.config.ocr_langs

        if create_dirs:
            self.scan_dir.mkdir(parents=True, exist_ok=True)
            self.done_dir.mkdir(parents=True, exist_ok=True)
            self.backup_dir.mkdir(parents=True, exist_ok=True)
            self.work_dir.mkdir(parents=True, exist_ok=True)

        self.journal = (
            journal if journal is not None else Journal(self.config.journal_path)
        )
        if ocr_engine is _UNSET:
            self.ocr_engine: Optional[OcrEngine] = OcrmypdfEngine()
        else:
            self.ocr_engine = ocr_engine
        self.title_extractor = title_extractor or RuleTitleExtractor()

    def _relative_key(self, path: Path) -> str:
        """scan_dir 기준 상대경로 (파일시스템 실제 철자 보존)."""
        return path.relative_to(self.scan_dir).as_posix()

    def scan(self) -> list[PlanItem]:
        """발견→등록→보호→추출→제안 단계 수행 (입력 무변경 보장, 잠금 및 격리 적용)."""
        with self.journal.run_lock(lease_s=float(self.config.lease_seconds)):
            plan_items: list[PlanItem] = []
            if not self.scan_dir.exists():
                return plan_items

            for path in sorted(self.scan_dir.rglob("*")):
                if not path.is_file() or path.is_symlink():
                    continue
                if path.suffix.lower() != ".pdf":
                    continue

                try:
                    rel = self._relative_key(path)
                    # 루트 탈출 검사
                    try:
                        ensure_within(self.scan_dir, rel)
                    except Exception as e:
                        log.warning("루트 밖 탈출 경로 무시: %s (%s)", rel, e)
                        continue

                    sha256 = sha256_file(path)
                    st = path.stat()

                    # 1. 등록 (DISCOVERED)
                    rec = self.journal.register(
                        rel, sha256, size=st.st_size, mtime_ns=st.st_mtime_ns
                    )

                    # 발행 및 정리 대기 상태(중간 크래시) 건너뛰기 및 recover 안내
                    if rec.state in (State.PUBLISHING, State.PUBLISHED, State.SOURCE_CLEANUP_PENDING):
                        log.warning(
                            "[%s] 발행/정리 대기 상태(%s)입니다. 'scaninbox recover'를 먼저 실행해 복구하십시오.",
                            rel,
                            rec.state.value,
                        )
                        continue

                    # 이미 완료된 파일 안내
                    if rec.state == State.COMPLETED:
                        log.info("[%s] 이미 처리 완료된 파일입니다 (해시: %s).", rel, sha256[:8])
                        continue

                    # 보류/실패 상태 안내
                    if rec.state in (State.HELD, State.FAILED):
                        log.info(
                            "[%s] %s 상태로 남아있습니다 (사유: %s). 'scaninbox retry'로 재시도할 수 있습니다.",
                            rel,
                            rec.state.value,
                            rec.reason or "없음",
                        )
                        continue

                    # 8. stable_seconds & 작성 안정성 검사 (PDF 파싱 검증 및 안정화 대기)
                    is_valid_pdf = False
                    try:
                        reader = PdfReader(str(path))
                        if len(reader.pages) >= 0:
                            is_valid_pdf = True
                    except Exception as err:
                        is_valid_pdf = False
                        log.debug("PDF 파싱 불완전: %s (%s)", rel, err)

                    if not is_valid_pdf:
                        log.info("[%s] PDF 파싱 불완전(작성/복사 중 의심), 안정화 대기", rel)
                        if rec.state == State.DISCOVERED:
                            self.journal.transition(
                                rec.id, State.WAITING_STABLE, reason="PDF 파싱 대기(작성 중)"
                            )
                        continue

                    # 이미 WAITING_STABLE 인 경우 안정화 시간 및 변동 확인
                    if rec.state == State.WAITING_STABLE:
                        now_s = time.time()
                        if (st.st_size != rec.size) or (st.st_mtime_ns != rec.mtime_ns):
                            log.info("[%s] 파일 크기/수정시각 변동 중, 안정화 대기", rel)
                            continue
                        time_since_rec = now_s - rec.updated_at
                        if time_since_rec < self.config.stable_seconds:
                            log.info(
                                "[%s] 파일 안정화 대기 중 (%0.1fs < %ds)",
                                rel,
                                time_since_rec,
                                self.config.stable_seconds,
                            )
                            continue

                    # 이미 등록된 DISCOVERED 레코드인데 크기나 mtime이 변경된 경우 안정화 대기
                    if rec.state == State.DISCOVERED and (
                        (rec.size is not None and st.st_size != rec.size)
                        or (rec.mtime_ns is not None and st.st_mtime_ns != rec.mtime_ns)
                    ):
                        log.info("[%s] 파일 크기/수정시각 변경 감지, 안정화 대기", rel)
                        self.journal.transition(
                            rec.id, State.WAITING_STABLE, reason="파일 크기/수정시각 변동 대기"
                        )
                        continue

                    # 이전 스캔 중 EXTRACTED 전이 후 REVIEW_PENDING 전에 중단된 경우,
                    # 허용 전이(EXTRACTED -> DISCOVERED)로 리셋하여 안전하게 보호 및 재추출을 수행한다.
                    if rec.state == State.EXTRACTED:
                        rec = self.journal.transition(
                            rec.id, State.DISCOVERED, reason="미완료 EXTRACTED 상태 재설정"
                        )

                    # 이미 추출/제안이 완료되어 승인 대기 중인 레코드 재사용
                    if rec.state in (State.REVIEW_PENDING, State.APPROVED):
                        if rec.plan_json:
                            try:
                                p_data = json.loads(rec.plan_json)
                                item = PlanItem(
                                    input_rel=p_data["input_rel"],
                                    original_sha256=p_data["original_sha256"],
                                    proposed_name=p_data["proposed_name"],
                                    ocr_status=OcrStatus(p_data["ocr_status"]),
                                    title=TitleProposal(
                                        title=p_data.get("title"),
                                        evidence_page=p_data.get("evidence_page"),
                                        evidence_snippet="",  # 본문 발췌는 저장/출력하지 않음
                                        doc_date=p_data.get("doc_date"),
                                        doc_type=p_data.get("doc_type"),
                                        review_reasons=p_data.get("warnings", []),
                                    ),
                                    needs_review=p_data["needs_review"],
                                    warnings=p_data.get("warnings", []),
                                )
                                plan_items.append(item)
                                continue
                            except Exception:
                                pass

                    # 2. 보호 (BACKED_UP 에 backup_rel 기록)
                    backup_rel = rel
                    bk_target = self.backup_dir / backup_rel
                    if bk_target.exists() and sha256_file(bk_target) != sha256:
                        stem = Path(rel).stem
                        parent = Path(rel).parent
                        base_gen = f"{stem}__{sha256[:8]}"
                        cand_name = f"{base_gen}.pdf"
                        cand_rel = (parent / cand_name).as_posix() if parent != Path(".") else cand_name
                        cand_target = self.backup_dir / cand_rel

                        counter = 1
                        while cand_target.exists() and sha256_file(cand_target) != sha256:
                            cand_name = f"{base_gen}_{counter}.pdf"
                            cand_rel = (parent / cand_name).as_posix() if parent != Path(".") else cand_name
                            cand_target = self.backup_dir / cand_rel
                            counter += 1

                        backup_rel = cand_rel
                        bk_target = cand_target

                    bk_target.parent.mkdir(parents=True, exist_ok=True)
                    if not bk_target.exists():
                        tmp_backup = self.work_dir / f"bk_{sha256[:12]}_{os.getpid()}_{time.time_ns()}.tmp"
                        shutil.copy2(path, tmp_backup)
                        try:
                            move_no_overwrite(tmp_backup, bk_target)
                        except FileExistsError:
                            if tmp_backup.exists():
                                tmp_backup.unlink()
                            if sha256_file(bk_target) != sha256:
                                self.journal.transition(
                                    rec.id, State.FAILED, reason="백업 파일 대상 충돌 및 해시 불일치"
                                )
                                continue

                    if sha256_file(bk_target) != sha256:
                        self.journal.transition(
                            rec.id, State.FAILED, reason="백업 해시 불일치 (복사 중 파일 변경 의심)"
                        )
                        continue

                    if rec.state in (State.DISCOVERED, State.WAITING_STABLE):
                        rec = self.journal.transition(
                            rec.id, State.BACKED_UP, backup_rel=backup_rel
                        )

                    # 3. 추출 (OCR)
                    # OCR 엔진 미설정 시 가짜 성공 금지 (fail-closed)
                    if self.ocr_engine is None:
                        self.journal.transition(
                            rec.id,
                            State.FAILED,
                            reason="OCR 엔진 미설정: OCR 필수 정책에 따라 엔진 없이 완료할 수 없음",
                        )
                        continue

                    tmp_ocr = self.work_dir / f"ocr_{sha256[:12]}"
                    tmp_ocr.mkdir(parents=True, exist_ok=True)
                    ocr_res = self.ocr_engine.run(bk_target, tmp_ocr, langs=self.langs)

                    if ocr_res.status == OcrStatus.ENCRYPTED:
                        self.journal.transition(
                            rec.id, State.HELD, reason=ocr_res.reason or "암호화된 PDF"
                        )
                        continue
                    elif ocr_res.status == OcrStatus.CORRUPT:
                        self.journal.transition(
                            rec.id, State.FAILED, reason=ocr_res.reason or "손상된 PDF"
                        )
                        continue
                    elif ocr_res.status == OcrStatus.FAILED:
                        self.journal.transition(
                            rec.id, State.FAILED, reason=ocr_res.reason or "OCR 실행 실패"
                        )
                        continue

                    rec = self.journal.transition(rec.id, State.EXTRACTED)

                    # 4. 제목 추출 및 제안 (EXTRACTED -> REVIEW_PENDING)
                    pdf_for_title = ocr_res.output if ocr_res.output else bk_target
                    title_prop = self.title_extractor.propose(
                        pdf_for_title, ocr_res.text_by_page
                    )
                    title_text = title_prop.title or "제목미상"
                    p_name = proposed_name(
                        title_text, sha256, doc_date=title_prop.doc_date, dir_path=str(self.done_dir)
                    )

                    needs_review = (
                        ocr_res.status == OcrStatus.PARTIAL
                        or title_prop.title is None
                        or title_prop.doc_date is None
                        or bool(title_prop.review_reasons)
                    )

                    # 계약 준수: evidence_snippet(OCR 본문)은 plan_json 에 절대 저장하지 않음 (메타데이터만)
                    plan_dict = {
                        "input_rel": rel,
                        "original_sha256": sha256,
                        "proposed_name": p_name,
                        "ocr_status": ocr_res.status.value,
                        "needs_review": needs_review,
                        "warnings": list(title_prop.review_reasons),
                        "title": title_prop.title,
                        "evidence_page": title_prop.evidence_page,
                        "doc_date": title_prop.doc_date,
                        "doc_type": title_prop.doc_type,
                        "ocr_output": str(ocr_res.output) if ocr_res.output else None,
                    }

                    rec = self.journal.transition(
                        rec.id,
                        State.REVIEW_PENDING,
                        plan_json=json.dumps(plan_dict, ensure_ascii=False),
                    )

                    item = PlanItem(
                        input_rel=rel,
                        original_sha256=sha256,
                        proposed_name=p_name,
                        ocr_status=ocr_res.status,
                        title=TitleProposal(
                            title=title_prop.title,
                            evidence_page=title_prop.evidence_page,
                            evidence_snippet="",  # 본문 발췌 미포함
                            doc_date=title_prop.doc_date,
                            doc_type=title_prop.doc_type,
                            review_reasons=title_prop.review_reasons,
                        ),
                        needs_review=needs_review,
                        warnings=list(title_prop.review_reasons),
                    )
                    plan_items.append(item)
                except Exception as ex:
                    log.exception("파일 스캔 중 오류 발생: %s (%s)", path, ex)
                    continue

            return plan_items

    def plan(self) -> list[PlanItem]:
        """현재 제안 계획 조회 (읽기 전용, scan 호출 없음)."""
        pending_records = self.journal.list(State.REVIEW_PENDING) + self.journal.list(State.APPROVED)
        items: list[PlanItem] = []
        for rec in pending_records:
            if not rec.plan_json:
                continue
            try:
                p_data = json.loads(rec.plan_json)
                item = PlanItem(
                    input_rel=p_data["input_rel"],
                    original_sha256=p_data["original_sha256"],
                    proposed_name=p_data["proposed_name"],
                    ocr_status=OcrStatus(p_data["ocr_status"]),
                    title=TitleProposal(
                        title=p_data.get("title"),
                        evidence_page=p_data.get("evidence_page"),
                        evidence_snippet="",  # 본문 발췌 없음
                        doc_date=p_data.get("doc_date"),
                        doc_type=p_data.get("doc_type"),
                        review_reasons=p_data.get("warnings", []),
                    ),
                    needs_review=p_data["needs_review"],
                    warnings=p_data.get("warnings", []),
                )
                items.append(item)
            except Exception:
                pass
        return items

    def unprocessed_count(self) -> int:
        """디스크의 PDF 파일 중 아직 스캔/제안되지 않은 신규 파일 개수 계산 (읽기 전용)."""
        if not self.scan_dir.exists():
            return 0
        known_rels = {
            r.input_rel
            for r in self.journal.list()
            if r.state not in (State.DISCOVERED, State.WAITING_STABLE)
        }
        count = 0
        for path in self.scan_dir.rglob("*.pdf"):
            if not path.is_file() or path.is_symlink():
                continue
            rel = self._relative_key(path)
            if rel not in known_rels:
                count += 1
        return count

    def retry(self, target: str | int) -> FileRecord:
        """HELD 또는 FAILED 상태인 항목을 재시도하기 위해 DISCOVERED 로 전이."""
        with self.journal.run_lock(lease_s=float(self.config.lease_seconds)):
            rec: Optional[FileRecord] = None
            if isinstance(target, int) or (isinstance(target, str) and target.isdigit()):
                rec = self.journal.get(int(target))
            else:
                target_str = str(target)
                norm_key = nfc(target_str)
                matches = [
                    r for r in self.journal.list()
                    if r.input_rel == target_str or nfc(r.input_rel) == norm_key
                ]
                if matches:
                    rec = matches[-1]

            if rec is None:
                raise KeyError(f"재시도할 대상 항목을 찾을 수 없습니다: {target}")

            if rec.state not in (State.HELD, State.FAILED):
                raise ValueError(
                    f"재시도 가능한 상태(HELD 또는 FAILED)가 아닙니다: {rec.input_rel} ({rec.state.value})"
                )

            return self.journal.transition(
                rec.id, State.DISCOVERED, reason="사용자 재시도 요청"
            )

    def _find_latest_record(self, input_rel: str) -> FileRecord:
        """input_rel 에 해당하는 가장 최근 저널 레코드 검색 (원래 철자 및 정규화 키 동시 비교)."""
        norm_key = nfc(input_rel)
        matches = [
            r for r in self.journal.list()
            if r.input_rel == input_rel or nfc(r.input_rel) == norm_key
        ]
        if not matches:
            raise KeyError(f"저널에 존재하지 않는 파일입니다: {input_rel}")
        return matches[-1]

    def rename(self, input_rel: str, new_name: str) -> FileRecord:
        """검토 항목 이름 지정 및 승인 (규약 2: approved_by_user=True)."""
        with self.journal.run_lock(lease_s=float(self.config.lease_seconds)):
            rec = self._find_latest_record(input_rel)
            if not new_name.lower().endswith(".pdf"):
                new_name = f"{new_name}.pdf"
            if new_name.startswith("."):
                raise ValueError(f"숨김 파일명은 지정할 수 없습니다: {new_name}")
            if not is_safe_component(new_name):
                raise ValueError(f"안전하지 않은 파일명입니다: {new_name}")

            plan_dict: dict[str, Any] = {}
            if rec.plan_json:
                try:
                    plan_dict = json.loads(rec.plan_json)
                except Exception:
                    pass
            plan_dict["proposed_name"] = new_name
            plan_dict["needs_review"] = False

            if rec.state == State.APPROVED:
                return self.journal.update_fields(
                    rec.id,
                    plan_json=json.dumps(plan_dict, ensure_ascii=False),
                    note="승인 항목 파일명 수정",
                )
            else:
                return self.journal.transition(
                    rec.id,
                    State.APPROVED,
                    approved_by_user=True,
                    plan_json=json.dumps(plan_dict, ensure_ascii=False),
                )

    def hold(self, input_rel: str, reason: str = "사용자 보류") -> FileRecord:
        """항목 보류 처리."""
        with self.journal.run_lock(lease_s=float(self.config.lease_seconds)):
            rec = self._find_latest_record(input_rel)
            return self.journal.transition(rec.id, State.HELD, reason=reason)

    def approve(self, input_rel: str) -> FileRecord:
        """개별 항목 명시 승인 (규약 2)."""
        with self.journal.run_lock(lease_s=float(self.config.lease_seconds)):
            rec = self._find_latest_record(input_rel)
            return self.journal.transition(rec.id, State.APPROVED, approved_by_user=True)

    def apply(
        self,
        targets: Optional[Sequence[str]] = None,
        approve_all: bool = False,
        force_review: bool = False,
    ) -> ApplyResult:
        """승인 항목 최종 확정 및 배치 (항목별 격리, 백업 무결성 엄격 보장, 낡은 레코드 보호)."""
        with self.journal.run_lock(lease_s=float(self.config.lease_seconds)):
            candidates: list[FileRecord] = []
            if targets:
                # 입력 순서 보존 (중복 제거)
                target_keys = list(dict.fromkeys(targets))
                for t in target_keys:
                    rec = self._find_latest_record(t)
                    candidates.append(rec)
            else:
                approved = self.journal.list(State.APPROVED)
                candidates.extend(approved)
                if approve_all:
                    pending = self.journal.list(State.REVIEW_PENDING)
                    candidates.extend(pending)

            applied: list[str] = []
            failed: list[tuple[str, str]] = []

            for rec in candidates:
                try:
                    src_input = self.scan_dir / rec.input_rel
                    if not src_input.exists():
                        reason = f"입력 파일 소실: {rec.input_rel}"
                        failed.append((rec.input_rel, reason))
                        continue

                    # 1. 낡은 레코드 검사 및 승인 시점 해시 재검증
                    current_hash = sha256_file(src_input)
                    if current_hash != rec.original_sha256:
                        # 같은 경로에 새로운 파일 도착: 낡은 REVIEW_PENDING/APPROVED 는 DISCOVERED 로 되돌려 재스캔 유도
                        reason = f"입력 파일 내용 변경 감지 (새 문서 도착): DISCOVERED로 복귀 ({rec.input_rel})"
                        if rec.state in (State.REVIEW_PENDING, State.APPROVED):
                            self.journal.transition(rec.id, State.DISCOVERED, reason=reason)
                        failed.append((rec.input_rel, reason))
                        continue

                    # 2. 백업 파일 무결성 완결 검증 (누락/변조 시 절대 입력을 지우지 않고 FAILED)
                    if not rec.backup_rel:
                        reason = f"백업 경로 정보 누락: backup_rel이 없어 입력을 보호할 수 없음 ({rec.input_rel})"
                        self.journal.transition(rec.id, State.FAILED, reason=reason)
                        failed.append((rec.input_rel, reason))
                        continue

                    bk_path = self.backup_dir / rec.backup_rel
                    if not bk_path.exists():
                        reason = f"백업 파일 소실: {rec.backup_rel}"
                        self.journal.transition(rec.id, State.FAILED, reason=reason)
                        failed.append((rec.input_rel, reason))
                        continue

                    if sha256_file(bk_path) != rec.original_sha256:
                        reason = f"백업 파일 변조 감지: 등록된 원본 해시와 불일치 ({rec.backup_rel})"
                        self.journal.transition(rec.id, State.FAILED, reason=reason)
                        failed.append((rec.input_rel, reason))
                        continue

                    plan_data: dict[str, Any] = {}
                    if rec.plan_json:
                        try:
                            plan_data = json.loads(rec.plan_json)
                        except Exception:
                            pass

                    # 3. needs_review 검증 (force_review 가 없고 명시적 APPROVED 가 아닌 경우 거절)
                    if plan_data.get("needs_review") and not force_review and rec.state != State.APPROVED:
                        reason = f"검토 필요 항목(needs_review)입니다. approve/rename 또는 --force-review 가 필요합니다: {rec.input_rel}"
                        failed.append((rec.input_rel, reason))
                        continue

                    # 4. 승인 상태 확인 (REVIEW_PENDING 에서 온 경우 승인 처리)
                    if rec.state != State.APPROVED:
                        rec = self.journal.transition(
                            rec.id, State.APPROVED, approved_by_user=True
                        )

                    # 5. 소스 PDF 결정 및 산출물 유실 시 DISCOVERED 복귀
                    ocr_st = plan_data.get("ocr_status")
                    source_pdf: Path
                    if ocr_st == OcrStatus.ALREADY_TEXT.value:
                        source_pdf = (
                            self.backup_dir / rec.backup_rel
                            if rec.backup_rel
                            else src_input
                        )
                    elif plan_data.get("ocr_output") and Path(plan_data["ocr_output"]).exists():
                        source_pdf = Path(plan_data["ocr_output"])
                    else:
                        # 산출물 유실 시 FAILED 로 영구 고착시키지 않고 재추출 가능하게 DISCOVERED 로 복귀
                        reason = f"OCR 산출물 유실 감지: 재추출을 위해 DISCOVERED 로 복귀 ({rec.input_rel})"
                        self.journal.transition(rec.id, State.DISCOVERED, reason=reason)
                        failed.append((rec.input_rel, reason))
                        continue

                    # 6. 배타적 임시 파일 생성 및 검증 (기존 파일 덮어쓰기 완전 방지)
                    target_name = plan_data.get("proposed_name") or f"{rec.original_sha256[:8]}.pdf"
                    final_rel = unique_name(self.done_dir, target_name)
                    pub_token = f".scaninbox_pub_{os.getpid()}_{time.time_ns()}_{uuid.uuid4().hex[:8]}.tmp"
                    tmp_target = self.done_dir / pub_token

                    with open(tmp_target, "xb") as f_out, open(source_pdf, "rb") as f_in:
                        shutil.copyfileobj(f_in, f_out)
                    shutil.copystat(source_pdf, tmp_target)
                    final_sha256 = sha256_file(tmp_target)

                    # 7. 규약 4: PUBLISHING 전에 final_rel/final_sha256 기록 후 pathsafe.move_no_overwrite 로 배치
                    rec = self.journal.transition(
                        rec.id, State.PUBLISHING, final_rel=final_rel, final_sha256=final_sha256
                    )
                    move_no_overwrite(tmp_target, self.done_dir / final_rel)
                    rec = self.journal.transition(rec.id, State.PUBLISHED)

                    # 8. 입력 정리 대기 및 입력 파일 제거
                    rec = self.journal.transition(rec.id, State.SOURCE_CLEANUP_PENDING)
                    src_input.unlink()

                    # 9. 완료 영속화 (규약 2: approved_by_user=True)
                    rec = self.journal.transition(
                        rec.id, State.COMPLETED, approved_by_user=True
                    )

                    # 10. OCR 임시 디렉터리 정리
                    tmp_ocr = self.work_dir / f"ocr_{rec.original_sha256[:12]}"
                    if tmp_ocr.exists():
                        shutil.rmtree(tmp_ocr, ignore_errors=True)

                    applied.append(rec.input_rel)
                except Exception as ex:
                    log.exception("항목 확정 처리 중 오류: %s (%s)", rec.input_rel, ex)
                    failed.append((rec.input_rel, str(ex)))

            return ApplyResult(applied=applied, failed=failed)

    def undo(self, input_rel: Optional[str] = None) -> list[str]:
        """완료된 항목 되돌리기 (규약 1: COMPLETED → DISCOVERED, 멱등성 및 격리 보장)."""
        with self.journal.run_lock(lease_s=float(self.config.lease_seconds)):
            candidates: list[FileRecord] = []
            if input_rel:
                norm_key = nfc(input_rel)
                completed_matches = [
                    r for r in self.journal.list(State.COMPLETED)
                    if r.input_rel == input_rel or nfc(r.input_rel) == norm_key
                ]
                if not completed_matches:
                    raise KeyError(f"되돌릴 완료 레코드가 없습니다: {input_rel}")
                candidates.append(completed_matches[-1])
            else:
                candidates = self.journal.list(State.COMPLETED)

            undone: list[str] = []
            for rec in candidates:
                try:
                    dest_input = self.scan_dir / rec.input_rel

                    # 백업본 확인
                    if not rec.backup_rel:
                        raise FileNotFoundError(f"백업 경로 정보 없음: {rec.input_rel}")
                    bk_file = self.backup_dir / rec.backup_rel
                    if not bk_file.exists() or sha256_file(bk_file) != rec.original_sha256:
                        raise ValueError(f"백업본 해시 불일치 또는 누락: {rec.input_rel}")

                    # 원래 입력 위치 파일 확인 (멱등 복구)
                    if dest_input.exists():
                        if sha256_file(dest_input) != rec.original_sha256:
                            raise FileExistsError(
                                f"되돌리기 충돌: 원래 입력 경로에 다른 내용의 파일이 존재합니다: {dest_input}"
                            )
                        # 이미 입력이 백업과 동일한 경우 복사는 건너뛰고 계속 진행(멱등성)
                    else:
                        dest_input.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(bk_file, dest_input)

                    # 최종본 확인 및 삭제
                    if rec.final_rel:
                        final_file = self.done_dir / rec.final_rel
                        if final_file.exists():
                            if sha256_file(final_file) != rec.final_sha256:
                                raise ValueError(
                                    f"되돌리기 충돌: 최종본이 변경되었으므로 삭제하지 않고 중단합니다: {final_file}"
                                )
                            final_file.unlink()

                    # 규약 1: COMPLETED → DISCOVERED
                    self.journal.transition(rec.id, State.DISCOVERED, reason="사용자 되돌리기(Undo)")
                    undone.append(rec.input_rel)
                except Exception as ex:
                    log.exception("되돌리기 중 오류 발생: %s (%s)", rec.input_rel, ex)
                    if input_rel:
                        raise

            return undone

    def recover(self) -> list[str]:
        """장애 복구 (규약 3: journal.recover 후 입력 정리 대기 건 처리, 잠금 보호)."""
        with self.journal.run_lock(lease_s=float(self.config.lease_seconds)):
            # 남아있는 Scan Inbox 발행 임시 파일 정리
            for tmp in self.done_dir.glob(".scaninbox_pub_*.tmp"):
                try:
                    tmp.unlink()
                except Exception:
                    pass

            # 1. journal.recover() 호출
            actions = self.journal.recover(
                input_dir=self.scan_dir,
                done_dir=self.done_dir,
                backup_dir=self.backup_dir,
            )

            recovered: list[str] = []
            for act in actions:
                recovered.append(f"{act.input_rel} ({act.action}: {act.detail})")
                # 규약 3: needs_source_cleanup 인 경우 입력 파일 해시 검증 후 삭제 및 COMPLETED
                if act.action == "needs_source_cleanup":
                    rec = self.journal.get(act.file_id)
                    src_input = self.scan_dir / rec.input_rel
                    if src_input.exists():
                        if sha256_file(src_input) == rec.original_sha256:
                            src_input.unlink()
                            self.journal.transition(
                                rec.id, State.COMPLETED, approved_by_user=True
                            )
                        else:
                            self.journal.transition(
                                rec.id,
                                State.HELD,
                                reason="복구 중단: 입력 내용 변경으로 삭제 중단 및 보류",
                            )

            return recovered

    def export_ledger(self, path: Optional[Path | str] = None) -> dict:
        """reconcile 호환 ledger JSON 내보내기."""
        return self.journal.export_ledger(path=path)
