# 합성 코퍼스 생성기와 인수 검증 하네스

[PILOT.md](../docs/PILOT.md) 1단계(합성 문서)와 5단계(건수 대조)를 프로그램으로 돌리기 위한 도구입니다. **모든 문서는 완전 가상**이며 실문서·실경로·개인정보를 쓰지 않습니다. 생성물(PDF·manifest)은 `.gitignore` 대상 디렉터리(`corpus_out/`)에만 만들고 저장소에는 생성기·스키마·시험만 둡니다.

## 설치와 생성

```sh
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m scaninbox_corpus.generate --out corpus_out --force   # 약 5초, 23개 PDF
.venv/bin/python -m pytest -q
```

- 의존성은 `pyproject.toml` 에 정확한 버전으로 고정합니다(Pillow, reportlab, pypdf; 시험용 pytest, jsonschema). 검증 환경: Python 3.14, macOS arm64.
- 같은 `--seed`·폰트·라이브러리 버전이면 바이트 단위로 같은 파일이 나옵니다(결정적). 기본 seed 20261105.
- **폰트**: 나눔바른고딕(NanumBarunGothic.ttf, Naver, SIL Open Font License 1.1) → 나눔고딕 → Noto Sans KR 순으로 macOS·Linux·Windows(`%WINDIR%\Fonts`, 사용자 `%LOCALAPPDATA%\Microsoft\Windows\Fonts`) 폴더에서 찾고, OFL 폰트가 없으면 마지막 수단으로 맑은 고딕(`malgun.ttf`, 비OFL)을 경고와 함께 씁니다. 이때 manifest 의 라이선스 필드는 OFL 이 아니라 `NOT-OFL: …` 로 기록되며 그 생성물은 공개 저장소에 올리면 안 됩니다(`--font`·`SCANINBOX_FONT` 로 지정한 임의 폰트도 같음). 폰트 파일은 저장소에 넣지 않으며 manifest 에는 파일명·SHA-256·라이선스만 기록합니다(경로 미기록). 다른 TTF 는 `--font` 또는 `SCANINBOX_FONT` 로 지정합니다(reportlab 이 CFF 기반 OTF 를 임베드하지 못해 TrueType 만 지원). 이 폰트의 OFL 은 배포처의 공식 고지에 근거한 기록이며, 시스템 설치본 파일 안에서 라이선스 문자열을 직접 확인하지는 못했습니다.
- 스캔 느낌은 150dpi 래스터화 + 기울기(±0.6도) + 종이 노이즈 + 약한 흐림 + 먼지 점 + JPEG(품질 72)입니다. 실제 스캐너 품질을 대표하지 않습니다.

## 생성되는 것

`corpus_out/input/` 은 스캔 폴더를 모사한 입력, `corpus_out/done_preexisting/` 는 완료 폴더에 미리 있는 파일입니다.

| category | 내용 | 기대 결과 |
|---|---|---|
| titled_scan_ko | 본문 제목이 있는 이미지 전용 한글 스캔 4건(계획서 2쪽, 회의록, 서식, 공문) | complete |
| untitled_form | 제목이 인쇄되지 않은 서식 2건. 날짜 없음·미기재 | review (제목·날짜를 지어내면 안 됨) |
| english_scan / mixed_lang_scan | 영문 2건, 한영 혼합 제목 2건 | complete |
| text_layer_pdf | 텍스트층 있는 PDF(한글·영문) | complete (OCR 불필요) |
| mixed_pdf | 1·3쪽 텍스트층 + 2·4쪽 스캔 | complete (필요한 쪽만 OCR) |
| rotated_blank | 빈 쪽 2개, 이미지 90도 회전 쪽, PDF /Rotate 90 쪽 | complete (쪽 수 5 보존) |
| multi_doc_bundle | 한 PDF 에 서로 다른 문서 3건(2+1+2쪽) | review |
| name_collision | 제안 제목·날짜가 같은 2건, 하위 폴더별 같은 파일명 2건, NFD 파일명, 긴 한글 경로, 완료 폴더에 이미 있는 이름 | complete (덮어쓰기 금지) |
| corrupt_pdf / encrypted_pdf | 끝이 잘린 PDF, 열기 암호 PDF | fail (성공으로 위장 금지) |

