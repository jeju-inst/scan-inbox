# G1 설계: 수동 CLI 수직 절편

작성일: 2026-11-05. 근거는 `docs/g0/decision.md`. 사용자 결정(2026-11-05): **신규 개발로 진행, 완료함은 최종본 보관처, OCR 필수.**

## 1. 목표와 범위

수동 CLI로 `보호(원본 보존) → 추출(OCR) → 제안 → 승인 → 배치 → 복구`를 끝까지 수행하고, 합성 코퍼스 하네스(`scaninbox-reconcile`·`scaninbox-snapshot`)로 PILOT 필수 조건을 자동 검증한다. 맥(Apple Silicon)과 Windows(x64 네이티브) 둘 다에서 돈다.

**포함**: 설정 검증, 발견·안정화 확인, 원본 보관소, OCR(필수)·쪽별 검증, 규칙 기반 제목·날짜 제안, 미리보기, 승인 후 배치, Undo, 재시작 복구, reconcile 호환 ledger 출력.
**제외(G2 이후)**: 트레이·검토 GUI, 폴더 감시·예약, 관련 폴더 이동·복사, LLM·구독 CLI 어댑터, 패키징·서명, 자동 승인.

## 2. 확정한 정책

- **완료함 = 최종본 보관처.** 이름·OCR이 적용된 최종본을 `done_dir`에 둔다. 원본 바이트는 별도 `backup_dir`에 먼저 보존한다(삭제·정리는 사용자 명시 동의 후에만).
- **OCR 필수.** OCR 결과가 `ok` 또는 `already_text`일 때만 완료 가능. `partial`은 검토 대상(`review_pending`), `encrypted`·`corrupt`·`failed`는 `held`/`failed`로 입력에 남긴다. OCR을 건너뛰고 완료하는 경로는 없다.
- **자동 완료 없음(G1).** 모든 건은 사용자가 승인해야 `completed`. `approved_by_user`를 ledger에 남긴다.
- 제목·날짜가 불명이거나 다중 문서 의심이면 `needs_review`로 표시하고 승인 시 사용자가 이름을 직접 지정해야 한다. 문서 날짜를 스캔일로 대체하지 않는다.
- 이름 규칙: `YYYY-MM-DD_제목__짧은ID.pdf`. 날짜 불명은 `날짜미상_제목__짧은ID.pdf`. 짧은 ID는 원본 SHA-256 앞 8자.
- 외부 전송 없음. LLM 호출 코드를 넣지 않는다. 로그에 OCR 본문을 남기지 않는다.

## 3. 모듈 구성과 책임

```
scaninbox/
  contracts.py   # 데이터·Protocol 계약 (이 PR 에서 고정)
  pathsafe.py    # [core] 파일명·경로 안전
  config.py      # [core] 프로파일 설정·검증
  journal.py     # [core] SQLite journal·상태 전이·복구
  ocr.py         # [engines] OCRmyPDF 래퍼·쪽별 검증
  title.py       # [engines] 규칙 기반 제목·날짜 제안
  pipeline.py    # [pipeline] 단계 조합: scan/plan/apply/undo/recover
  cli.py         # [pipeline] 서브커맨드, 사람이 읽는 출력, ledger 내보내기
```

