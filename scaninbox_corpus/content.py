"""완전 가상 문서 내용. 실존 기관·인물·번호와 무관하며 모든 값은 임의로 지어낸 것이다.

문서 정의: dict(id, title, type, date, lang, pages)
  title : 문서 본문에 실제로 인쇄되는 제목. 제목 없는 서식은 None.
  type  : 기대 문서 유형(자유 라벨)
  date  : 문서에 인쇄된 문서 날짜(YYYY-MM-DD) 또는 None(날짜 없음·애매)
  pages : 페이지별 요소 목록(layout.py 참조)
"""
from __future__ import annotations

ORG = "예시시 가상과"
ORG_EN = "Example Facilities Office"


def _p(*parts: str) -> str:
    return " ".join(parts)


def doc_plan() -> dict:
    t = "2026년도 예시 사업 추진계획"
    return dict(id="plan", title=t, type="계획서", date="2026-03-12", lang="ko", pages=[
        [("org", ORG), ("meta", "문서번호 가상-0001 / 2026. 3. 12."), ("title", t),
         ("para", "1. 추진 배경: 가상의 시설 운영 효율을 높이기 위하여 연간 사업 일정을 정리함."),
         ("para", "2. 추진 목표: 점검 주기를 분기별로 표준화하고 결과를 한 곳에 모아 관리함."),
         ("para", _p("3. 추진 방향: 담당 부서는 월별 일정표를 작성하고, 필요한 경우 외부 전문가의",
                     "의견을 구하되 모든 자료는 예시용 가상 데이터를 사용함.")),
         ("table", [["구분", "내용", "시기"], ["1분기", "현황 조사", "1-3월"], ["2분기", "시범 운영", "4-6월"]]),
         ("footer", "- 1 -")],
        [("org", ORG), ("para", "4. 소요 예산: 본 계획의 예산은 가상의 금액이며 실제 집행과 무관함."),
         ("table", [["항목", "금액(가상)"], ["운영비", "000,000"], ["점검비", "000,000"]]),
         ("para", "5. 기대 효과: 업무 절차가 단순해지고 기록의 일관성이 높아질 것으로 기대됨."),
         ("stamp", "검토"), ("footer", "- 2 -")],
    ])


def doc_meeting(title: str = "연구용역 착수회의 결과", date: str = "2026-05-20", seed_text: str = "착수") -> dict:
    y, m, d = date.split("-")
    return dict(id="meeting", title=title, type="회의록", date=date, lang="ko", pages=[
        [("org", ORG), ("meta", f"{y}. {int(m)}. {int(d)}."), ("title", title),
         ("para", f"일시: {y}년 {int(m)}월 {int(d)}일 오후 2시 / 장소: 가상 회의실 A"),
         ("para", "참석자: 담당자 가, 담당자 나, 담당자 다 (모두 가상 인물)"),
         ("para", f"안건 1. {seed_text} 일정 확인 - 세부 일정은 별도 공유하기로 함."),
         ("para", "안건 2. 역할 분담 - 자료 정리는 담당자 가, 일정 관리는 담당자 나가 맡기로 함."),
         ("para", "결정 사항: 다음 회의는 2주 뒤에 같은 장소에서 열기로 함."),
         ("stamp", "확인"), ("footer", "- 1 -")],
    ])


def doc_purchase() -> dict:
    t = "물품 구매 요청서"
    return dict(id="purchase", title=t, type="서식", date="2026-11-03", lang="ko", pages=[
        [("org", ORG), ("meta", "작성일 2026. 11. 3."), ("title", t),
         ("table", [["품명", "수량", "비고"], ["가상 사무용품 세트", "2", "예시"], ["가상 라벨지", "5", "예시"]]),
         ("para", "위 물품의 구매를 요청합니다. 금액과 업체는 모두 가상입니다."),
         ("stamp", "결재")],
    ])


def doc_notice() -> dict:
    t = "시설 안전점검 안내"
    return dict(id="notice", title=t, type="공문", date="2026-06-02", lang="ko", pages=[
        [("org", ORG), ("meta", "문서번호 가상-0042 / 2026. 6. 2."), ("title", t),
         ("para", "수신: 각 부서장 (가상)"),
         ("para", "다음과 같이 정기 안전점검을 실시하오니 업무에 참고하시기 바랍니다."),
         ("para", "점검 기간: 2026년 6월 15일부터 6월 19일까지 / 점검 대상: 가상 건물 1동"),
         ("para", "문의: 가상과 대표번호 000-0000-0000"),
         ("stamp", "발신"), ("footer", "- 1 -")],
    ])


