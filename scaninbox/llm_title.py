"""로컬 LLM(Ollama) 제목 제안 (선택 기능, 기본 꺼짐).

규칙 추출기(``RuleTitleExtractor``)를 먼저 돌리고, 규칙이 검토 사유 없이 제목을 내지 못했을 때만 첫 쪽 OCR 텍스트를 사용자가 지정한 Ollama 서버에 보내 제목을
다시 묻는다. LLM 답은 아래 검증을 모두 통과할 때만 쓰고, 아니면 규칙 결과를 그대로 돌려준다.

- 모델이 ``confidence: high`` 로 답했을 것
- 제목 글자(공백 제외 2글자 묶음)의 60% 이상이 원문 첫 쪽에 있을 것(지어낸 제목 차단)
- 제목에 한글 또는 라틴 글자가 2자 이상 있고 150자 이하일 것(날짜만 있는 줄 차단)
- 날짜는 원문 첫 쪽에 같은 연·월·일이 실제로 적혀 있을 때만 쓴다(지어낸 날짜 차단)

LLM 이 낸 제목에는 항상 검토 사유를 붙여 사용자가 확인하게 한다. 서버 연결 실패·시간 초과·형식 오류는
조용히 규칙 결과로 돌아간다. OCR 텍스트는 지정한 서버로만 가며, 서버는 사용자 자신의 머신이어야 한다.
"""
from __future__ import annotations

import datetime as _dt
import json
import re
import sys
import urllib.request
from typing import Optional, Sequence
from urllib.parse import urlparse

from .contracts import TitleProposal
from .title import RuleTitleExtractor, _doc_type, normalize

DEFAULT_URL = "http://127.0.0.1:11434"
DEFAULT_MODEL = "qwen3:30b-a3b"
MAX_CHARS = 1500  # 첫 쪽에서 보내는 최대 글자 수

_PROMPT = (
    "다음은 스캔 문서 첫 쪽의 OCR 텍스트다. OCR 잡음과 음절 사이 띄어쓰기 오류가 있다. "
    "문서의 제목을 원문 표현대로(띄어쓰기는 바로잡아) 한 줄로 쓰고, 문서 작성일이 본문에 명시돼 있으면 "
    "YYYY-MM-DD 로 쓴다. 제목을 확신할 수 없으면 title 을 null 로. 추측해서 지어내지 말 것. "
    'JSON 으로만 답: {"title": ..., "date": ..., "confidence": "high|low"}\n\n---\n'
)

LLM_REASON = "로컬 LLM 이 제안한 제목(원문 대조 통과, 확인 필요)"


def _compact(s: str) -> str:
    return re.sub(r"\s", "", s or "")


def _bigrams(s: str) -> set[str]:
    c = _compact(s)
    return {c[i:i + 2] for i in range(len(c) - 1)}


def title_supported(title: str, text: str) -> bool:
    if not title or len(title) > 150:
        return False
    if len(re.findall(r"[가-힣A-Za-z]", title)) < 2:
        return False
    b = _bigrams(title)
    if not b:
        return False
    return len(b & _bigrams(text)) / len(b) >= 0.6


def date_supported(date: Optional[str], text: str) -> Optional[str]:
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", date or "")
    if not m:
        return None
    y, mo, d = m.group(1), int(m.group(2)), int(m.group(3))
    try:
        _dt.date(int(y), mo, d)
    except ValueError:
        return None  # 달력에 없는 날짜(2월 30일 등)
    c = _compact(text)
    full = rf"(?<!\d){y}[년.\-/]0?{mo}[월.\-/]0?{d}(?!\d)"
    short = rf"(?<!\d){y[2:]}[.\-/]0?{mo}[.\-/]0?{d}(?!\d)"
    return date if re.search(full, c) or re.search(short, c) else None


def _valid_url(url: str) -> str:
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.netloc:
        raise ValueError(f"Ollama 주소가 올바르지 않음: {url!r} (예: http://127.0.0.1:11434)")
    return url.rstrip("/")


class OllamaTitleExtractor:
    """``contracts.TitleExtractor`` 구현. 규칙 결과를 기본으로 하고 LLM 답이 검증을 통과하면 제목을 바꾼다."""

    def __init__(self, url: str = DEFAULT_URL, model: str = DEFAULT_MODEL, timeout_s: float = 300.0,
                 fallback: Optional[RuleTitleExtractor] = None):
        self.url = _valid_url(url)
        self.model = model
        self.timeout_s = timeout_s
        self.fallback = fallback or RuleTitleExtractor()
        self._warned = False

    def _ask(self, text: str) -> Optional[dict]:
        body = json.dumps({
            "model": self.model, "prompt": _PROMPT + text[:MAX_CHARS], "format": "json",
            "stream": False, "think": False, "options": {"temperature": 0},
        }).encode("utf-8")
        req = urllib.request.Request(f"{self.url}/api/generate", data=body,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as r:
                resp = json.load(r)
            if not isinstance(resp, dict) or not isinstance(resp.get("response"), str):
                raise ValueError("unexpected response envelope")
            out = json.loads(resp["response"])
            return out if isinstance(out, dict) else None
        except Exception as e:  # noqa: BLE001 - 어떤 실패든 규칙 결과로 돌아간다(스캔을 멈추지 않음)
            if not self._warned:
                print(f"경고: 로컬 LLM 호출 실패, 규칙 제목만 씁니다 ({type(e).__name__})", file=sys.stderr)
                self._warned = True
            return None

    def propose(self, pdf, text_by_page: Sequence[str]) -> TitleProposal:
        rule = self.fallback.propose(pdf, text_by_page)
        if rule.title and not rule.review_reasons:
            return rule  # 규칙이 검토 사유 없이 낸 제목은 그대로 쓴다(시간 절약, 바꿀 이유 없음)
        first = normalize(text_by_page[0]) if text_by_page else ""
        if not _compact(first):
            return rule
        ans = self._ask(first)
        if not ans or ans.get("confidence") != "high":
            return rule
        title = ans.get("title")
        if not isinstance(title, str):
            return rule
        title = re.sub(r"\s+", " ", title).strip()
        if not title_supported(title, first):
            return rule
        date = rule.doc_date or date_supported(ans.get("date") if isinstance(ans.get("date"), str) else None, first)
        reasons = [r for r in rule.review_reasons if not r.startswith("제목")]
        reasons.append(LLM_REASON)
        snippet = title if len(title) <= 80 else title[:79] + "…"
        return TitleProposal(title=title, evidence_page=1, evidence_snippet=snippet, doc_date=date,
                             doc_type=_doc_type(title), review_reasons=reasons)
