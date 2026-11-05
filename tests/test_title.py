"""title.RuleTitleExtractor 시험.

``SAMPLES`` 는 합성 코퍼스(150dpi, OCRmyPDF 17.13.0 + Tesseract 5.5.3 kor+eng, tessdata_fast 4.1.0, macOS arm64)를
OCR 한 쪽별 텍스트 21건과 manifest 정답이다(완전 가상 문서의 OCR 결과이며 실문서 정보 없음). OCR 이 없는 환경에서도
제목 규칙의 회귀를 잡기 위해 텍스트를 그대로 박아 둔다. 이 표본으로 규칙을 다듬었으므로 **정확도 수치는
학습 표본 위의 수치**이며 일반 성능이 아니다. 일반화 여부는 아래 별도 단위 시험(표본에 없는 문서 모양)으로 본다.
"""
from __future__ import annotations

import os
import re
import unicodedata
from pathlib import Path

import pytest

from scaninbox.title import RuleTitleExtractor, normalize

EX = RuleTitleExtractor()


def _c(s):
    return "".join(unicodedata.normalize("NFC", s or "").split())


SAMPLES = [  # (category, 기대 제목, 기대 날짜, OCR 쪽별 텍스트)
    ('titled_scan_ko', '2026년도 예시 사업 추진계획', '2026-03-12',
     ['예시시 가상과\n문서번호 가 상 -0001 / 2026. 3. 12. \n20264 & 예시사업추진계획\n추진 배경: 가상의시설운영효율을높이기위하여연간사업일정을 정리함. \n2. 추진 목표: 점검주기를분기별로표준화하고결과를한곳에모아관리함 . \n3, 추진 방향: 담당부서는월별일정표를작성 하고, 필요한경우외부전문가의의견을구하되모든자료는\n예시용가상 CHOLES 사용함. \n구분 내용 4 \n1 분기 현황조사 1-3 월\n2 분기 시범운영 4-6 월\n',
      '예시시 가 상과\n4. 소요 예산: 본 계획의 예산은 가상의 금액이며실제집행과 무관함 . \n항목 | 금 액 (가상) \n운영비 000,000 \n점검비 000,000 \n5. 기대 효과: 업무절차가단순해지고기록의 일관성이높아질것으로 기대됨. \n검토\n']),
    ('titled_scan_ko', '연구용역 착수회의 결과', '2026-05-20',
     ['예시시 가상과\n연구용역착수회의결과\n2026. 5. 20. \n일시: 202614 5 월 20Y 오후 2A / 장소: 가상회의실 ^ \n참석자: 담당자 가, 담당자 나, 담당자다 (모두 가상 인물) \n안건 1. 착수일정확인 - 세부일정은별도공유하기로함 . \n안건 2. 역할분담 - 자료정리는담당자 가, 일정관리는담당자나가맡기로함 . \n결정 사항: 다음회의는 2 주뒤에같은장소에서열기로함 . \n확인\n']),
    ('titled_scan_ko', '물품 구매 요청서', '2026-11-03',
     ['예시시 가상과\n작성일 2026.11. 3. \n[=i == Z \n물품구매요청서\n품명 수량 tes \n가상사무용품세트 2 예시\n가상라벨지 5 네시\n위 물품의구매를 요청 합니다. 금액과 업체는 모두가상 입니다. \n결재\n']),
    ('titled_scan_ko', '시설 안전점검 안내', '2026-06-02',
     ['예시시 가상 과\n문 서 번호 가 상 -0042 / 2026. 6. 2. \n시설 안전점검 안내\n수신: 각 부서장 (가상) \n다음과 같이 정기 안전점검을 실시하오니 업무에 참고하시기 바랍니다. \n점검 기간: 2026 년 6 월 15 일부터 6 월 19 일까지 / 점검 대상: 가상 건물 1 동\n문의: 가 상 과 대 표번호 000-0000-0000 \n']),
    ('untitled_form', None, None,
     ['예시시 가상과\n성명\n기간\n연락처 000-0000-0000 \n위와 같이 신청 합니다. \n']),
    ('untitled_form', None, None,
     ['예시시 가상 과\nTE 내용\n작성일: 26. . ( 미 기 재 ) \n']),
    ('english_scan', 'Quarterly Facilities Maintenance Summary', '2026-04-15',
     ['Example Facilities Office \nRef. EX-0007 / April 15, 2026 \nQuarterly Facilities Maintenance Summary \nThis summary covers routine maintenance activities for the fictional East Building. \nAll inspections were completed on schedule. No critical issues were recorded. \nArea Status Note \nLobby Done Sample \nRoof Done Sample \nNext review is planned for the following quarter. This text is entirely synthetic. \n']),
    ('english_scan', 'Notice of Equipment Delivery Schedule', '2026-07-08',
     ['Example Facilities Office \nJuly 8, 2026 \nNotice of Equipment Delivery Schedule \nDear colleagues, the delivery of the sample equipment is scheduled for next Tuesday. \nPlease keep the loading area clear between 9:00 and 11:00. Thank you for your cooperation. \nSent \n']),
    ('mixed_lang_scan', '분기별 시설 점검 요약 Quarterly Inspection Summary', '2026-04-15',
     ['예시시 가상과\n2026. 04. 15 \n분 기별시설 점검 요약 Quarterly Inspection \nSummary \n이번분기점검결과를 요약 함 . The inspections were completed without critical issues. \nArea 24 Status 상태\nLobby 로비 OK es \nRoof $4 야양호\n']),
    ('mixed_lang_scan', 'Project Kickoff 착수 보고', '2026-08-03',
     ['예 시시 가 상 과\nProject Kickoff 착수보고\n2026. 08. 03 \nKickoff meeting 결과: 일정과역할을확인함 . Action items are listed below. \nItem 항목 Owner SS \nSchedule 일정 Team A7t \nBudget O/4t Team BLt \n']),
    ('text_layer_pdf', '2026년 예산 집행 현황 보고', '2026-09-01',
     ['예시시 가상과\n2026. 9. 1.\n2026년 예산 집행 현황 보고\n상반기 예산 집행률을 정리함. 모든 수치는 가상이며 실제 예산과 무관함.\n구분\n계획\n집행\n운영\n000\n000\n사업\n000\n000\n- 1 -\n',
      '예시시 가상과\n하반기 집행 계획: 분기별 점검 결과를 반영하여 조정하기로 함.\n특이사항 없음. 이 문서는 텍스트층이 있는 PDF 시험용 합성 자료임.\n- 2 -\n']),
    ('text_layer_pdf', 'Annual Training Schedule', '2026-01-10',
     ['Example Facilities Office\nJanuary 10, 2026\nAnnual Training Schedule\nThe following sessions are planned for the fictional staff training program.\nMonth\nSession\nMarch\nSafety basics\nSeptember\nRecords handling\n- 1 -\n']),
    ('mixed_pdf', '위탁 관리 운영 결과 보고', '2026-10-05',
     ['예시시 가상과\n2026. 10. 5.\n위탁 관리 운영 결과 보고\n위탁 운영 결과를 보고함. 본 문서는 가상이며 첫 쪽과 셋째 쪽은 텍스트층이 있고 나머지는 스캔임.\n- 1 -\n',
      '예 시시 가 상 과\n[붙임 1] 운영 일지 사본 (스캔 페이지) - 가상 기록: 점검 이상 없음. \n일자 내용\n10/1 점검\n10/2 정리\n',
      '예시시 가상과\n종합 의견: 다음 해에도 같은 방식으로 운영하는 것이 적절하다고 판단됨.\n- 3 -\n',
      '예시시 가상과\n[붙임 2] 확인서 사본 (스캔 페이지) - 가상 서명란 . \n확 인 자 서명\n']),
    ('rotated_blank', '현장 점검 결과 보고', '2026-02-17',
     ['예시시 가 상과\n2026. 2. 17. \n',
      '',
      '예 시시 가상 과\n[ 옆으로 AWS 쪽 ] 이 페이지는 가로 방향 종이로 스캔된 것처럼 이미지 자체 가 90 도 회전되어 있음. \nOCRE SHS 감지하여올바르게읽어야함 . \n',
      '[회전 속성쪽 ] 이미지는바르게서있으나 PDF 쪽회전속성이 90 도로지정되어 있음. \n',
      '']),
    ('multi_doc_bundle', None, None,
     ['예 시시 가상과\n2026. 4. 22. \n출장 결과 보고\n가상 출장 결과를 보고함 . 방문처와 인원은 모두 가상임 . \n',
      '예시시 가상과\n출장 중 확인한 사항과 후속 조치 계획을 정리함. \n',
      '예시시 가상과\n신청일 2026. 4. 23. \n더no \n과\n+ \n70 \n담당자 가\n',
      'Example Facilities Office \nApril 24, 2026 \nVisitor Access Guide \nVisitors must sign in at the front desk. This guide is fictional. \n',
      'Example Facilities Office \nBadges are returned at the end of each visit. \n']),
    ('name_collision', '회의 결과 보고', '2026-07-14',
     ['예시시 가상과\n회의 결과 보고\n2026.7.14. \n일시: 2026 년 7 월 14 일 오후 2 시 / 장소: 가상 회의실 4 \n참석자: 담당자 가, 담당자 나, 담당자 다 (모두 가상 인물) \n안건 1. 예산 일정 확인 - 세부 일정은 별도 공유하기로 함 . \n안건 2. 역할 분담 - 자료 정리는 담당자 가, 일정 관리는 담당자 나가 맡기로 함 . \n결정 사항: 다음 회의는 2 주 뒤에 같은 장소에서 열기로함 . \n']),
    ('name_collision', '회의 결과 보고', '2026-07-14',
     ['예시시 가상과\n회의 결과 보고\n2026.7.14. \n일시: 2026 년 7 월 14 일오후 2 시 / 장소: 가상회의실 ^ \n참석자: 담당자 가, 담당자 나, 담당자다 (모두 가상 인물) \n안건 1. 일정일정확인 - 세부일정은별도공유하기로함 . \n안건 2. 역할분담 - 자료정리는담당자 가, 일정관리는담당자나가맡기로함 . \n결정 사항: 다음회의는 2 주뒤에같은장소에서열기로함 . \n']),
    ('name_collision', '하위 폴더 시험 문서 가', '2026-08-11',
     ['예시시 가상과\n하위 폴더 시험 문서 가\n2026.8.11. \n일시: 2026 년 BB 11 일 오후 2 시 / 장소: 가상회의실 ^ \n참석자: 담당자 가, 담당자 나, 담당자다 (모두 가상 인물) \n안건 1. 가일정확인 - 세부일정은별도공유하기로함 . \n안건 2. 역할분담 - 자료정리는담당자 가, 일정관리는담당자나가맡기로함 . \n결정 사항: 다음회의는 2 주뒤에같은장소에서열기로함 . \n']),
    ('name_collision', '하위 폴더 시험 문서 나', '2026-08-12',
     ['예시시 가상과\n하위폴더시험문서나\n2026. 8. 12. \n일시: 202644 BB 12 일오후 2 시 / 장소: 가상회의실 A \n참석자: 담당자 가, 담당자 나, 담당자다 (모두 가상 인물) \n안건 1. 나일정확인 - 세부일정은별도공유하기로함 . \n안건 2. 역할분담 - 자료정리는담당자 가, 일정관리는담당자나가맡기로함 . \n결정 사항: 다음회의는 2 주뒤에같은장소에서열기로함 . \n']),
    ('name_collision', '긴 경로 시험 문서', '2026-09-10',
     ['예시시 가상과\n긴 경로 시험 문서\n2026.9.10. \n일시: 2026 년 9 월 10 일 오후 2 시 / 장소: 가상 회의실 4 \n참석자: 담당자 가, 담당자 나, 담당자 다 (모두 가상 인물) \n안건 1. 경로 일정 확인 - 세부 일정은 별도 공유하기로 함 . \n안건 2. 역할 분담 - 자료 정리는 담당자 가, 일정 관리는 담당자 나가 맡기로 함 . \n결정 사항: 다음 회의는 2 주 뒤에 같은 장소에서 열기로 함 . \n']),
    ('name_collision', '정규화 시험 문서', '2026-09-09',
     ['예시시 가상과\n정규화시험 문서\n2026.9.9. \n일시: 2026 년 9 월 9 일오후 2 시 / 장소: 가상회의실 4 \n참석자: 담당자 가, 담당자 나, 담당자다 (모두 가상 인물) \n안건 1. 정규화일정확인 - 세부 US 별도공유하기로함 . \n안건 2. 역할분담 - 자료정리는담당자 가, 일정 Bele 담당자나가맡기로함 . \n결정 사항: 다음회의는 2 주뒤에같은장소에서열기로함 . \n']),
]