대소문자만 다른 파일명 쌍은 대소문자 구분 파일시스템에서만 만들어지고, 기본 macOS·Windows 볼륨에서는 `manifest.skipped` 에 이유와 함께 기록됩니다.

## 정답 manifest

`corpus_out/manifest.json` 은 [schema/manifest.schema.json](schema/manifest.schema.json) 을 따릅니다. 파일별로 기대 제목·유형·문서 날짜·언어·기대 결과(`complete|review|fail`)·페이지 수·SHA-256·크기와 페이지별 정보(텍스트층 유무, OCR 필요, 물리 회전, PDF 회전)를 담습니다. 묶음 파일은 문서별 제목·쪽 범위를 `documents` 에 둡니다. 파일 키는 `input/` 기준 상대 경로이며 유니코드 NFC 입니다.

## 인수 검증 스크립트

**입력 등록 = 완료 + 보류 + 실패 + 미착수** 대조:

```sh
.venv/bin/python -m scaninbox_corpus.reconcile --manifest corpus_out/manifest.json \
  --ledger run_ledger.json --input-dir corpus_out/input --done-dir <완료 폴더>
```

시험 대상 도구가 남긴 기록(ledger, [schema/ledger.schema.json](schema/ledger.schema.json))을 정답·실제 폴더 상태와 맞춥니다. ledger 에 없는 입력은 미착수로 셉니다. 다음을 위반으로 보고하고 종료 코드 1을 냅니다: 건수 불일치·중복·미등록 처리, 완료인데 입력에 남음, 완료가 아닌데 입력 소실·변경, 원본 해시 불일치, 최종본 없음·해시 불일치·경로 중복, 완료 폴더의 기존 파일 덮어쓰기·소실, 보류·실패의 사유 누락, 처리 불가 파일을 완료로 위장.

**입력 무변경**(이름·크기·SHA-256·mtime) 검증:

```sh
.venv/bin/python -m scaninbox_corpus.snapshot create corpus_out/input -o before.json
#  ... 미리보기 등 시험 ...
.venv/bin/python -m scaninbox_corpus.snapshot verify corpus_out/input before.json
```

삭제·추가·이름 변경(NFC/NFD 변경 구분)·내용 수정·mtime 변경·폴더 증감을 보고하고 변경이 있으면 종료 코드 1입니다. 심볼릭 링크는 따라가지 않고 대상 문자열만 비교합니다.

## Windows 에서

- 임시 폴더 접두와 합친 "긴 한글 경로"(228자)가 `MAX_PATH`(260자)를 넘어 일반 경로로는 `mkdir`·`copytree` 가 실패할 수 있습니다. 생성기·`snapshot`·`reconcile` 은 루트 경로를 `\\?\` 확장 경로로 바꿔 쓰고(Windows 에서만), 시험도 같은 방식으로 임시 루트를 만듭니다. 이 처리는 macOS 에서 문자열 변환만 시험했으며 **Windows 실기 재검증이 필요합니다.**
- `snapshot` 은 링크 대상 문자열의 `\\?\` 접두를 떼고 기록·비교합니다.

## reconcile 이 잡는 기대 결과 위반

- 기대 결과 `fail`(암호화·손상 PDF)이 `completed` 로 기록되면 `[실패 위장]`(최종본 기록이 부족해도 잡음).
- 기대 결과 `review`(제목 없는 서식, 다중 문서 묶음)가 ledger 의 `approved_by_user: true` 없이 `completed` 로 기록되면 `[검토 누락]`. 사용자가 검토·승인한 뒤 완료하는 흐름은 `approved_by_user: true` 를 남겨 통과시킵니다.

## 한계

- 합성 페이지는 단순한 한 가지 글꼴·표 레이아웃입니다. 손글씨·도장 겹침·저해상도·번짐 등 실스캔 품질은 재현하지 않습니다. OCR 품질 결론은 실문서 대표 복사본 시험(PILOT.md 3단계)이 별도로 필요합니다.
- 장애 주입(강제 종료·디스크 부족·잠긴 파일 등)은 이 코퍼스 범위 밖입니다. 손상·암호화 PDF 와 NFC/NFD·긴 경로 입력만 제공합니다.