def doc_untitled_form(variant: int = 1) -> dict:
    """제목이 인쇄되지 않은 서식. 날짜는 없거나 애매하다."""
    if variant == 1:
        rows = [["성명", "", "소속", ""], ["기간", "", "사유", ""], ["연락처", "000-0000-0000", "확인", ""]]
        extra = [("para", "위와 같이 신청합니다.")]
        date = None
    else:
        rows = [["구분", "내용", "수량", "비고"], ["", "", "", ""], ["", "", "", ""], ["", "", "", ""]]
        extra = [("para", "작성일: 26.  .  (미기재)")]
        date = None
    return dict(id=f"untitled{variant}", title=None, type="서식(제목없음)", date=date, lang="ko", pages=[
        [("org", ORG), ("table", rows), *extra, ("stamp", "확인")],
    ])


def doc_english_summary() -> dict:
    t = "Quarterly Facilities Maintenance Summary"
    return dict(id="en_summary", title=t, type="report", date="2026-04-15", lang="en", pages=[
        [("org", ORG_EN), ("meta", "Ref. EX-0007 / April 15, 2026"), ("title", t),
         ("para", "This summary covers routine maintenance activities for the fictional East Building."),
         ("para", "All inspections were completed on schedule. No critical issues were recorded."),
         ("table", [["Area", "Status", "Note"], ["Lobby", "Done", "Sample"], ["Roof", "Done", "Sample"]]),
         ("para", "Next review is planned for the following quarter. This text is entirely synthetic."),
         ("footer", "- 1 -")],
    ])


def doc_english_notice() -> dict:
    t = "Notice of Equipment Delivery Schedule"
    return dict(id="en_notice", title=t, type="notice", date="2026-07-08", lang="en", pages=[
        [("org", ORG_EN), ("meta", "July 8, 2026"), ("title", t),
         ("para", "Dear colleagues, the delivery of the sample equipment is scheduled for next Tuesday."),
         ("para", "Please keep the loading area clear between 9:00 and 11:00. Thank you for your cooperation."),
         ("stamp", "Sent")],
    ])


def doc_mixed(n: int = 1) -> dict:
    if n == 1:
        t = "분기별 시설 점검 요약 Quarterly Inspection Summary"
        date = "2026-04-15"
        body = [("para", "이번 분기 점검 결과를 요약함. The inspections were completed without critical issues."),
                ("table", [["Area 구역", "Status 상태"], ["Lobby 로비", "OK 양호"], ["Roof 옥상", "OK 양호"]])]
    else:
        t = "Project Kickoff 착수 보고"
        date = "2026-08-03"
        body = [("para", "Kickoff meeting 결과: 일정과 역할을 확인함. Action items are listed below."),
                ("table", [["Item 항목", "Owner 담당"], ["Schedule 일정", "Team A 가"], ["Budget 예산", "Team B 나"]])]
    return dict(id=f"mixed{n}", title=t, type="보고(한영혼합)", date=date, lang="ko+en", pages=[
        [("org", ORG), ("meta", date.replace("-", ". ")), ("title", t), *body, ("footer", "- 1 -")],
    ])


def doc_budget_text() -> dict:
    t = "2026년 예산 집행 현황 보고"
    return dict(id="budget", title=t, type="보고서", date="2026-09-01", lang="ko", pages=[
        [("org", ORG), ("meta", "2026. 9. 1."), ("title", t),
         ("para", "상반기 예산 집행률을 정리함. 모든 수치는 가상이며 실제 예산과 무관함."),
         ("table", [["구분", "계획", "집행"], ["운영", "000", "000"], ["사업", "000", "000"]]),
         ("footer", "- 1 -")],
        [("org", ORG), ("para", "하반기 집행 계획: 분기별 점검 결과를 반영하여 조정하기로 함."),
         ("para", "특이사항 없음. 이 문서는 텍스트층이 있는 PDF 시험용 합성 자료임."), ("footer", "- 2 -")],
    ])


