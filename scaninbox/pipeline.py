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
from scaninbox.journal import FileRecord, Journal, RecoveryAction, sha256_file
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

log = logging.getLogger("scaninbox.pipeline")


_UNSET = object()


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
        """scan_dir 기준 NFC 정규화 상대경로."""
        rel = path.relative_to(self.scan_dir).as_posix()
        return nfc(rel)

    def scan(self) -> list[PlanItem]:
        """발견→등록→보호→추출→제안 단계 수행 (입력 무변경 보장)."""
        plan_items: list[PlanItem] = []
        if not self.scan_dir.exists():
            return plan_items

        for path in sorted(self.scan_dir.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            if path.suffix.lower() != ".pdf":
                continue

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

            # 이미 종결된 상태면 건너뜀
            if rec.state in (State.COMPLETED, State.HELD, State.FAILED):
                continue

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
                                evidence_snippet=p_data.get("evidence_snippet", ""),
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
            # 동일 파일명 이전 세대 원본 보존 및 충돌 방지:
            # 백업 경로가 이미 존재하고 내용(해시)이 다르면 고유한 후보 경로를 찾아 기존 백업을 절대 덮어쓰지 않는다.
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

            plan_dict = {
                "input_rel": rel,
                "original_sha256": sha256,
                "proposed_name": p_name,
                "ocr_status": ocr_res.status.value,
                "needs_review": needs_review,
                "warnings": list(title_prop.review_reasons),
                "title": title_prop.title,
                "evidence_page": title_prop.evidence_page,
                "evidence_snippet": title_prop.evidence_snippet[:80] if title_prop.evidence_snippet else "",
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
                title=title_prop,
                needs_review=needs_review,
                warnings=list(title_prop.review_reasons),
            )
            plan_items.append(item)

        return plan_items

    def plan(self) -> list[PlanItem]:
        """현재 제안 계획 조회 (입력 폴더 무변경)."""
        pending_records = self.journal.list(State.REVIEW_PENDING) + self.journal.list(State.APPROVED)
        if not pending_records:
            return self.scan()

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
                        evidence_snippet=p_data.get("evidence_snippet", ""),
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

    def _find_latest_record(self, input_rel: str) -> FileRecord:
        """input_rel 에 해당하는 가장 최근 저널 레코드 검색."""
        norm_key = nfc(input_rel)
        matches = [r for r in self.journal.list() if r.input_rel == norm_key]
        if not matches:
            raise KeyError(f"저널에 존재하지 않는 파일입니다: {input_rel}")
        return matches[-1]

    def rename(self, input_rel: str, new_name: str) -> FileRecord:
        """검토 항목 이름 지정 및 승인 (규약 2: approved_by_user=True)."""
        rec = self._find_latest_record(input_rel)
        if not new_name.lower().endswith(".pdf"):
            new_name = f"{new_name}.pdf"
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

        return self.journal.transition(
            rec.id,
            State.APPROVED,
            approved_by_user=True,
            plan_json=json.dumps(plan_dict, ensure_ascii=False),
        )

    def hold(self, input_rel: str, reason: str = "사용자 보류") -> FileRecord:
        """항목 보류 처리."""
        rec = self._find_latest_record(input_rel)
        return self.journal.transition(rec.id, State.HELD, reason=reason)

    def approve(self, input_rel: str) -> FileRecord:
        """개별 항목 명시 승인 (규약 2)."""
        rec = self._find_latest_record(input_rel)
        return self.journal.transition(rec.id, State.APPROVED, approved_by_user=True)

    def apply(
        self,
        targets: Optional[Sequence[str]] = None,
        approve_all: bool = False,
    ) -> list[str]:
        """승인 항목 최종 확정 및 배치."""
        candidates: list[FileRecord] = []
        if targets:
            target_keys = {nfc(t) for t in targets}
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
        for rec in candidates:
            src_input = self.scan_dir / rec.input_rel
            if not src_input.exists():
                raise FileNotFoundError(f"입력 파일이 소실되었습니다: {rec.input_rel}")

            # 1. 승인 시점과 현재 입력 파일의 SHA-256 해시 재검증
            current_hash = sha256_file(src_input)
            if current_hash != rec.original_sha256:
                reason = "승인 후 입력 파일 변경 감지: 원본 해시와 불일치하여 확정 거부"
                self.journal.transition(rec.id, State.FAILED, reason=reason)
                raise ValueError(f"{rec.input_rel}: {reason}")

            # 2. 승인 상태 확인 (REVIEW_PENDING 에서 온 경우 승인 처리)
            if rec.state != State.APPROVED:
                rec = self.journal.transition(
                    rec.id, State.APPROVED, approved_by_user=True
                )

            # 3. 소스 PDF 결정 (OCR 필수 정책 준수)
            plan_data: dict[str, Any] = {}
            if rec.plan_json:
                try:
                    plan_data = json.loads(rec.plan_json)
                except Exception:
                    pass

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
                reason = f"OCR 산출물 누락: {rec.input_rel}의 OCR 결과 파일이 존재하지 않아 완료할 수 없음"
                self.journal.transition(rec.id, State.FAILED, reason=reason)
                raise FileNotFoundError(reason)

            # 4. 임시 파일 생성 및 검증
            target_name = plan_data.get("proposed_name") or f"{rec.original_sha256[:8]}.pdf"
            final_rel = unique_name(self.done_dir, target_name)
            tmp_target = self.done_dir / f".tmp_{final_rel}"

            import shutil
            shutil.copy2(source_pdf, tmp_target)
            final_sha256 = sha256_file(tmp_target)

            # 5. 규약 4: PUBLISHING 전에 final_rel/final_sha256 기록 후 pathsafe.move_no_overwrite 로 배치
            rec = self.journal.transition(
                rec.id, State.PUBLISHING, final_rel=final_rel, final_sha256=final_sha256
            )
            move_no_overwrite(tmp_target, self.done_dir / final_rel)
            rec = self.journal.transition(rec.id, State.PUBLISHED)

            # 6. 입력 정리 대기 및 입력 파일 제거
            rec = self.journal.transition(rec.id, State.SOURCE_CLEANUP_PENDING)
            src_input.unlink()

            # 7. 완료 영속화 (규약 2: approved_by_user=True)
            rec = self.journal.transition(
                rec.id, State.COMPLETED, approved_by_user=True
            )
            applied.append(rec.input_rel)

        return applied

    def undo(self, input_rel: Optional[str] = None) -> list[str]:
        """완료된 항목 되돌리기 (규약 1: COMPLETED → DISCOVERED)."""
        candidates: list[FileRecord] = []
        if input_rel:
            rec = self._find_latest_record(input_rel)
            if rec.state != State.COMPLETED:
                raise KeyError(f"완료 상태가 아닙니다: {input_rel} ({rec.state})")
            candidates.append(rec)
        else:
            candidates = self.journal.list(State.COMPLETED)

        undone: list[str] = []
        for rec in candidates:
            dest_input = self.scan_dir / rec.input_rel
            # 원래 입력 위치에 이미 다른 파일이 존재하면 충돌 중단
            if dest_input.exists():
                raise FileExistsError(
                    f"되돌리기 충돌: 원래 입력 경로에 이미 파일이 존재합니다: {dest_input}"
                )

            # 백업본 확인
            if not rec.backup_rel:
                raise FileNotFoundError(f"백업 경로 정보 없음: {rec.input_rel}")
            bk_file = self.backup_dir / rec.backup_rel
            if not bk_file.exists() or sha256_file(bk_file) != rec.original_sha256:
                raise ValueError(f"백업본 해시 불일치 또는 누락: {rec.input_rel}")

            # 최종본 확인
            if not rec.final_rel:
                raise ValueError(f"최종본 경로 정보 없음: {rec.input_rel}")
            final_file = self.done_dir / rec.final_rel
            if not final_file.exists():
                raise FileNotFoundError(f"최종본 파일 누락: {final_file}")
            if sha256_file(final_file) != rec.final_sha256:
                raise ValueError(
                    f"되돌리기 충돌: 최종본이 변경되었으므로 삭제하지 않고 중단합니다: {final_file}"
                )

            # 원본 복구 및 최종본 삭제
            import shutil
            dest_input.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(bk_file, dest_input)
            final_file.unlink()

            # 규약 1: COMPLETED → DISCOVERED
            self.journal.transition(rec.id, State.DISCOVERED)
            undone.append(rec.input_rel)

        return undone

    def recover(self) -> list[str]:
        """장애 복구 (규약 3: journal.recover 후 입력 정리 대기 건 처리)."""
        # 남아있는 임시 파일 정리
        for tmp in self.done_dir.glob(".tmp_*"):
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
