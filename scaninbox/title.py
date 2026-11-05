"""규칙 기반 제목·날짜 제안(LLM 없음, 외부 전송 없음).

OCR 텍스트에는 글자 크기·위치 정보가 없으므로 다음 규칙을 쓴다.

1. 쪽 텍스트를 정규화(NUL·제어 문자 제거, NFC)하고 줄로 나눈다.
2. 제목이 아닌 줄을 건너뛴다: 기관명 머리줄, 문서번호·``Ref.`` 줄, 날짜만 있는 줄, 쪽번호, ``라벨:`` 줄,
   번호 붙은 문단, 문장형 줄(``…함.``/``…다.``), 표 머리글, 인식 쓰레기 줄. ``제목: …`` 라벨은 값을 제목으로 쓴다.
3. 남은 첫 줄이 제목 후보다. 후보에 인식 쓰레기 흔적(``& [ ] =`` 등 기호, 한글 사이의 2자 이하 라틴 조각)이
   있거나 제목답지 않으면 **제목을 내지 않고** ``review_reasons`` 로 돌린다. 후보 줄은 검토 화면용으로
   ``evidence_snippet`` 에만 남긴다(오답을 자신 있게 내지 않는 쪽을 택함).
4. 날짜는 머리 영역(첫 쪽의 앞쪽 줄, 라벨이 붙은 줄)에서만 찾고, 서로 다른 날짜가 둘 이상이면 고르지 않는다.
   날짜가 없으면 ``None`` 이며 스캔일로 대체하지 않는다.
5. 서로 다른 제목 후보가 둘 이상의 쪽에 있으면 다중 문서 의심, 제목줄이 없는 서식은 검토 사유로 표시한다.

OCR 이 한글 띄어쓰기를 잃는 경우가 많아(``연구용역착수회의결과``) 제목의 공백은 복원하지 않는다. 최종 이름은
승인 단계에서 사람이 고친다. 로그에는 본문을 남기지 않는다(이 모듈은 로깅하지 않는다).
"""
from __future__ import annotations

import datetime as _dt
import re
import unicodedata
from typing import Optional, Sequence

from .contracts import TitleProposal

_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_WS_RE = re.compile(r"\s+")
_HANGUL_RUN_RE = re.compile(r"[가-힣]")

# ---- 줄 분류 규칙 ------------------------------------------------------------------------------

# '결과'·'성과'·'효과' 처럼 '과' 로 끝나는 보통 명사를 기관명으로 오인하지 않도록 앞 글자를 제외한다.
_ORG_SUFFIX_RE = re.compile(r"((?<![결성효])과|부|팀|처|청|실|국|센터|위원회|사무소|사무국|Office|Department|Division|Team|Center)$")
_DOCNO_RE = re.compile(r"^(문\s*서\s*번\s*호|시\s*행|접\s*수|Ref\b\.?|No\b\.?|Doc(ument)?\s*No)", re.I)
_PAGENO_RE = re.compile(r"^[-–—\s]*\d{1,3}\s*(/\s*\d{1,3})?[-–—\s]*$")
_LABEL_RE = re.compile(r"^[^\s:：]{1,8}(\s[^\s:：]{1,6})?\s*[:：]")
_NUMBERED_RE = re.compile(r"^(\d{1,2}[.,)]|[가-하][.)]|[①-⑩]|\(\d{1,2}\))\s*\S")
_ATTACH_RE = re.compile(r"^\[?\s*(붙\s*임|별\s*첨|첨\s*부)")
_KO_SENTENCE_END_RE = re.compile(r"(함|음|임|됨|다|요|니다|합니다|입니다|바랍니다)\s*[.。]\s*$")
_KO_SENTENCE_MID_RE = re.compile(r"(함|음|임|됨)\s*[.。]\s*\S")
_SENTENCE_END_RE = re.compile(r"(함|음|임|됨|다|요|니다|합니다|입니다|바랍니다)\s*[.。]\s*$|[.!?]\s*$")
_STRAY_RE = re.compile(r"[\[\]{}|&=^$~!@#<>\\`]")
_TITLE_LABEL_RE = re.compile(r"^제\s*목\s*[:：]\s*(.+)$")

