# G1 Windows 실기 시험 결과 (core·engines·pipeline·CLI)

시험일: 2026-11-16. 대상: `main` ed9d52a(트랙 A·B·C 머지본). 시험자: claude/scan-inbox-worker-1. 합성 코퍼스(`scaninbox-corpus`, 23건)만 사용했고 실문서·실제 스캔 폴더는 열지 않았습니다. 외부 서비스 전송은 없고, 전역 설치·계정 로그인·레지스트리·PATH 변경도 하지 않았습니다(아래 환경 절 참조).

## 1. 요약

| 항목 | 결과 |
| --- | --- |
| `pytest -q -rs` 전체 | **216 passed, 1 failed, 0 skipped** (Mac 기준 217 passed, 0 skipped). 실제 OCR e2e(`test_pipeline_real_engine_e2e_corpus`)는 skip 되지 않고 통과 |
| 실패 1건 | `test_rotate_behavior_with_default_engine` — **Windows 파일시스템 이슈가 아니라 합성 코퍼스 글꼴·Tesseract 버전에 따른 회전 임계값 민감도**(3절) |
| CLI e2e 수동 1회 | `init → scan → plan → apply --approve-all → export-ledger → reconcile → undo` 모두 동작. **reconcile 위반 0**(완료함에 사전 파일을 시드한 경우), undo 후 입력 snapshot 완전 일치 |
| Windows 고유 이슈 | 이번 시험에서 **확인된 것 없음**(긴 한글 폴더·NFD 이름·공백 이름·`\\?\` 경로·사전 파일 충돌 모두 통과). 단 아래 4절의 관찰 3건은 Windows 와 무관한 동작·사용성 항목 |

## 2. 환경

- Windows 11 Home x64(빌드 26300), 비관리자 계정, SSH 원격 셸. `LongPathsEnabled=0`, `PYTHONUTF8=1`, 대소문자 구분 꺼짐. symlink 생성은 가능했습니다(관련 skip 없음).
- 작업 폴더는 짧은 경로(`<WORK>`)에 새로 만들었습니다. `origin/main` clone, `uv venv`(Python 3.12.10) + `pip install -e . pytest jsonschema`(전부 Windows 휠, 컴파일러·관리자 권한 불필요).
- OCRmyPDF 17.13.0, pikepdf 10.18.1, pypdfium2 5.15.2, pypdf 6.21.4, Pillow 13.0.0, reportlab 5.1.0, pytest 9.2.0.
- Tesseract 5.4.0.20240606(UB-Mannheim): 설치 프로그램을 **실행하지 않고** 7-Zip 으로 사용자 폴더에 풀어 `tesseract.exe` 를 직접 실행. `kor.traineddata` 는 tessdata_fast 태그 4.1.0(1,677,415 B, SHA-256 `6B85E11D…B3B2`, G0 때와 동일)을 별도로 받아 `tessdata` 에 두었고, `TESSDATA_PREFIX`·`PATH` 는 시험 셸의 자식 프로세스 환경에만 지정했습니다.
- 글꼴: 이 머신에는 나눔바른고딕이 없어 코퍼스 글꼴 탐색이 `NotoSansKR-Regular.ttf` 를 골랐습니다(Mac 은 나눔바른고딕). 이 차이가 3절의 원인입니다.

## 3. 실패 1건: 회전 쪽 임계값이 글꼴에 따라 달라짐

`tests/test_ocr.py::test_rotate_behavior_with_default_engine` 는 `rotated_blank`(빈 쪽 2·5, 이미지가 90도 돌아간 3쪽, PDF `/Rotate 90` 인 4쪽)에서 3·4쪽 모두 재현율 0.6 이상을 단정합니다. 기본 엔진(1.2 → 의심 쪽이 있으면 0.5 로 재시도 후 문제 쪽이 적은 결과 채택)이 최종적으로 **3쪽은 교정했지만 4쪽이 쓰레기 텍스트**(재현율 0.03)였습니다.

최소 재현: `OcrmypdfEngine(rotate_threshold=T, retry_rotate_thresholds=())` 로 같은 입력(dpi 150)을 임계값별로 실행한 쪽별 글자 재현율.

| 임계값 | NotoSansKR 로 렌더한 코퍼스 3쪽 / 4쪽 | 맑은 고딕(`SCANINBOX_FONT`) 3쪽 / 4쪽 |
| --- | --- | --- |
| 12, 1.2 | 0.03 / 0.92 | 0.06 / 1.00 |
| 1.0, 0.9 | **0.90 / 0.92** | (0.9) **0.83 / 1.00** |
| 0.8 | **0.90 / 0.92** | — |
| 0.7, 0.6, 0.5, 0.3 | 0.90 / **0.03** | (0.5) 0.83 / 1.00 |

- NotoSansKR 에서는 임계값 **0.8〜1.0 만** 두 쪽을 모두 살립니다. 현재 값(1.2 → 재시도 0.5)은 첫 실행에서 3쪽, 재시도에서 4쪽이 각각 깨져 **어느 쪽을 채택해도 한 쪽이 쓰레기**입니다. `ocr.py` 주석의 Windows 행(0.5 에서 두 쪽 모두 양호)은 맑은 고딕 코퍼스에서 잰 값이라 글꼴이 바뀌면 성립하지 않습니다.
- 맑은 고딕 코퍼스에서는 이 테스트도 다른 지점에서 실패했습니다. 첫 실행(1.2)의 3쪽 출력이 쓰레기 텍스트인데 `_has_suspect` 가 이를 의심 쪽으로 판정하지 못해 **재시도가 시작되지 않았고**(엔진 문자열 `rotate-threshold=1.2`, 3쪽 0.06), 단정 `>= 0.6` 이 깨졌습니다.
- 결론(보고만 하고 코드는 수정하지 않았습니다): 회전 임계값은 코퍼스 글꼴·Tesseract 버전에 민감한 합성 소표본 값이라 테스트가 환경별로 흔들립니다. 후보: (a) 재시도 목록에 0.9 를 추가, (b) 테스트 글꼴을 고정(`SCANINBOX_FONT`)하거나 환경별 단정 완화, (c) 쓰레기 텍스트 의심 판정 강화. 실문서 표본 재측정 전에는 단일 값으로 고정하지 않는 편이 안전합니다.

## 4. CLI e2e 수동 1회

방법: `scaninbox-corpus --dpi 150` 로 생성한 입력(23건: 한글 스캔, 서식, 영문, 혼합, 텍스트층, 회전·빈 쪽, 다중 문서, 하위 폴더 이름 충돌, NFD 한글 파일명, 공백·점 이름, 228자 긴 한글 폴더, 손상·암호화 각 1)을 격리 폴더(`SCANINBOX_HOME` 포함 모두 `<WORK>` 아래)에서 처리. 완료함은 코퍼스가 함께 만든 `done_preexisting`(사전 파일 1건, 기대 시나리오: 이름 충돌·기존 파일 보존)을 복사해 시드했습니다. 앱 데이터는 사용자 앱 데이터가 아닌 격리 폴더입니다.

| 단계 | 결과 |
| --- | --- |
| `scaninbox-snapshot create` | 파일 23·폴더 6 기록 |
| `scan` | 21건 발견·제안 생성(약 77초, OCR 포함). 승인가능 15 + 검토필요 6 |
| `plan` | 입력 무변경: `snapshot verify` 통과(이름·크기·해시·mtime 동일) |
| `apply --approve-all` | 15건 배치·입력 정리. 검토 필요 6건은 승인 없이 **적용 제외**(PILOT 요건 충족) |
| `export-ledger` → `scaninbox-reconcile` | `입력 등록 23 = 완료 15 + 보류 0 + 실패 0 + 미착수 8`, **대조 통과**(위반 0) |
| `apply --approve-all --force-review` | 검토 6건 추가 배치 → 완료 21, reconcile 통과(`미착수 2`) |
| `undo`(전체) | 21건 원본 복구·완료 파일 삭제. 입력 snapshot 과 **완전 일치**, 사전 파일은 그대로 |
| `recover` | 0건(멱등) |

관찰된 Windows 특이 사항: 228자 긴 한글 폴더 입력, NFD 로 만든 한글 파일명, 공백·점이 들어간 이름, 같은 이름(`scan.pdf`)이 두 하위 폴더에 있는 경우를 모두 처리했고 `\\?\` 확장 경로가 입력·완료함·백업에서 문제를 일으키지 않았습니다(`LongPathsEnabled=0` 상태). 완료함의 대소문자·NFC/NFD 충돌은 사전 파일과 이름이 겹치지 않는 제안 이름이라 충돌 접미사 경로까지는 이 코퍼스로 유발되지 않았습니다.

사용 중 발견한 비 Windows 항목(코드 수정 없이 보고):

1. **손상·암호화 PDF 는 첫 `scan` 에서 `waiting_stable` 로 남고, 안내가 불충분합니다(결함 아님, 안내 문제).** 위 e2e 는 `scan` 을 한 번만 돌렸고 그때 두 파일(코퍼스 `corrupt_pdf`·`encrypted_pdf`, 기대 결과 fail)의 mtime 이 `stable_seconds`(기본 10초)보다 새것이라 파싱 실패 파일이 안정화 대기에 머물렀습니다. 두 파일만 새 입력 폴더에 복사해 `scan` → 15초 대기 → `scan` 을 다시 돌리자 `status` 가 `failed` 1·`held` 1 로 분류됐고 이후 `scan` 은 변화가 없었습니다(설계대로). 남는 문제는 안내입니다: 첫 `scan` 은 "총 0건 발견"으로 끝나고, `plan` 은 "아직 스캔되지 않은 신규 파일 N건이 대기 중"이라고만 하며, `status` 는 `waiting_stable` 로만 보여 "몇 초 후 다시 `scan`" 이라는 안내가 없습니다. 첫 `scan` 에서 pypdf 의 `EOF marker not found` 가 그대로 stderr 로 나옵니다.
2. `apply --force-review` 는 단독으로 쓰면 "적용할 파일 목록을 지정하거나 `--approve-all` 을 사용하라"며 실패합니다(`--approve-all --force-review` 가 필요). 도움말에 명시가 없습니다.
3. 한글 제목 추출에 OCR 공백 오인이 보입니다(예: `2026 년 도 예시 사업 추 진계획`). 합성 렌더링·tessdata_fast 기준이라 일반화하지 않지만, 제안 이름에 그대로 반영됩니다.

## 5. 테스트 실행 메모

- 전체 pytest 약 270초(Mac 대비 느림, 실제 OCR 포함). 임시 폴더는 `--basetemp` 를 짧은 경로로 지정해 MAX_PATH 문제를 피했습니다.
- PowerShell 5.1 에서 한글 출력이 깨질 때는 파이프(`|`)·`>` 리다이렉트가 CP949 로 해석합니다. 로그는 `cmd /c "… >> 파일"` 로 바이트 그대로 저장한 뒤 UTF-8 로 읽어야 했습니다(앱 문제가 아니라 시험 셸 문제).
- 임시 파일 삭제 시 `PermissionError`, symlink 생성 권한 부족 skip 은 이번 실행에서 발생하지 않았습니다.

## 6. 미시험·한계

- 합성 코퍼스·tessdata_fast 기준이며 실문서 한글 OCR 품질은 여전히 미측정입니다.
- 대소문자만 다른 이름은 이 파일시스템 특성상 코퍼스가 생략(`case_only_collision`)했으므로 완료함 안에서의 대소문자 충돌은 단위 시험(`tests/test_pathsafe.py`)으로만 확인됩니다.
- 정식 설치 프로그램 설치·제거, 관리자 권한 설치, 다른 Windows 빌드는 미시험입니다.
- 시험 머신에 남긴 것: 작업 폴더의 clone·가상환경·Tesseract 압축 해제본(전역 영향 없음). e2e 산출물·pytest 임시 폴더·로그는 시험 후 삭제했습니다.

작성: 조남운 · claude/scan-inbox-worker-1 · 2026-11-16