def _measure():
    tot = dict(correct=0, wrong=0, review=0)
    detail = []
    for cat, exp_title, exp_date, pages in SAMPLES:
        p = EX.propose(None, pages)
        accepted = p.title is not None and not p.review_reasons
        kind = ("correct" if _c(p.title) == _c(exp_title) else "wrong") if accepted else "review"
        tot[kind] += 1
        detail.append((cat, exp_title, exp_date, p, kind))
    return tot, detail


def test_corpus_samples_never_confident_and_wrong():
    """오답을 자신 있게 내지 않는다: 자동 승인 후보(제목 있음, 검토 사유 없음)는 전부 정답이어야 한다."""
    tot, detail = _measure()
    assert tot["wrong"] == 0, [(d[0], d[1], d[3].title) for d in detail if d[4] == "wrong"]
    # 학습 표본 위의 측정값(2026-11-05, macOS): 정확 16 / 오답 0 / 검토 5 (21건, 검토 대상 정답 3건 포함).
    # 같은 코퍼스를 Windows(Tesseract 5.4.0)에서 OCR 하면 OCR 이 제목을 더 자주 놓쳐 정확 11 / 오답 0 / 검토 10.
    assert tot["correct"] >= 16 and sum(tot.values()) == 21


def test_expected_review_cases_are_not_accepted():
    """제목 없는 서식·다중 문서 묶음은 검토 사유 없이 통과하면 안 된다."""
    for cat, exp_title, exp_date, p, kind in _measure()[1]:
        if exp_title is None:
            assert kind == "review" and p.title is None and p.review_reasons, cat
            assert p.doc_date is None, cat  # 날짜를 지어내지 않는다