def doc_english_text() -> dict:
    t = "Annual Training Schedule"
    return dict(id="en_text", title=t, type="schedule", date="2026-01-10", lang="en", pages=[
        [("org", ORG_EN), ("meta", "January 10, 2026"), ("title", t),
         ("para", "The following sessions are planned for the fictional staff training program."),
         ("table", [["Month", "Session"], ["March", "Safety basics"], ["September", "Records handling"]]),
         ("footer", "- 1 -")],
    ])


def doc_outsourcing() -> dict:
    t = "위탁 관리 운영 결과 보고"
    return dict(id="outsourcing", title=t, type="보고서", date="2026-10-05", lang="ko", pages=[
        [("org", ORG), ("meta", "2026. 10. 5."), ("title", t),
         ("para", "위탁 운영 결과를 보고함. 본 문서는 가상이며 첫 쪽과 셋째 쪽은 텍스트층이 있고 나머지는 스캔임."),
         ("footer", "- 1 -")],
        [("org", ORG), ("para", "[붙임 1] 운영 일지 사본 (스캔 페이지) - 가상 기록: 점검 이상 없음."),
         ("table", [["일자", "내용"], ["10/1", "점검"], ["10/2", "정리"]]), ("footer", "- 2 -")],
        [("org", ORG), ("para", "종합 의견: 다음 해에도 같은 방식으로 운영하는 것이 적절하다고 판단됨."),
         ("footer", "- 3 -")],
        [("org", ORG), ("para", "[붙임 2] 확인서 사본 (스캔 페이지) - 가상 서명란."),
         ("table", [["확인자", "서명"], ["담당자 가", ""]]), ("footer", "- 4 -")],
    ])


def doc_rotation_case() -> dict:
    t = "현장 점검 결과 보고"
    return dict(id="rotcase", title=t, type="보고서", date="2026-02-17", lang="ko", pages=[
        [("org", ORG), ("meta", "2026. 2. 17."), ("title", t),
         ("para", "현장 점검 결과를 정리함. 2쪽은 빈 페이지, 3쪽은 옆으로 스캔된 쪽, 4쪽은 PDF 회전 속성이 있는 쪽임."),
         ("footer", "- 1 -")],
        None,  # 빈 페이지
        [("org", ORG), ("para", "[옆으로 스캔된 쪽] 이 페이지는 가로 방향 종이로 스캔된 것처럼 이미지 자체가 90도 회전되어 있음."),
         ("para", "OCR 은 방향을 감지하여 올바르게 읽어야 함."), ("footer", "- 3 -")],
        [("org", ORG), ("para", "[회전 속성 쪽] 이미지는 바르게 서 있으나 PDF 쪽 회전 속성이 90도로 지정되어 있음."),
         ("footer", "- 4 -")],
        None,  # 끝 빈 페이지
    ])


def doc_bundle() -> list[dict]:
    """한 PDF 에 합쳐 스캔된 서로 다른 문서 3건."""
    a = dict(id="b_trip", title="출장 결과 보고", type="보고서", date="2026-04-22", lang="ko", pages=[
        [("org", ORG), ("meta", "2026. 4. 22."), ("title", "출장 결과 보고"),
         ("para", "가상 출장 결과를 보고함. 방문처와 인원은 모두 가상임."), ("footer", "- 1 -")],
        [("org", ORG), ("para", "출장 중 확인한 사항과 후속 조치 계획을 정리함."), ("footer", "- 2 -")],
    ])
    b = dict(id="b_req", title="자료 열람 신청서", type="서식", date="2026-04-23", lang="ko", pages=[
        [("org", ORG), ("meta", "신청일 2026. 4. 23."), ("title", "자료 열람 신청서"),
         ("table", [["신청인", "담당자 가", "목적", "내부 참고"]]), ("stamp", "접수")],
    ])
    c = dict(id="b_eng", title="Visitor Access Guide", type="guide", date="2026-04-24", lang="en", pages=[
        [("org", ORG_EN), ("meta", "April 24, 2026"), ("title", "Visitor Access Guide"),
         ("para", "Visitors must sign in at the front desk. This guide is fictional."), ("footer", "- 1 -")],
        [("org", ORG_EN), ("para", "Badges are returned at the end of each visit."), ("footer", "- 2 -")],
    ])
    return [a, b, c]