# ---- 날짜 ----------------------------------------------------------------------------------------

_DATE_KR_RE = re.compile(r"(?<!\d)(\d{4})\s*[년.\-/]\s*(\d{1,2})\s*[월.\-/]\s*(\d{1,2})\s*(?:일|\.|(?!\d))")
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
_DATE_EN_RE = re.compile(
    r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?\s*,?\s+(\d{4})\b", re.I)
_DATE_LABEL_RE = re.compile(r"(작\s*성\s*일|신\s*청\s*일|시\s*행\s*일|일\s*자|날\s*짜|발\s*행\s*일|Date)", re.I)

_HEADER_LINES = 5  # 첫 쪽에서 기관명 줄을 뺀 앞쪽 몇 줄을 '머리 영역'으로 볼지

_TYPE_HINTS = [
    (re.compile(r"회의(록|결과)?$"), "회의록"), (re.compile(r"(신청서|요청서|확인서|동의서)$"), "서식"),
    (re.compile(r"(계획|계획서|계획\(안\))$"), "계획서"), (re.compile(r"(보고서?|결과)$"), "보고서"),
    (re.compile(r"(안내|통보|공고|알림)$"), "공문"),
    (re.compile(r"(Summary|Report|Guide|Schedule|Notice|Plan|Minutes)$", re.I), None),
]


def normalize(text: str) -> str:
    """NUL·제어 문자 제거, NFC, 줄 끝 공백 정리(줄바꿈은 유지)."""
    t = unicodedata.normalize("NFC", _CTRL_RE.sub("", text))
    return "\n".join(_WS_RE.sub(" ", ln).strip() for ln in t.splitlines())


def _compact(s: str) -> str:
    return _WS_RE.sub("", s)


def _lines(text: str) -> list[str]:
    return [ln for ln in normalize(text).split("\n") if ln.strip()]


def _parse_dates(line: str) -> list[str]:
    out: list[str] = []
    for m in _DATE_KR_RE.finditer(line):
        d = _valid_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if d:
            out.append(d)
    for m in _DATE_EN_RE.finditer(line):
        d = _valid_date(int(m.group(3)), _MONTHS[m.group(1).lower()[:3]], int(m.group(2)))
        if d:
            out.append(d)
    return out


def _valid_date(y: int, mo: int, d: int) -> Optional[str]:
    if not 1990 <= y <= 2100:
        return None
    try:
        return _dt.date(y, mo, d).isoformat()
    except ValueError:
        return None


def _strip_dates(line: str) -> str:
    s = _DATE_KR_RE.sub(" ", line)
    return _DATE_EN_RE.sub(" ", s)


def _org_like(line: str) -> bool:
    """짧고 끝이 기관 접미사인 줄(첫 줄 기관명 후보). 접미사만으로는 확정하지 않는다('결과'도 '과'로 끝남).
    구조(뒤에 제목 후보가 더 있음)와 함께 ``_page_candidate`` 가 판정한다."""
    c = _compact(line)
    if re.search(r"[가-힣]", c):
        return len(c) <= 14 and bool(_ORG_SUFFIX_RE.search(c))
    words = line.split()
    return len(words) <= 4 and bool(_ORG_SUFFIX_RE.search(words[-1]))


def _is_meta_line(line: str) -> bool:
    """문서번호·날짜·쪽번호처럼 제목이 아닌 머리 정보 줄."""
    if _DOCNO_RE.match(line) or _PAGENO_RE.match(line):
        return True
    if _parse_dates(line):
        rest = _compact(_strip_dates(line))
        rest = re.sub(r"[\d/.,:\-–—()]+", "", rest)
        # 날짜 말고는 라벨(작성일·신청일 등)이나 기호뿐이면 메타 줄
        return len(rest) <= 6 or bool(_DATE_LABEL_RE.match(line))
    return False


def _junk_line(line: str) -> bool:
    """글자다운 부분이 거의 없는 인식 쓰레기 줄(도장·로고·선이 만든 잡음)."""
    c = _compact(line)
    if not c:
        return True
    letters = sum(1 for ch in c if ("가" <= ch <= "힣") or (ch.isascii() and ch.isalpha()))
    run = re.search(r"[가-힣]{3,}", c) or re.search(r"[A-Za-z]{4,}", c)
    return letters / len(c) < 0.5 or run is None


def _title_noise(line: str) -> Optional[str]:
    """인식 쓰레기 흔적이 있으면 사유를 돌려준다."""
    if _STRAY_RE.search(line):
        return "제목 후보에 인식 잡음 기호가 있음"
    toks = line.split()
    hangul_dominant = len(_HANGUL_RUN_RE.findall(line)) >= len(re.findall(r"[A-Za-z]", line))
    if hangul_dominant:
        for t in toks:
            if re.fullmatch(r"[A-Za-z0-9]{1,2}", t) and not re.fullmatch(r"\d{1,2}", t):
                return "제목 후보에 한글과 섞인 2자 이하 라틴 조각이 있음"
            if re.fullmatch(r"[a-z]{3}", t):
                return "제목 후보에 한글과 섞인 소문자 3자 라틴 조각이 있음"
    for t in toks:  # 한글에 붙은 소문자 라틴 꼬리/머리('문서ro'): 도장·잡티가 만든 글자
        if re.search(r"[가-힣][a-z]{1,3}$", t) or re.search(r"^[a-z]{1,3}[가-힣]", t):
            return "제목 후보에 한글에 붙은 소문자 라틴 조각이 있음"
    if re.search(r"\d{5,}", line):
        return "제목 후보에 비정상적인 긴 숫자열이 있음"
    return None


def _title_like(line: str) -> bool:
    c = _compact(line)
    if len(c) < 4 or len(c) > 100:
        return False
    if not (re.search(r"[가-힣]{3,}", c) or len(re.findall(r"[A-Za-z]{3,}", line)) >= 2):
        return False
    if _KO_SENTENCE_END_RE.search(line.strip()) or _KO_SENTENCE_MID_RE.search(line):
        return False  # 한국어 문장 종결 + 마침표는 본문(줄 중간에 문장이 끝나는 경우 포함)
    if re.search(r"[,，]", line) or len(line.split()) >= 9:
        return False  # 쉼표가 있거나 단어가 아주 많은 줄은 본문 문장일 가능성이 크다(제목이면 검토로 넘어감)
    if _SENTENCE_END_RE.search(line.strip()) and len(line.split()) >= 5:
        return False  # 영어 문장
    return True


def _table_like(lines: list[str], i: int) -> bool:
    """후보와 뒤따르는 줄들이 모두 짧은 항목명이면 서식(표·양식)으로 본다."""
    window = lines[i:i + 3]
    return len(window) >= 3 and all(len(_compact(w)) <= 8 for w in window)


def _candidates(lines: list[str], *, first_page: bool) -> tuple[list[tuple[int, str]], Optional[str]]:
    """본문이 시작되기 전의 제목 후보 줄들[(줄 번호, 줄)]과 ``제목:`` 라벨 값을 돌려준다."""
    for i, ln in enumerate(lines):
        m = _TITLE_LABEL_RE.match(ln)
        if m:
            return [(i, m.group(1).strip())], m.group(1).strip()
    found: list[tuple[int, str]] = []
    i = 0
    while i < len(lines) and len(found) < 3:
        ln = lines[i]
        if _is_meta_line(ln) or _junk_line(ln):
            i += 1
            continue
        if _LABEL_RE.match(ln) or _NUMBERED_RE.match(ln) or _ATTACH_RE.match(ln):
            break  # 본문 시작
        if not _title_like(ln):
            if first_page and not found and _table_like(lines, i):
                break
            if len(_compact(ln)) <= 3 and i + 1 < len(lines):
                i += 1  # 머리 잡음 한 줄은 건너뛴다
                continue
            break
        if first_page and not found and _table_like(lines, i):
            break
        cand, used = ln, 1
        # 제목이 두 줄로 나뉜 경우(짧은 라틴 단어 한 줄이 이어짐)
        if i + 1 < len(lines):
            nxt = lines[i + 1]
            if (len(nxt.split()) <= 2 and re.fullmatch(r"[A-Za-z][A-Za-z\- ]*", nxt)
                    and not _SENTENCE_END_RE.search(cand) and not _is_meta_line(nxt)
                    and re.search(r"[A-Za-z]{3,}$", cand)):
                cand, used = f"{cand} {nxt}", 2
        found.append((i, cand))
        i += used
    return found, None


def _page_candidate(text: str, *, first_page: bool, repeated: frozenset[str] = frozenset()) -> dict:
    """한 쪽의 제목 후보. 반환: dict(line, idx, label, ambiguous(사유|None), lines)."""
    lines = _lines(text)
    res: dict = dict(line=None, idx=None, label=False, ambiguous=None, lines=lines)
    cands, label_value = _candidates(lines, first_page=first_page)
    if label_value is not None:
        res.update(line=label_value, idx=cands[0][0], label=True)
        return res
    if not cands:
        return res
    # 첫 줄이 기관명(머리글)이고 뒤에 후보가 더 있으면 첫 줄을 건너뛴다
    if len(cands) >= 2 and cands[0][0] == 0 and (_org_like(cands[0][1]) or _compact(cands[0][1]) in repeated):
        cands = cands[1:]
    idx, line = cands[0]
    res.update(line=line, idx=idx)
    if len(cands) == 1 and idx == 0 and (_org_like(line) or _compact(line) in repeated):
        # 기관명·머리글처럼 보이는 줄 하나뿐: 제목을 놓쳤을 가능성이 커서 제목으로 확정하지 않는다
        if not first_page:
            res.update(line=None, idx=None)  # 뒤쪽의 반복 머리글은 제목 후보가 아니다
            return res
        res["ambiguous"] = "기관명으로 보이는 줄뿐이고 제목줄을 찾지 못함"
    others = [c for _, c in cands[1:] if not _title_noise(c)]
    if others and len(_compact(line)) <= 8:
        res["ambiguous"] = "첫 줄이 짧아 기관명일 수 있고 제목 후보가 여러 줄임"
    return res


def _repeated_headers(pages_lines: list[list[str]]) -> set[str]:
    """둘 이상의 쪽 첫 줄에 반복되는 줄(기관명 머리글)."""
    if len(pages_lines) < 2:
        return set()
    firsts = [_compact(p[0]) for p in pages_lines if p]
    return {f for f in firsts if f and firsts.count(f) >= 2}


def _header_dates(lines: list[str], title_idx: Optional[int]) -> list[str]:
    """머리 영역의 날짜. 기관명 줄을 뺀 앞쪽 몇 줄과 제목 바로 위·아래의 메타 줄, 날짜 라벨 줄."""
    found: list[str] = []
    start = 1 if lines and _org_like(lines[0]) and len(lines) > 1 else 0
    head = lines[start:start + _HEADER_LINES]
    if title_idx is not None:
        head = list(dict.fromkeys(head + lines[max(0, title_idx - 2):title_idx + 3]))
    for ln in head:
        ds = _parse_dates(ln)
        if not ds:
            continue
        if _is_meta_line(ln) or _DATE_LABEL_RE.search(ln) or ln in lines[start:start + 3]:
            found.extend(ds)
    return list(dict.fromkeys(found))


def _snippet(s: str) -> str:
    s = s.strip()
    return s if len(s) <= 80 else s[:79] + "…"


def _doc_type(title: str) -> Optional[str]:
    c = _compact(title)
    for rx, label in _TYPE_HINTS:
        if rx.search(c):
            return label
    return None


class RuleTitleExtractor:
    """``contracts.TitleExtractor`` 구현(규칙 기반)."""

    def propose(self, pdf, text_by_page: Sequence[str]) -> TitleProposal:  # noqa: ARG002 - pdf 는 현재 미사용
        texts = [normalize(t) for t in text_by_page]
        if not any(_compact(t) for t in texts):
            return TitleProposal(title=None, review_reasons=["OCR 텍스트가 없음"])
        pages_lines = [_lines(t) for t in texts]
        repeated = _repeated_headers(pages_lines)

        cands = []
        for pi, t in enumerate(texts):
            cands.append(_page_candidate(t, first_page=(pi == 0), repeated=frozenset(repeated)))

        reasons: list[str] = []
        first = cands[0]
        title: Optional[str] = None
        evidence_page: Optional[int] = None
        snippet = ""

        # 제목 후보는 첫 쪽에서만 취한다(뒤쪽은 다중 문서 판정에 쓴다).
        if first["line"]:
            evidence_page = 1
            snippet = _snippet(first["line"])
            noise = _title_noise(first["line"])
            if noise:
                reasons.append(noise)
            elif first["ambiguous"]:
                reasons.append(first["ambiguous"])
            elif not first["label"] and not _title_like(first["line"]):
                reasons.append("제목 후보가 제목답지 않음")
            else:
                title = _WS_RE.sub(" ", first["line"]).strip()
        else:
            reasons.append("제목줄을 찾지 못함(제목 없는 서식이거나 OCR 이 제목을 놓침)")

        # 다중 문서 의심: 서로 다른 제목 후보가 둘 이상의 쪽에 있음
        titled_pages = [(pi + 1, _compact(c["line"])) for pi, c in enumerate(cands)
                        if c["line"] and not c["ambiguous"] and not _title_noise(c["line"]) and _title_like(c["line"])]
        distinct = {c for _, c in titled_pages}
        multi = len(titled_pages) >= 2 and len(distinct) >= 2
        # 쪽마다 서로 다른 머리 날짜가 반복되는 경우도 보강 신호로 쓴다
        page_dates = []
        for pi, ls in enumerate(pages_lines):
            ds = _header_dates(ls, cands[pi]["idx"])
            if ds:
                page_dates.append((pi + 1, ds[0] if len(ds) == 1 else None))
        date_distinct = {d for _, d in page_dates if d}
        multi = multi or (len(date_distinct) >= 2 and len(titled_pages) >= 2)
        if multi:
            pg = ",".join(str(p) for p, _ in titled_pages[:5])
            reasons.append(f"다중 문서 의심(쪽 {pg} 에 서로 다른 제목)")
            title = None  # 첫 문서의 제목을 묶음 전체의 이름으로 확정하지 않는다(후보는 evidence_snippet 에 남김)

        # 날짜: 첫 쪽 머리 영역
        doc_date: Optional[str] = None
        ds1 = _header_dates(pages_lines[0], first["idx"]) if pages_lines else []
        if multi:
            pass  # 날짜도 첫 문서의 것일 수 있어 묶음 전체에 붙이지 않는다
        elif len(ds1) == 1:
            doc_date = ds1[0]
        elif len(ds1) > 1:
            reasons.append("날짜 후보가 여러 개라 고르지 않음")

        return TitleProposal(title=title, evidence_page=evidence_page, evidence_snippet=snippet,
                             doc_date=doc_date, doc_type=_doc_type(title) if title else None,
                             review_reasons=reasons)
