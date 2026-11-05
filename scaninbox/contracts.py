"""모듈 사이 계약(데이터 형식과 Protocol). 구현 모듈은 이 파일을 import 해서 쓰고, 바꿀 때는 총괄과 협의한다.

- core(pathsafe/config/journal): 경로 안전, 설정 검증, 상태 journal
- engines(ocr/title): OCR 실행·검증, 제목 규칙 추출
- pipeline/cli: 위 부품을 조합해 보호→추출→제안→승인→배치→복구를 수행
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Protocol, Sequence


class State(str, enum.Enum):
    """파일별 처리 상태. 오류는 단계별 failed 로 보존한다."""
    DISCOVERED = "discovered"
    WAITING_STABLE = "waiting_stable"
    BACKED_UP = "backed_up"
    EXTRACTED = "extracted"
    REVIEW_PENDING = "review_pending"      # 제안 완료, 사용자 승인 대기
    APPROVED = "approved"
    PUBLISHING = "publishing"
    PUBLISHED = "published"                # 최종본이 완료 폴더에 있음
    SOURCE_CLEANUP_PENDING = "source_cleanup_pending"
    COMPLETED = "completed"
    HELD = "held"                          # 검토 불가·보류(이유 필수). 입력에 남김
    FAILED = "failed"                      # 처리 실패(이유 필수). 입력에 남김


class OcrStatus(str, enum.Enum):
    OK = "ok"                    # 모든 쪽에서 텍스트층 확인
    PARTIAL = "partial"          # 일부 쪽 텍스트 0자(회전·빈 쪽 등) -> 검토 대상
    ALREADY_TEXT = "already_text"  # 이미 충분한 텍스트층. OCR 생략
    ENCRYPTED = "encrypted"
    CORRUPT = "corrupt"
    FAILED = "failed"            # 엔진 오류·시간 초과


@dataclass
class OcrResult:
    status: OcrStatus
    output: Optional[Path] = None              # 텍스트층이 들어간 임시 출력(성공·partial 일 때)
    page_count_in: int = 0
    page_count_out: int = 0
    text_by_page: list[str] = field(default_factory=list)   # 출력 PDF 에서 다시 추출한 쪽별 텍스트
    empty_pages: list[int] = field(default_factory=list)    # 텍스트 0자인 쪽(1부터)
    reason: str = ""                            # 비정상일 때 사람이 읽을 사유(본문 발췌 금지)
    engine: str = ""                            # 예: "ocrmypdf 17.13.0 / tesseract 5.5.3 kor+eng"


@dataclass
class TitleProposal:
    title: Optional[str]                        # 제안 제목. 근거 없으면 None
    evidence_page: Optional[int] = None         # 제목 근거 쪽(1부터)
    evidence_snippet: str = ""                  # 짧은 발췌(최대 80자). 로그 저장 금지, 검토 화면용
    doc_date: Optional[str] = None              # YYYY-MM-DD. 불명이면 None(스캔일로 대체하지 않음)
    doc_type: Optional[str] = None
    review_reasons: list[str] = field(default_factory=list)  # 비어 있지 않으면 자동 승인 불가


@dataclass
class PlanItem:
    """미리보기 한 건. 승인 시점에 입력 해시·타겟 상태가 같은지 재검증한다."""
    input_rel: str
    original_sha256: str
    proposed_name: str                          # pathsafe 통과한 최종 파일명
    ocr_status: OcrStatus
    title: TitleProposal
    needs_review: bool                          # OCR partial·제목 불명·다중 문서 의심 등
    warnings: list[str] = field(default_factory=list)


class OcrEngine(Protocol):
    def run(self, src: Path, dst_dir: Path, *, langs: str = "kor+eng", timeout_s: int = 600) -> OcrResult: ...


class TitleExtractor(Protocol):
    def propose(self, pdf: Path, text_by_page: Sequence[str]) -> TitleProposal: ...