def test_doc_date_is_correct_or_none_on_corpus():
    for cat, exp_title, exp_date, p, kind in _measure()[1]:
        if p.doc_date is not None:
            assert p.doc_date == exp_date, (cat, p.doc_date, exp_date)


def test_ocr_lost_title_goes_to_review_with_candidate_kept_for_screen():
    # OCR 이 제목 글자를 깨뜨린 문서: 잡음 기호가 있는 후보는 제목으로 내지 않고 화면용 발췌만 남긴다.
    p = EX.propose(None, ["예시시 가상과\n문서번호 가 상 -0001 / 2026. 3. 12. \n20264 & 예시사업추진계획\n추진 배경: 가상 내용. \n"])
    assert p.title is None and p.review_reasons and p.evidence_page == 1
    assert "20264" in p.evidence_snippet and len(p.evidence_snippet) <= 80
    assert p.doc_date == "2026-03-12"


# Windows 11 x64(Tesseract 5.4.0, 맑은 고딕 합성 코퍼스)에서 OCR 한 결과 중 규칙이 처음에 자신 있게 틀렸던 두 건.
# 하나는 제목이 사라진 쪽의 본문 문장, 하나는 제목 뒤에 잡티가 만든 라틴 글자가 붙은 경우였다.
WIN_WRONG_CASES = [
    ["예 시시 가 상 과\n2026. 2. 17. \nA \n= \nrat \not \n고\n교\nica \nHI \nKI \n"
     "현장 점검 결과를 정리함. 2 쪽은 빈 페이지, 3 쪽은 엮으로 스 캔된 쪽 , 4 쪽 은 PDF 회전 속 성 이 있는\n쪽임 . : \n"],
    ["예 시 시 가 상 과\n긴 경로 시험 문서ro \n2026. 9. 10. \n일시: 20264 9 월 108 오후 2 시 / 장소: 가상 회의실 4 \n"
     "참석자: 담당자 가, 담당자 나, 담당자 다 (모두 가상 인물) \n"],
]


