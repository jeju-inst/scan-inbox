"""Scan Inbox 파이프라인: 발견→등록→보호→추출→제안→승인→확정→입력정리→완료.

이 모듈은 의존성(OcrEngine, TitleExtractor, Journal, PathSafe)을 주입 가능한 형태로 설계되었습니다.
core(worker-1)와 engines(worker-2) 모듈이 머지되기 전에도 독립적으로 동작할 수 있도록
Protocol 및 지속성(JsonFileJournal) / 인메모리(InMemoryJournal) 구현체를 포함합니다.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
import unicodedata
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, Sequence, Tuple

from pypdf import PdfReader

from scaninbox.contracts import (
    OcrEngine,
    OcrResult,
    OcrStatus,
    PlanItem,
    State,
    TitleExtractor,
    TitleProposal,
)


@dataclass
class JournalEntry:
    """단일 파일의 파이프라인 처리 상태 기록."""
    input_rel: str
    state: State
    original_sha256: str
    original_size: int
    original_mtime_ns: int
    actual_input_rel: Optional[str] = None      # 디스크 상의 원본 철자 (NFD 등 보존)
    backup_path: Optional[str] = None
    backup_sha256: Optional[str] = None
    ocr_output_path: Optional[str] = None
    ocr_status: Optional[OcrStatus] = None
    proposed_name: Optional[str] = None
    final_name: Optional[str] = None
    intended_final_path: Optional[str] = None   # 발행 중 의도한 최종 경로
    expected_final_sha256: Optional[str] = None # 발행 중 산출물의 기대 SHA-256
    final_path: Optional[str] = None
    final_sha256: Optional[str] = None
    approved_by_user: bool = False
    needs_review: bool = False
    reason: str = ""
    warnings: list[str] = field(default_factory=list)
    evidence_page: Optional[int] = None
    evidence_snippet: str = ""
    doc_date: Optional[str] = None
    doc_title: Optional[str] = None
    doc_type: Optional[str] = None
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["state"] = self.state.value if isinstance(self.state, State) else self.state
        if self.ocr_status is not None:
            d["ocr_status"] = (
                self.ocr_status.value
                if isinstance(self.ocr_status, OcrStatus)
                else self.ocr_status
            )
        return d


class JournalProtocol(Protocol):
    """상태 저널 프로토콜 (worker-1의 journal.py 가 구현할 인터페이스).

    총괄이 worker-1(feat/core)에게 전달할 필수 메서드 목록:
    -------------------------------------------------------
    1. get_entry(input_rel: str) -> Optional[JournalEntry]
       - 특정 입력 상대경로에 대한 현재 저널 기록 조회
    2. list_entries(state: Optional[State] = None) -> list[JournalEntry]
       - 전체 또는 특정 상태의 저널 기록 목록 반환
    3. record_discovered(input_rel: str, sha256: str, size: int, mtime_ns: int, actual_rel: Optional[str] = None) -> JournalEntry
       - 파일 발견 및 기본 메타데이터 등록 (DISCOVERED)
    4. record_backed_up(input_rel: str, backup_path: Path, backup_sha256: str) -> JournalEntry
       - 원본 백업 복사 완료 및 해시 일치 확인 기록 (BACKED_UP)
    5. record_extracted(
           input_rel: str,
           ocr_result: OcrResult,
           title: TitleProposal,
           proposed_name: str,
           needs_review: bool,
           warnings: list[str],
       ) -> JournalEntry
       - OCR 및 제목 제안 추출 완료 기록 (REVIEW_PENDING)
    6. record_approved(input_rel: str, final_name: Optional[str] = None) -> JournalEntry
       - 사용자 검토/승인 및 확정 파일명 기록 (APPROVED)
    7. record_publishing(
           input_rel: str,
           temp_path: Path,
           intended_final_path: Optional[str] = None,
           expected_final_sha256: Optional[str] = None,
       ) -> JournalEntry
       - 대상 폴더 임시 파일 생성 및 검증 단계 기록 (PUBLISHING)
    8. record_published(input_rel: str, final_path: str, final_sha256: str) -> JournalEntry
       - 최종본 배치 완료 기록 (PUBLISHED)
    9. record_source_cleanup_pending(input_rel: str) -> JournalEntry
       - 입력 파일 정리 대기 단계 기록 (SOURCE_CLEANUP_PENDING)
    10. record_completed(input_rel: str, approved_by_user: bool = True) -> JournalEntry
        - 입력 정리 및 전체 처리 완료 기록 (COMPLETED)
    11. record_held(input_rel: str, reason: str) -> JournalEntry
        - 보류 처리 및 사유 기록 (HELD)
    12. record_failed(input_rel: str, reason: str) -> JournalEntry
        - 실패 처리 및 사유 기록 (FAILED)
    13. record_undo(input_rel: str) -> JournalEntry
        - 실행 취소 및 롤백 기록 (DISCOVERED 등으로 전이)
    14. archive_completed_entry(input_rel: str) -> Optional[JournalEntry]
        - 동일 이름의 새 파일 도착 시 기존 완료 엔트리를 히스토리로 아카이브
    15. export_ledger() -> dict
        - scaninbox_corpus/schema/ledger.schema.json 호환 ledger 딕셔너리 반환
    """

    def get_entry(self, input_rel: str) -> Optional[JournalEntry]: ...
    def list_entries(self, state: Optional[State] = None) -> list[JournalEntry]: ...
    def record_discovered(
        self,
        input_rel: str,
        sha256: str,
        size: int,
        mtime_ns: int,
        actual_rel: Optional[str] = None,
    ) -> JournalEntry: ...
    def record_backed_up(
        self, input_rel: str, backup_path: Path, backup_sha256: str
    ) -> JournalEntry: ...
    def record_extracted(
        self,
        input_rel: str,
        ocr_result: OcrResult,
        title: TitleProposal,
        proposed_name: str,
        needs_review: bool,
        warnings: list[str],
    ) -> JournalEntry: ...
    def record_approved(
        self, input_rel: str, final_name: Optional[str] = None
    ) -> JournalEntry: ...
    def record_publishing(
        self,
        input_rel: str,
        temp_path: Path,
        intended_final_path: Optional[str] = None,
        expected_final_sha256: Optional[str] = None,
    ) -> JournalEntry: ...
    def record_published(
        self, input_rel: str, final_path: str, final_sha256: str
    ) -> JournalEntry: ...
    def record_source_cleanup_pending(self, input_rel: str) -> JournalEntry: ...
    def record_completed(
        self, input_rel: str, approved_by_user: bool = True
    ) -> JournalEntry: ...
    def record_held(self, input_rel: str, reason: str) -> JournalEntry: ...
    def record_failed(self, input_rel: str, reason: str) -> JournalEntry: ...
    def record_undo(self, input_rel: str) -> JournalEntry: ...
    def archive_completed_entry(self, input_rel: str) -> Optional[JournalEntry]: ...
    def export_ledger(self) -> dict: ...


# 허용된 상태 전이 표 (State transition matrix)
VALID_TRANSITIONS: dict[State, set[State]] = {
    State.DISCOVERED: {State.WAITING_STABLE, State.BACKED_UP, State.FAILED, State.HELD},
    State.WAITING_STABLE: {State.BACKED_UP, State.FAILED, State.HELD},
    State.BACKED_UP: {State.EXTRACTED, State.REVIEW_PENDING, State.FAILED, State.HELD},
    State.EXTRACTED: {State.REVIEW_PENDING, State.FAILED, State.HELD},
    State.REVIEW_PENDING: {State.APPROVED, State.HELD, State.FAILED},
    State.APPROVED: {State.PUBLISHING, State.REVIEW_PENDING, State.HELD, State.FAILED},
    State.PUBLISHING: {State.PUBLISHED, State.REVIEW_PENDING, State.APPROVED, State.HELD, State.FAILED},
    State.PUBLISHED: {State.SOURCE_CLEANUP_PENDING, State.COMPLETED, State.HELD, State.FAILED},
    State.SOURCE_CLEANUP_PENDING: {State.COMPLETED, State.HELD, State.FAILED},
    State.COMPLETED: {State.DISCOVERED},  # Undo 지원
    State.HELD: {State.REVIEW_PENDING, State.DISCOVERED},
    State.FAILED: {State.DISCOVERED, State.REVIEW_PENDING},
}


class InMemoryJournal:
    """메모리 기반 Journal 기본 스텁 구현체 (독립 테스트 및 fallback용)."""

    def __init__(self) -> None:
        self.entries: dict[str, JournalEntry] = {}

    def _normalize_key(self, path: str) -> str:
        return unicodedata.normalize("NFC", path.replace("\\", "/"))

    def _transition(self, entry: JournalEntry, new_state: State) -> None:
        allowed = VALID_TRANSITIONS.get(entry.state, set())
        if new_state not in allowed:
            raise ValueError(
                f"허용되지 않은 상태 전이: {entry.state.value} -> {new_state.value} (입력: {entry.input_rel})"
            )
        entry.state = new_state
        entry.updated_at = time.time()

    def get_entry(self, input_rel: str) -> Optional[JournalEntry]:
        return self.entries.get(self._normalize_key(input_rel))

    def list_entries(self, state: Optional[State] = None) -> list[JournalEntry]:
        items = list(self.entries.values())
        if state is not None:
            items = [e for e in items if e.state == state]
        return sorted(items, key=lambda x: x.input_rel)

    def archive_completed_entry(self, input_rel: str) -> Optional[JournalEntry]:
        """동일 이름의 새 문서 도착 시 기존 완료 엔트리를 보존 아카이브."""
        key = self._normalize_key(input_rel)
        if key in self.entries and self.entries[key].state == State.COMPLETED:
            old = self.entries.pop(key)
            archive_key = f"{key}@{old.original_sha256[:8]}"
            self.entries[archive_key] = old
            return old
        return None

    def record_discovered(
        self,
        input_rel: str,
        sha256: str,
        size: int,
        mtime_ns: int,
        actual_rel: Optional[str] = None,
    ) -> JournalEntry:
        key = self._normalize_key(input_rel)
        if key in self.entries:
            entry = self.entries[key]
            entry.original_sha256 = sha256
            entry.original_size = size
            entry.original_mtime_ns = mtime_ns
            if actual_rel:
                entry.actual_input_rel = actual_rel
            return entry
        entry = JournalEntry(
            input_rel=key,
            state=State.DISCOVERED,
            original_sha256=sha256,
            original_size=size,
            original_mtime_ns=mtime_ns,
            actual_input_rel=actual_rel or input_rel,
        )
        self.entries[key] = entry
        return entry

    def record_backed_up(
        self, input_rel: str, backup_path: Path, backup_sha256: str
    ) -> JournalEntry:
        entry = self.entries[self._normalize_key(input_rel)]
        self._transition(entry, State.BACKED_UP)
        entry.backup_path = str(backup_path)
        entry.backup_sha256 = backup_sha256
        return entry

    def record_extracted(
        self,
        input_rel: str,
        ocr_result: OcrResult,
        title: TitleProposal,
        proposed_name: str,
        needs_review: bool,
        warnings: list[str],
    ) -> JournalEntry:
        entry = self.entries[self._normalize_key(input_rel)]
        self._transition(entry, State.REVIEW_PENDING)
        entry.ocr_status = ocr_result.status
        entry.ocr_output_path = str(ocr_result.output) if ocr_result.output else None
        entry.proposed_name = proposed_name
        entry.needs_review = needs_review
        entry.warnings = list(warnings)
        entry.evidence_page = title.evidence_page
        entry.evidence_snippet = title.evidence_snippet[:80] if title.evidence_snippet else ""
        entry.doc_date = title.doc_date
        entry.doc_title = title.title
        entry.doc_type = title.doc_type
        return entry

    def record_approved(
        self, input_rel: str, final_name: Optional[str] = None
    ) -> JournalEntry:
        entry = self.entries[self._normalize_key(input_rel)]
        self._transition(entry, State.APPROVED)
        entry.approved_by_user = True
        if final_name:
            entry.final_name = final_name
        elif not entry.final_name:
            entry.final_name = entry.proposed_name
        return entry

    def record_publishing(
        self,
        input_rel: str,
        temp_path: Path,
        intended_final_path: Optional[str] = None,
        expected_final_sha256: Optional[str] = None,
    ) -> JournalEntry:
        entry = self.entries[self._normalize_key(input_rel)]
        self._transition(entry, State.PUBLISHING)
        if intended_final_path:
            entry.intended_final_path = intended_final_path
        if expected_final_sha256:
            entry.expected_final_sha256 = expected_final_sha256
        return entry

    def record_published(
        self, input_rel: str, final_path: str, final_sha256: str
    ) -> JournalEntry:
        entry = self.entries[self._normalize_key(input_rel)]
        self._transition(entry, State.PUBLISHED)
        entry.final_path = final_path
        entry.final_sha256 = final_sha256
        return entry

    def record_source_cleanup_pending(self, input_rel: str) -> JournalEntry:
        entry = self.entries[self._normalize_key(input_rel)]
        self._transition(entry, State.SOURCE_CLEANUP_PENDING)
        return entry

    def record_completed(
        self, input_rel: str, approved_by_user: bool = True
    ) -> JournalEntry:
        entry = self.entries[self._normalize_key(input_rel)]
        self._transition(entry, State.COMPLETED)
        entry.approved_by_user = approved_by_user
        return entry

    def record_held(self, input_rel: str, reason: str) -> JournalEntry:
        entry = self.entries[self._normalize_key(input_rel)]
        self._transition(entry, State.HELD)
        entry.reason = reason
        return entry

    def record_failed(self, input_rel: str, reason: str) -> JournalEntry:
        entry = self.entries[self._normalize_key(input_rel)]
        self._transition(entry, State.FAILED)
        entry.reason = reason
        return entry

    def record_undo(self, input_rel: str) -> JournalEntry:
        entry = self.entries[self._normalize_key(input_rel)]
        self._transition(entry, State.DISCOVERED)
        entry.final_path = None
        entry.final_sha256 = None
        entry.intended_final_path = None
        entry.expected_final_sha256 = None
        entry.approved_by_user = False
        return entry

    def export_ledger(self) -> dict:
        """ledger.schema.json 호환 기록 출력."""
        entries_out = []
        for entry in self.list_entries():
            if entry.state == State.COMPLETED:
                entries_out.append(
                    {
                        "input": entry.actual_input_rel or entry.input_rel,
                        "status": "completed",
                        "original_sha256": entry.original_sha256,
                        "final_path": entry.final_path or "",
                        "final_sha256": entry.final_sha256 or "",
                        "approved_by_user": entry.approved_by_user,
                    }
                )
            elif entry.state == State.HELD:
                entries_out.append(
                    {
                        "input": entry.actual_input_rel or entry.input_rel,
                        "status": "held",
                        "original_sha256": entry.original_sha256,
                        "reason": entry.reason or "검토 보류",
                    }
                )
            elif entry.state == State.FAILED:
                entries_out.append(
                    {
                        "input": entry.actual_input_rel or entry.input_rel,
                        "status": "failed",
                        "original_sha256": entry.original_sha256,
                        "reason": entry.reason or "처리 실패",
                    }
                )
        return {"entries": entries_out}


class JsonFileJournal(InMemoryJournal):
    """JSON 파일 기반 영속 저널 구현체 (CLI 프로세스 간 상태 영속화)."""

    def __init__(self, file_path: Path) -> None:
        super().__init__()
        self.file_path = Path(file_path).resolve()
        self._load()

    def _load(self) -> None:
        if not self.file_path.exists():
            return
        try:
            raw = self.file_path.read_text(encoding="utf-8")
            data = json.loads(raw)
            for item in data.get("entries", []):
                state_val = State(item["state"]) if isinstance(item["state"], str) else item["state"]
                ocr_val = OcrStatus(item["ocr_status"]) if item.get("ocr_status") else None
                entry = JournalEntry(
                    input_rel=item["input_rel"],
                    state=state_val,
                    original_sha256=item["original_sha256"],
                    original_size=item["original_size"],
                    original_mtime_ns=item["original_mtime_ns"],
                    actual_input_rel=item.get("actual_input_rel"),
                    backup_path=item.get("backup_path"),
                    backup_sha256=item.get("backup_sha256"),
                    ocr_output_path=item.get("ocr_output_path"),
                    ocr_status=ocr_val,
                    proposed_name=item.get("proposed_name"),
                    final_name=item.get("final_name"),
                    intended_final_path=item.get("intended_final_path"),
                    expected_final_sha256=item.get("expected_final_sha256"),
                    final_path=item.get("final_path"),
                    final_sha256=item.get("final_sha256"),
                    approved_by_user=item.get("approved_by_user", False),
                    needs_review=item.get("needs_review", False),
                    reason=item.get("reason", ""),
                    warnings=item.get("warnings", []),
                    evidence_page=item.get("evidence_page"),
                    evidence_snippet=item.get("evidence_snippet", ""),
                    doc_date=item.get("doc_date"),
                    doc_title=item.get("doc_title"),
                    doc_type=item.get("doc_type"),
                    updated_at=item.get("updated_at", time.time()),
                )
                self.entries[self._normalize_key(entry.input_rel)] = entry
        except Exception:
            pass

    def _save(self) -> None:
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        data = {"entries": [e.to_dict() for e in self.list_entries()]}
        tmp = self.file_path.parent / f".tmp_{self.file_path.name}"
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.file_path)

    def archive_completed_entry(self, *args: Any, **kwargs: Any) -> Optional[JournalEntry]:
        res = super().archive_completed_entry(*args, **kwargs)
        if res:
            self._save()
        return res

    def record_discovered(self, *args: Any, **kwargs: Any) -> JournalEntry:
        res = super().record_discovered(*args, **kwargs)
        self._save()
        return res

    def record_backed_up(self, *args: Any, **kwargs: Any) -> JournalEntry:
        res = super().record_backed_up(*args, **kwargs)
        self._save()
        return res

    def record_extracted(self, *args: Any, **kwargs: Any) -> JournalEntry:
        res = super().record_extracted(*args, **kwargs)
        self._save()
        return res

    def record_approved(self, *args: Any, **kwargs: Any) -> JournalEntry:
        res = super().record_approved(*args, **kwargs)
        self._save()
        return res

    def record_publishing(self, *args: Any, **kwargs: Any) -> JournalEntry:
        res = super().record_publishing(*args, **kwargs)
        self._save()
        return res

    def record_published(self, *args: Any, **kwargs: Any) -> JournalEntry:
        res = super().record_published(*args, **kwargs)
        self._save()
        return res

    def record_source_cleanup_pending(self, *args: Any, **kwargs: Any) -> JournalEntry:
        res = super().record_source_cleanup_pending(*args, **kwargs)
        self._save()
        return res

    def record_completed(self, *args: Any, **kwargs: Any) -> JournalEntry:
        res = super().record_completed(*args, **kwargs)
        self._save()
        return res

    def record_held(self, *args: Any, **kwargs: Any) -> JournalEntry:
        res = super().record_held(*args, **kwargs)
        self._save()
        return res

    def record_failed(self, *args: Any, **kwargs: Any) -> JournalEntry:
        res = super().record_failed(*args, **kwargs)
        self._save()
        return res

    def record_undo(self, *args: Any, **kwargs: Any) -> JournalEntry:
        res = super().record_undo(*args, **kwargs)
        self._save()
        return res


# PathSafe 내장 도우미 (worker-1 모듈 머지 전 기본 안전성 보장)
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
}


def sanitize_filename(name: str, max_length: int = 120) -> str:
    """Windows/macOS 호환 파일명 정규화."""
    # NFC 정규화
    s = unicodedata.normalize("NFC", name)
    # 줄바꿈 및 특수 공백 치환
    s = re.sub(r"[\r\n\t]+", " ", s)
    # 금지 문자 치환: \ / : * ? " < > |
    s = re.sub(r'[\\/:*?"<>|]', "_", s)
    # 연속 공백 정리
    s = re.sub(r"\s+", " ", s).strip()
    # 후행 마침표 및 공백 제거
    s = s.rstrip(". ")
    if not s:
        s = "제목미상"
    # Windows 예약어 회피
    stem = s.split(".")[0].upper()
    if stem in _WINDOWS_RESERVED:
        s = f"_{s}"
    # 길이 제한 (확장자 제외)
    if len(s) > max_length:
        s = s[:max_length].rstrip(". ")
    return s


def compute_sha256(path: Path) -> str:
    """파일의 SHA-256 해시 계산."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class Pipeline:
    """Scan Inbox 핵심 파이프라인.
    
    발견 → 등록 → 보호 → 추출 → 제안 → 승인 → 확정 → 입력정리 → 완료
    """

    def __init__(
        self,
        input_dir: Path,
        done_dir: Path,
        backup_dir: Path,
        work_dir: Optional[Path] = None,
        ocr_engine: Optional[OcrEngine] = None,
        title_extractor: Optional[TitleExtractor] = None,
        journal: Optional[JournalProtocol] = None,
        journal_path: Optional[Path] = None,
        langs: str = "kor+eng",
    ) -> None:
        self.input_dir = Path(input_dir).resolve()
        self.done_dir = Path(done_dir).resolve()
        self.backup_dir = Path(backup_dir).resolve()
        self.work_dir = (
            Path(work_dir).resolve() if work_dir else self.backup_dir / ".tmp"
        )
        self.ocr_engine = ocr_engine
        self.title_extractor = title_extractor
        self.langs = langs

        # 디렉터리 준비
        self.done_dir.mkdir(parents=True, exist_ok=True)
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self.work_dir.mkdir(parents=True, exist_ok=True)

        # 저널 인스턴스: 주입되지 않으면 JsonFileJournal 로 CLI 호출 간 영속성 유지
        if journal is not None:
            self.journal = journal
        else:
            j_path = journal_path or (self.backup_dir / "journal.json")
            self.journal = JsonFileJournal(j_path)

    def _relative_key(self, path: Path) -> str:
        """input_dir 기준 상대경로 문자열 (NFC 정규화 키)."""
        rel = path.relative_to(self.input_dir).as_posix()
        return unicodedata.normalize("NFC", rel)

    def _resolve_input_path(self, rel_key: str, actual_rel: Optional[str] = None) -> Optional[Path]:
        """정규화 키 또는 실제 상대경로를 바탕으로 파일시스템 상의 실제 Path 조회."""
        if actual_rel:
            p = self.input_dir / actual_rel
            if p.exists():
                return p
        direct = self.input_dir / rel_key
        if direct.exists():
            return direct
        # NFD/NFC 정규화 차이로 인한 불일치 대응
        norm_target = unicodedata.normalize("NFC", rel_key)
        for p in self.input_dir.rglob("*"):
            if p.is_file() and not p.is_symlink():
                p_norm = unicodedata.normalize("NFC", p.relative_to(self.input_dir).as_posix())
                if p_norm == norm_target:
                    return p
        return None

    def _is_file_stable(self, path: Path) -> bool:
        """파일 작성 안정성 확인 및 PDF 파싱 재열기 검사."""
        if not path.is_file() or path.is_symlink():
            return False
        try:
            st1 = path.stat()
            if st1.st_size == 0:
                return False
            # PDF 파일 구조 기본 파싱 시도 (헤더 및 재열기)
            with open(path, "rb") as f:
                header = f.read(5)
                if not header.startswith(b"%PDF-"):
                    return False
            reader = PdfReader(str(path))
            _ = len(reader.pages)
            return True
        except Exception:
            return False

    def scan(self) -> list[PlanItem]:
        """발견→등록→보호→추출(OCR)→제안(PlanItem) 단계 수행.

        중요: plan/scan 과정 중에는 입력 폴더 내의 원본 파일을 절대 변경하지 않습니다.
        """
        plan_items: list[PlanItem] = []
        if not self.input_dir.exists():
            return plan_items

        for path in sorted(self.input_dir.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            if path.suffix.lower() != ".pdf":
                continue

            rel_key = self._relative_key(path)
            actual_rel = path.relative_to(self.input_dir).as_posix()
            existing_entry = self.journal.get_entry(rel_key)
            st = path.stat()
            sha256 = compute_sha256(path)

            # 이미 완료된 파일명으로 새 문서가 도착한 경우 (Codex finding 4 해결)
            if existing_entry and existing_entry.state == State.COMPLETED:
                if existing_entry.original_sha256 == sha256:
                    # 동일 내용의 기완료 파일이면 스킵
                    continue
                else:
                    # 다른 내용의 새 문서 도착: 기존 완료 항목을 아카이브하고 새 등록 진행
                    self.journal.archive_completed_entry(rel_key)
                    existing_entry = None

            # 이미 보류/실패된 항목
            if existing_entry and existing_entry.state in (State.HELD, State.FAILED):
                continue

            # 재스캔 시 이미 등록되어 검토/승인 대기 중인 항목 처리 (Codex finding 3 해결: REVIEW_PENDING, APPROVED 만 재사용)
            if existing_entry and existing_entry.state in (
                State.REVIEW_PENDING,
                State.APPROVED,
            ):
                if (
                    st.st_size == existing_entry.original_size
                    and sha256 == existing_entry.original_sha256
                ):
                    item = PlanItem(
                        input_rel=rel_key,
                        original_sha256=existing_entry.original_sha256,
                        proposed_name=existing_entry.final_name
                        or existing_entry.proposed_name
                        or f"미정__{existing_entry.original_sha256[:8]}.pdf",
                        ocr_status=existing_entry.ocr_status or OcrStatus.OK,
                        title=TitleProposal(
                            title=existing_entry.doc_title,
                            evidence_page=existing_entry.evidence_page,
                            evidence_snippet=existing_entry.evidence_snippet,
                            doc_date=existing_entry.doc_date,
                            doc_type=existing_entry.doc_type,
                            review_reasons=existing_entry.warnings,
                        ),
                        needs_review=existing_entry.needs_review,
                        warnings=existing_entry.warnings,
                    )
                    plan_items.append(item)
                    continue

            # 1. 안정성 검사
            stable = self._is_file_stable(path)

            # 2. 등록 (Registration)
            entry = self.journal.record_discovered(
                rel_key,
                sha256=sha256,
                size=st.st_size,
                mtime_ns=st.st_mtime_ns,
                actual_rel=actual_rel,
            )

            # 3. 보호 (Backup)
            backup_target = self.backup_dir / actual_rel
            backup_target.parent.mkdir(parents=True, exist_ok=True)
            # 이미 백업이 존재하고 해시가 일치하면 재복사 생략 (BACKED_UP 상태에서 중단 후 재개 시)
            if not backup_target.exists() or compute_sha256(backup_target) != sha256:
                shutil.copy2(path, backup_target)
            backup_sha256 = compute_sha256(backup_target)
            if backup_sha256 != sha256:
                reason = "백업 해시 불일치 (복사 중 파일 변경 의심)"
                self.journal.record_failed(rel_key, reason=reason)
                continue
            if entry.state == State.DISCOVERED or entry.state == State.WAITING_STABLE:
                self.journal.record_backed_up(
                    rel_key, backup_path=backup_target, backup_sha256=backup_sha256
                )

            # 4. 추출 (OCR & 제목)
            # OCR 엔진 미설정 시 fail-closed 정책 (Codex finding 1 해결: OCR 필수이므로 엔진 없이 성공 위장 금지)
            if self.ocr_engine is None:
                ocr_res = OcrResult(
                    status=OcrStatus.FAILED,
                    reason="OCR 엔진 미설정: OCR 필수 정책에 따라 엔진 없이 완료할 수 없습니다.",
                    engine="none",
                )
            else:
                tmp_out = self.work_dir / f"ocr_{sha256[:12]}"
                tmp_out.mkdir(parents=True, exist_ok=True)
                ocr_res = self.ocr_engine.run(
                    backup_target, tmp_out, langs=self.langs
                )

            # OCR 필수 정책 처리
            # ok/already_text 만 완료 가능, partial 은 review, encrypted/corrupt/failed 는 held/failed
            if ocr_res.status in (OcrStatus.ENCRYPTED, OcrStatus.CORRUPT):
                reason = ocr_res.reason or (
                    "암호화된 PDF"
                    if ocr_res.status == OcrStatus.ENCRYPTED
                    else "손상된 PDF"
                )
                if ocr_res.status == OcrStatus.ENCRYPTED:
                    self.journal.record_held(rel_key, reason=reason)
                else:
                    self.journal.record_failed(rel_key, reason=reason)
                continue
            elif ocr_res.status == OcrStatus.FAILED:
                reason = ocr_res.reason or "OCR 엔진 실행 실패"
                self.journal.record_failed(rel_key, reason=reason)
                continue

            # 제목 추출
            if self.title_extractor is not None:
                pdf_for_title = ocr_res.output if ocr_res.output else backup_target
                title_prop = self.title_extractor.propose(
                    pdf_for_title, ocr_res.text_by_page
                )
            else:
                title_prop = TitleProposal(
                    title=path.stem,
                    evidence_page=1,
                    evidence_snippet="기본 파일명",
                    doc_date=None,
                )

            # 제안 파일명 조합: YYYY-MM-DD_제목__짧은ID.pdf (불명시 날짜미상, 제목미상)
            date_part = title_prop.doc_date if title_prop.doc_date else "날짜미상"
            raw_title = title_prop.title if title_prop.title else "제목미상"
            title_part = sanitize_filename(raw_title)
            short_id = sha256[:8]
            proposed_name = f"{date_part}_{title_part}__{short_id}.pdf"

            # 검토 필요성(needs_review) 판정
            needs_review = False
            warnings: list[str] = []
            if ocr_res.status == OcrStatus.PARTIAL:
                needs_review = True
                warnings.append("OCR partial: 일부 페이지 텍스트 누락")
            if not title_prop.title or title_prop.title == "제목미상":
                needs_review = True
                warnings.append("제목 불명: 사용자 확인 필요")
            if not title_prop.doc_date:
                needs_review = True
                warnings.append("문서 날짜 불명: 날짜미상 표기")
            if title_prop.review_reasons:
                needs_review = True
                warnings.extend(title_prop.review_reasons)

            self.journal.record_extracted(
                rel_key,
                ocr_result=ocr_res,
                title=title_prop,
                proposed_name=proposed_name,
                needs_review=needs_review,
                warnings=warnings,
            )

            item = PlanItem(
                input_rel=rel_key,
                original_sha256=sha256,
                proposed_name=proposed_name,
                ocr_status=ocr_res.status,
                title=title_prop,
                needs_review=needs_review,
                warnings=warnings,
            )
            plan_items.append(item)

        return plan_items

    def plan(self) -> list[PlanItem]:
        """현재 제안 계획 조회 (입력 폴더 무변경 보장)."""
        entries = self.journal.list_entries()
        if not entries:
            return self.scan()

        items: list[PlanItem] = []
        for e in entries:
            if e.state in (State.COMPLETED, State.HELD, State.FAILED):
                continue
            item = PlanItem(
                input_rel=e.input_rel,
                original_sha256=e.original_sha256,
                proposed_name=e.final_name or e.proposed_name or f"미정__{e.original_sha256[:8]}.pdf",
                ocr_status=e.ocr_status or OcrStatus.OK,
                title=TitleProposal(
                    title=e.doc_title,
                    evidence_page=e.evidence_page,
                    evidence_snippet=e.evidence_snippet,
                    doc_date=e.doc_date,
                    doc_type=e.doc_type,
                    review_reasons=e.warnings,
                ),
                needs_review=e.needs_review,
                warnings=e.warnings,
            )
            items.append(item)
        return items

    def rename(self, input_rel: str, new_name: str) -> JournalEntry:
        """검토 대상 항목의 파일명을 지정하고 승인 상태로 전환."""
        key = unicodedata.normalize("NFC", input_rel.replace("\\", "/"))
        entry = self.journal.get_entry(key)
        if not entry:
            raise KeyError(f"저널에 존재하지 않는 파일입니다: {input_rel}")
        if not new_name.lower().endswith(".pdf"):
            new_name = f"{new_name}.pdf"
        sanitized = sanitize_filename(new_name[:-4]) + ".pdf"
        entry.needs_review = False
        return self.journal.record_approved(key, final_name=sanitized)

    def hold(self, input_rel: str, reason: str = "사용자 보류") -> JournalEntry:
        """항목을 보류(HELD) 처리하고 입력에 남김."""
        key = unicodedata.normalize("NFC", input_rel.replace("\\", "/"))
        entry = self.journal.get_entry(key)
        if not entry:
            raise KeyError(f"저널에 존재하지 않는 파일입니다: {input_rel}")
        return self.journal.record_held(key, reason=reason)

    def approve(self, input_rel: str) -> JournalEntry:
        """개별 항목 명시 승인."""
        key = unicodedata.normalize("NFC", input_rel.replace("\\", "/"))
        entry = self.journal.get_entry(key)
        if not entry:
            raise KeyError(f"저널에 존재하지 않는 파일입니다: {input_rel}")
        return self.journal.record_approved(key)

    def _find_safe_target_path(self, target_filename: str) -> Path:
        """덮어쓰기 없는 고유 대상 경로 생성."""
        stem = target_filename[:-4] if target_filename.lower().endswith(".pdf") else target_filename
        dest = self.done_dir / f"{stem}.pdf"
        if not dest.exists():
            return dest
        counter = 1
        while True:
            candidate = self.done_dir / f"{stem}__{counter}.pdf"
            if not candidate.exists():
                return candidate
            counter += 1

    def apply(
        self,
        targets: Optional[Sequence[str]] = None,
        approve_all: bool = False,
    ) -> list[str]:
        """승인 항목 확정 배치 및 입력 정리.
        
        G1 정책:
        - 모든 건은 사용자가 승인해야 완료 (approved_by_user=True).
        - 승인 시점에 현재 입력 파일의 SHA-256 해시를 재검증하여 일치하지 않으면 거절.
        - 임시 파일 -> 검증 -> 덮어쓰기 없는 최종 배치 -> 기록 영속화 -> 입력 정리.
        """
        all_entries = self.journal.list_entries()
        candidates: list[JournalEntry] = []

        if targets:
            target_keys = {
                unicodedata.normalize("NFC", t.replace("\\", "/")) for t in targets
            }
            for e in all_entries:
                if e.input_rel in target_keys:
                    candidates.append(e)
        else:
            for e in all_entries:
                if e.state == State.APPROVED:
                    candidates.append(e)
                elif approve_all and e.state == State.REVIEW_PENDING:
                    candidates.append(e)

        applied: list[str] = []
        for entry in candidates:
            # 실제 입력 파일 경로 조회
            src_input = self._resolve_input_path(entry.input_rel, entry.actual_input_rel)
            if not src_input or not src_input.exists():
                raise FileNotFoundError(
                    f"입력 파일이 소실되었습니다: {entry.input_rel}"
                )

            # 1. 입력 해시 재검증
            current_hash = compute_sha256(src_input)
            if current_hash != entry.original_sha256:
                reason = "승인 후 입력 파일 변경 감지: 원본 해시와 불일치하여 확정 거부"
                self.journal.record_failed(entry.input_rel, reason=reason)
                raise ValueError(f"{entry.input_rel}: {reason}")

            # 승인 상태 마크
            if entry.state != State.APPROVED:
                self.journal.record_approved(entry.input_rel)

            # 2. 배치할 소스 파일 결정 (OCR 결과물 우선, 없으면 백업본)
            source_pdf: Path
            if entry.ocr_output_path and Path(entry.ocr_output_path).exists():
                source_pdf = Path(entry.ocr_output_path)
            elif entry.backup_path and Path(entry.backup_path).exists():
                source_pdf = Path(entry.backup_path)
            else:
                source_pdf = src_input

            # 3. 임시 파일 생성 (done_dir 내 .tmp 파일)
            target_name = entry.final_name or entry.proposed_name or f"scan__{entry.original_sha256[:8]}.pdf"
            safe_target = self._find_safe_target_path(target_name)
            final_rel = unicodedata.normalize(
                "NFC", safe_target.relative_to(self.done_dir).as_posix()
            )
            temp_target = self.done_dir / f".tmp_{safe_target.name}"
            shutil.copy2(source_pdf, temp_target)

            # 4. 임시 파일 무결성 및 PDF 파싱 검증
            final_sha256 = compute_sha256(temp_target)
            try:
                reader = PdfReader(str(temp_target))
                if len(reader.pages) == 0:
                    raise ValueError("산출물 페이지 수가 0입니다.")
            except Exception as ex:
                if temp_target.exists():
                    temp_target.unlink()
                self.journal.record_failed(
                    entry.input_rel, reason=f"임시 산출물 검증 실패: {ex}"
                )
                continue

            # expected_final_sha256 와 intended_final_path 를 기록하여 크래시 복구 시 신뢰 검증 보장 (Codex finding 2 해결)
            self.journal.record_publishing(
                entry.input_rel,
                temp_path=temp_target,
                intended_final_path=final_rel,
                expected_final_sha256=final_sha256,
            )

            # 5. 덮어쓰기 없는 원자적 최종 배치
            temp_target.rename(safe_target)
            self.journal.record_published(
                entry.input_rel, final_path=final_rel, final_sha256=final_sha256
            )

            # 6. 입력 정리 대기 및 입력 파일 제거
            self.journal.record_source_cleanup_pending(entry.input_rel)
            if src_input.exists():
                src_input.unlink()

            # 7. 완료 영속화 (G1은 사용자 승인 필수: approved_by_user=True)
            self.journal.record_completed(entry.input_rel, approved_by_user=True)
            applied.append(entry.input_rel)

        return applied

    def undo(self, input_rel: Optional[str] = None) -> list[str]:
        """완료된 항목 되돌리기.
        
        규칙:
        - 원본 복구 및 해시 확인.
        - 최종본이 이후 수정되었거나, 원 경로가 이미 사용 중이면 타 파일을 지우지 않고 충돌로 멈춤.
        """
        all_entries = self.journal.list_entries(state=State.COMPLETED)
        if input_rel:
            key = unicodedata.normalize("NFC", input_rel.replace("\\", "/"))
            candidates = [e for e in all_entries if e.input_rel == key]
            if not candidates:
                raise KeyError(f"완료 상태인 저널 항목을 찾을 수 없습니다: {input_rel}")
        else:
            candidates = all_entries

        undone: list[str] = []
        for entry in candidates:
            target_rel = entry.actual_input_rel or entry.input_rel
            dest_input = self.input_dir / target_rel

            # 1. 원래 경로 사용 중인지 확인 (타 파일 보호)
            if dest_input.exists():
                raise FileExistsError(
                    f"되돌리기 충돌: 원래 입력 경로에 이미 파일이 존재합니다: {dest_input}"
                )

            # 2. 백업 파일 존재 및 해시 확인
            if not entry.backup_path or not Path(entry.backup_path).exists():
                raise FileNotFoundError(
                    f"백업 파일을 찾을 수 없어 되돌릴 수 없습니다: {entry.input_rel}"
                )
            backup_p = Path(entry.backup_path)
            if compute_sha256(backup_p) != entry.original_sha256:
                raise ValueError(
                    f"백업본 해시가 원본과 일치하지 않습니다: {entry.input_rel}"
                )

            # 3. 최종본 파일 확인 및 해시 확인
            if not entry.final_path:
                raise ValueError(f"최종본 경로 정보가 없습니다: {entry.input_rel}")
            final_p = self.done_dir / entry.final_path
            if not final_p.exists():
                raise FileNotFoundError(
                    f"완료 폴더에 최종본 파일이 존재하지 않습니다: {final_p}"
                )
            if compute_sha256(final_p) != entry.final_sha256:
                raise ValueError(
                    f"되돌리기 충돌: 최종본이 변경되었으므로 삭제하지 않고 중단합니다: {final_p}"
                )

            # 4. 원본 복구 및 해시 확인
            dest_input.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup_p, dest_input)
            restored_sha256 = compute_sha256(dest_input)
            if restored_sha256 != entry.original_sha256:
                if dest_input.exists():
                    dest_input.unlink()
                raise ValueError(
                    f"복원된 입력 파일의 해시가 일치하지 않습니다: {dest_input}"
                )

            # 5. 최종본 삭제
            final_p.unlink()

            # 6. 저널 기록 되돌리기
            self.journal.record_undo(entry.input_rel)
            undone.append(entry.input_rel)

        return undone

    def recover(self) -> list[str]:
        """중단되거나 비정상 종료된 중간 상태 복구.
        
        보호 후 중단, 배치 후 입력 정리 전 중단 등의 장애 발생 시
        중복 최종본 생성이나 입력 손실 없이 안전하게 복구합니다.
        """
        recovered: list[str] = []
        entries = self.journal.list_entries()

        # 남아있는 임시 파일 정리
        for tmp_file in self.done_dir.glob(".tmp_*"):
            try:
                tmp_file.unlink()
            except Exception:
                pass

        for entry in entries:
            src_input = self._resolve_input_path(entry.input_rel, entry.actual_input_rel)

            # 1. 배치 완료 후 입력 정리 전 중단된 상태 (PUBLISHED, SOURCE_CLEANUP_PENDING)
            if entry.state in (State.PUBLISHED, State.SOURCE_CLEANUP_PENDING):
                final_p = self.done_dir / (entry.final_path or "")
                if final_p.exists() and compute_sha256(final_p) == entry.final_sha256:
                    if src_input and src_input.exists():
                        # 안전 검증: 입력 파일이 변경되었는지 확인
                        src_hash = compute_sha256(src_input)
                        if src_hash != entry.original_sha256:
                            reason = "복구 중단: 입력 파일 내용이 변경되어 삭제를 중단하고 보존함"
                            self.journal.record_held(entry.input_rel, reason=reason)
                            recovered.append(entry.input_rel)
                            continue
                        src_input.unlink()

                    self.journal.record_completed(
                        entry.input_rel, approved_by_user=entry.approved_by_user or True
                    )
                    recovered.append(entry.input_rel)
                else:
                    entry.state = State.APPROVED
                    recovered.append(entry.input_rel)

            # 2. 임시 파일 생성 중 중단된 상태 (PUBLISHING) (Codex finding 2 해결)
            elif entry.state == State.PUBLISHING:
                intended_p = (
                    self.done_dir / entry.intended_final_path
                    if entry.intended_final_path
                    else None
                )
                # 최종본 경로에 파일이 존재하고, 기대 해시(expected_final_sha256)와 정확히 일치할 때만 발행 완료로 인정
                if (
                    intended_p
                    and intended_p.exists()
                    and entry.expected_final_sha256
                    and compute_sha256(intended_p) == entry.expected_final_sha256
                ):
                    self.journal.record_published(
                        entry.input_rel,
                        final_path=entry.intended_final_path,
                        final_sha256=entry.expected_final_sha256,
                    )
                    if src_input and src_input.exists():
                        if compute_sha256(src_input) == entry.original_sha256:
                            src_input.unlink()
                        else:
                            self.journal.record_held(
                                entry.input_rel,
                                reason="복구 중단: 입력 파일 내용이 변경되어 보존함",
                            )
                            recovered.append(entry.input_rel)
                            continue
                    self.journal.record_completed(
                        entry.input_rel, approved_by_user=entry.approved_by_user or True
                    )
                    recovered.append(entry.input_rel)
                elif intended_p and intended_p.exists():
                    # 파일은 존재하나 기대 해시와 다름 -> 충돌로 보존하고 덮어쓰지 않음
                    self.journal.record_held(
                        entry.input_rel,
                        reason=f"복구 충돌: 대상 경로({entry.intended_final_path})에 다른 내용의 파일이 존재합니다.",
                    )
                    recovered.append(entry.input_rel)
                else:
                    # 배치가 완료되지 않았음 -> APPROVED 로 롤백
                    entry.state = State.APPROVED
                    recovered.append(entry.input_rel)

            # 3. 백업 후 중단된 상태 (BACKED_UP) -> 재스캔 시 추출 재개 가능하도록 상태 유지
            elif entry.state == State.BACKED_UP:
                recovered.append(entry.input_rel)

        return recovered

    def export_ledger(self) -> dict:
        """ledger.schema.json 호환 JSON 딕셔너리 반환."""
        return self.journal.export_ledger()