### core
- `pathsafe`: Windows 금지문자·예약이름(CON, NUL 등)·후행 점·공백, 길이 제한(제목 부분 + 전체 경로, Windows `\\?\` 처리), NFC 키 비교로 NFC/NFD·대소문자 충돌 판정, 충돌 시 접미사, 루트 포함 검사(`..`·절대경로·symlink·junction 탈출 거절). 덮어쓰기는 항상 거절하고 새 이름을 만든다.
- `config`: 스캔 폴더·`done_dir`·`backup_dir`·앱 데이터(journal) 경로를 검증. 겹침·내부 포함·symlink 루트 탈출 거절. 기본 `done_dir`은 스캔 폴더의 형제 `scan_처리완료`, 설정 파일은 앱 데이터에 저장(`platformdirs`).
- `journal`: 소스 폴더 밖 SQLite. 파일별 상태 전이를 **전이표로 강제**(허용되지 않은 전이는 예외). 단계마다 영속화 후 다음 단계. `recover()`는 실제 파일·해시와 journal을 대조해 `publishing`·`source_cleanup_pending` 등 중간 상태를 복구하고 새 결과를 중복 생성하지 않는다. 단일 실행 잠금과 임대 만료.

### engines
- `ocr`: `OcrEngine` 구현. OCRmyPDF(17.13.0 고정) + Tesseract `kor+eng`를 **하위 프로세스(인자 배열, 셸 미사용)**로 호출, 시간·종료 처리(Windows는 프로세스 트리 종료). 기본 옵션은 `--deskew`를 쓰고 `--oversample 300`·`psm 6`은 쓰지 않는다(G0 결과). 출력 PDF에서 쪽별 텍스트를 다시 추출해 `empty_pages`를 계산, 쪽 수 보존 확인. `/Rotate` 쪽은 종료코드 0이어도 텍스트 0자이면 `partial`. 암호화·손상 사전 판별. kor 언어팩 존재 확인과 설치 안내 오류. 엔진·언어팩 버전 기록.
- `title`: `TitleExtractor` 구현. OCR 텍스트의 제목줄(큰 글씨 위치 정보 없이 첫 의미 줄·문서번호 줄 건너뛰기 등)과 날짜 패턴(한글 날짜 표기 `YYYY년 M월 D일` 포함)을 규칙으로 추출. 오인식 가능성(공백 소실·NUL 문자)을 정규화하고, 근거(쪽·발췌)를 함께 반환. 불확실하면 `title=None`과 `review_reasons`. 다중 문서 의심(쪽마다 제목·날짜 반복)은 검토 사유.

### pipeline / cli
- 단계: 발견(작성 안정 확인·PDF 재열기) → 등록(SHA-256·크기·mtime) → 보호(backup 복사·해시 확인) → 추출 → 제안(`PlanItem`) → 승인 → 확정(승인한 계획과 현재 입력 해시·타겟 상태 재검증, 임시 파일→검증→덮어쓰기 없는 최종 배치→기록 영속화) → 입력 정리 → 완료.
- CLI: `scaninbox init`, `scan`, `plan`(무변경 미리보기·JSON 출력 지원), `apply`(승인한 항목만, `--approve-all` 은 계획을 본 뒤 명시적으로), `hold`/`rename`(검토 항목 이름 지정), `undo`, `status`, `recover`, `export-ledger`(reconcile 호환 JSON).
- `plan`은 입력 폴더의 이름·해시·mtime을 바꾸지 않는다(`scaninbox-snapshot`으로 검증).
- Undo: 원본을 입력 경로에 충돌 없이 복구하고 해시 확인. 최종본이 이후 수정됐거나 원래 경로가 사용 중이면 타 파일을 지우지 않고 충돌로 멈춘다.

## 4. 검증

- 단위: 각 모듈 pytest. core는 Windows 규칙을 OS와 무관하게 시험할 수 있게 규칙 함수를 분리한다.
- 통합: 합성 코퍼스 23건으로 `scan → plan → apply → export-ledger → scaninbox-reconcile` 통과(위반 0). 기대 review·fail 건이 승인 없이 완료되지 않음.
- 장애 주입: 보호 후 중단, 배치 후 입력 정리 전 중단, journal commit 직전 중단, 중복 이벤트·재실행에서 중복 최종본·입력 손실 없이 복구.
- 크로스 플랫폼: 맥과 Windows 실기에서 코퍼스 e2e 한 번씩(Windows는 `\\?\`·NFC/NFD·대소문자 충돌 케이스 포함).
- PR마다 Codex 리뷰(pr-watch). 새 테스트 데이터는 합성 자료만.

## 5. 작업 분담과 병합 순서

모듈 경계가 곧 파일 경계이므로 서로 다른 파일만 수정한다. `contracts.py`와 `pyproject.toml`은 총괄만 수정하고, 바꿔야 하면 총괄에게 요청한다.

| 트랙 | 담당 | 파일 | 선행 |
|---|---|---|---|
| A core | worker-1 | `pathsafe.py` `config.py` `journal.py` + 테스트 | 없음 |
| B engines | worker-2 | `ocr.py` `title.py` + 테스트 | 없음 |
| C pipeline·cli | agy | `pipeline.py` `cli.py` + e2e 테스트 | A·B의 인터페이스(스텁으로 병행 개발, 머지 후 연결) |

머지 순서는 A·B → C → 통합 시험(맥·Windows) → G1 완료 보고. 워커는 머지하지 않고 PR만 올린다.