@pytest.mark.parametrize("pages", WIN_WRONG_CASES)
def test_windows_ocr_regressions_are_not_accepted(pages):
    p = EX.propose(None, pages)
    assert p.title is None and p.review_reasons  # 오답을 제목으로 내지 않고 검토로 넘긴다
    assert p.evidence_snippet  # 화면용 후보는 남긴다


# ---- 표본에 없는 문서 모양(일반화 확인) --------------------------------------------------------------------

@pytest.mark.parametrize("text,date", [
    ("시설 점검 결과 보고\n2026년 3월 5일\n본문 내용입니다.", "2026-03-05"),
    ("업무 협조 요청서\n2026-03-05\n수신: 각 부서", "2026-03-05"),
    ("월간 업무 보고\n작성일 2026.03.05\n1. 추진 실적: 완료함.", "2026-03-05"),
    ("Annual Budget Review\nMarch 5, 2026\nThis document is fictional.", "2026-03-05"),
    ("회의 결과 보고\n2026 년 7 월 14 일\n본문", "2026-07-14"),
])
def test_date_formats(text, date):
    p = EX.propose(None, [text])
    assert p.doc_date == date


def test_invalid_date_is_ignored_not_invented():
    p = EX.propose(None, ["정기 점검 결과\n2026-13-40\n내용"])
    assert p.doc_date is None and p.title == "정기 점검 결과"  # '결과'는 기관명('과')으로 오인하지 않는다


def test_body_date_is_not_a_document_date():
    p = EX.propose(None, ["시설 안전점검 안내\n수신: 각 부서장\n다음과 같이 점검을 실시함.\n점검 기간: 2026년 6월 15일부터 6월 19일까지\n"])
    assert p.title == "시설 안전점검 안내" and p.doc_date is None


def test_two_header_dates_not_chosen():
    p = EX.propose(None, ["가상 점검 공고문\n2026. 3. 5.\n2026. 4. 7.\n본문 내용"])
    assert p.doc_date is None and any("날짜" in r for r in p.review_reasons)


def test_nul_and_spacing_normalized():
    assert normalize("연\x00구\x00용역  착수회의   결과") == "연구용역 착수회의 결과"
    p = EX.propose(None, ["연\x00구\x00용역 착수회의 결과\n2026. 5. 20.\n일시: 가상"])
    assert p.title == "연구용역 착수회의 결과" and p.doc_type == "회의록" and p.doc_date == "2026-05-20"


def test_title_label_and_document_number_lines():
    p = EX.propose(None, ["예시시 가상과\n제목: 2026년 하반기 점검 계획\n일자 2026. 7. 1."])
    assert p.title == "2026년 하반기 점검 계획" and p.doc_date == "2026-07-01"
    p = EX.propose(None, ["문서번호 제2026-15호\n2026. 3. 5.\n시설 개선 사업 추진 계획\n1. 배경: 가상임."])
    assert p.title == "시설 개선 사업 추진 계획" and p.evidence_page == 1


def test_body_only_and_empty_pages_go_to_review():
    p = EX.propose(None, ["1. 추진 배경: 내용이 시작됨.\n2. 추진 목표: 내용임."])
    assert p.title is None and p.review_reasons and p.doc_date is None
    p = EX.propose(None, ["", "  \n"])
    assert p.title is None and p.review_reasons == ["OCR 텍스트가 없음"]


def test_short_first_line_with_second_candidate_is_ambiguous():
    p = EX.propose(None, ["가상기관\n2026. 3. 5.\n업무 협조 요청\n수신 각 부서"])
    assert p.title is None and any("기관명" in r for r in p.review_reasons)
    assert "가상기관" in p.evidence_snippet


def test_english_org_header_is_not_joined_with_title_line():
    p = EX.propose(None, ["Example Facilities Office\nAnnual Report\nMarch 5, 2026\nThis document is fictional."])
    assert p.title == "Annual Report" and not p.review_reasons


def test_multi_document_suspected_when_pages_have_different_titles():
    p = EX.propose(None, ["업무 협조 요청서\n2026. 3. 5.\n내용: 가상", "현장 방문 결과 보고\n2026. 3. 9.\n내용: 가상"])
    assert p.title is None and p.doc_date is None
    assert any("다중 문서" in r for r in p.review_reasons)


def test_running_header_across_pages_is_not_multi_document():
    pages = ["예시시 가상과\n시설 개선 사업 추진 계획\n2026. 3. 5.\n1. 배경: 가상.",
             "예시시 가상과\n2. 소요 예산: 가상의 금액임.\n표 내용"]
    p = EX.propose(None, pages)
    assert p.title == "시설 개선 사업 추진 계획" and not p.review_reasons


def test_snippet_limit_and_page():
    p = EX.propose(None, ["예시시 가상과\n" + "가" * 30 + " " + "나" * 70 + "\n2026. 3. 5."])
    assert p.evidence_page == 1 and len(p.evidence_snippet) <= 80


def test_never_uses_scan_date_and_does_not_log(caplog):
    caplog.set_level("DEBUG")
    p = EX.propose(Path("SCAN_20261105_090107_0001.pdf"), ["공고\n본문 내용만 있음\n"])
    assert p.doc_date is None  # 파일명의 스캔 시각을 문서 날짜로 쓰지 않는다
    assert "본문" not in caplog.text


# ---- 실제 OCR 연결 시험(환경이 있을 때만) ---------------------------------------------------------------------

def test_real_ocr_corpus_end_to_end(tmp_path_factory):
    """실제 코퍼스 → OcrmypdfEngine → RuleTitleExtractor. 자동 승인 후보 중 오답 비율을 측정한다(플랫폼별 OCR 차이를 허용)."""
    from scaninbox.ocr import OcrEnvironmentError, OcrmypdfEngine
    from scaninbox.contracts import OcrStatus
    from scaninbox_corpus.fonts import find_font
    from scaninbox_corpus.generate import generate
    from scaninbox_corpus.pathutil import extended
    tess = os.environ.get("SCANINBOX_TESSDATA")
    tdir = os.environ.get("SCANINBOX_TESSERACT_DIR")
    eng = OcrmypdfEngine(tessdata_dir=Path(tess) if tess else None, tesseract_dir=Path(tdir) if tdir else None)
    try:
        eng.check_environment()
        find_font()
    except (OcrEnvironmentError, FileNotFoundError) as e:
        pytest.skip(f"실제 OCR/폰트 환경 없음: {str(e)[:60]}")
    out = extended(tmp_path_factory.mktemp("c150") / "out")
    manifest = generate(out, dpi=150)
    accepted = wrong = 0
    for f in manifest["files"]:
        if f["expected_outcome"] == "fail":
            continue
        src = next(p for p in (Path(out) / "input").rglob("*") if p.is_file()
                   and unicodedata.normalize("NFC", p.relative_to(Path(out) / "input").as_posix()) == f["file"])
        r = eng.run(src, Path(out) / "ocr", timeout_s=300)
        if r.status not in (OcrStatus.OK, OcrStatus.ALREADY_TEXT, OcrStatus.PARTIAL):
            continue
        p = EX.propose(src, r.text_by_page)
        if p.title is not None and not p.review_reasons:
            accepted += 1
            wrong += _c(p.title) != _c(f["expected_title"])
    assert accepted >= 10
    assert wrong / accepted <= 0.1, (wrong, accepted)  # 오답 자동 승인 비율 10% 이하(macOS 측정은 0)
