# OSS 비교와 채택 게이트

조사일: 2026-11-04. 공식 GitHub README·문서 소스·라이선스와 릴리스 메타데이터를 확인했습니다. **설치·실행·한국어 인식률·배포 패키지 품질은 아직 시험하지 않았습니다.** main 문서는 최신 정식 릴리스보다 앞설 수 있으므로 파일럿에서 버전·커밋을 고정해야 합니다. 아래 적합성 판단은 문서 기반 추론입니다.

## 1. Paperless-ngx: 완성형 문서관리 우선 후보

- 확인된 기능: consumption 폴더 감시, OCR·검색, 원본과 archive 파일 보관, 문서 제목·분류·storage path, 이름 템플릿. 처리된 입력은 consumption 폴더에서 제거됩니다. 현재 main 문서에는 AI 기반 제목·분류 등 제안도 기술되어 있습니다.
- 장점: 입력 폴더를 비우고 검색 가능한 문서 보관소를 만드는 전체 흐름이 이미 있습니다. 별도 앱을 새로 만들기 전에 가장 먼저 비교할 대상입니다.
- 차이: 자체 DB·media 보관 구조를 관리하는 서비스입니다. 일반 업무 폴더를 마음대로 이동·수정하는 데스크톱 도구와는 다릅니다. 관리 파일을 탐색기에서 임의 이동하는 운영을 기본 전제로 하지 않습니다.
- 설치: 공식 문서는 Docker를 우선 권장하며 bare-metal은 Linux만 지원한다고 명시합니다. Windows·macOS에서 웹 UI를 쓰는 것과 Windows·macOS 네이티브 앱인 것은 다릅니다.
- 시험 시 주의: consumption에 실제 원본을 바로 넣지 않습니다. 합성 문서 또는 승인된 복사본만 사용하고, 제목·storage path 설정으로 어느 정도 충족되는지와 별도 업무 폴더 내보내기의 추가 작업을 확인합니다.
- 라이선스: GitHub 메타데이터 GPL-3.0. 확인한 최신 정식 릴리스 메타데이터는 v3.4.2, 2026-11-03 배포입니다. 기능 기술은 main 문서 기준이며 이 릴리스에서 모두 검증했다는 의미가 아닙니다.
- 출처: [저장소](https://github.com/paperless-ngx/paperless-ngx), [입력 처리·원본 보존](https://github.com/paperless-ngx/paperless-ngx/blob/main/docs/usage.md), [분류·이름·storage path](https://github.com/paperless-ngx/paperless-ngx/blob/main/docs/advanced_usage.md), [설치](https://github.com/paperless-ngx/paperless-ngx/blob/main/docs/setup.md), [라이선스](https://github.com/paperless-ngx/paperless-ngx/blob/main/LICENSE), [릴리스](https://github.com/paperless-ngx/paperless-ngx/releases)

## 2. AI File Sorter: 데스크톱 UX 우선 후보

- 공식 README상 Windows·macOS·Linux, 로컬/원격 모델, 문서 내용 기반 이름 제안, 분류 후보 제한, 검토·dry-run, persistent undo를 제공합니다. 한국어 인터페이스도 명시되어 있습니다.
- 장점: 기존 폴더와 GUI를 유지하려는 요구에 Paperless보다 직접적입니다. 이름·폴더 추천·승인·Undo를 새로 구현할 필요가 있는지 먼저 시험할 가치가 있습니다.
- 미확인: 스캔 이미지 전용 PDF의 한국어 내용 인식, 검색 가능한 PDF 텍스트층 생성, 폴더 감시·예약, 입력 안정성 감지, 별도 원본 보존과 완료함 정책. 이미지 시각 분석이나 PDF 텍스트 추출이 있다는 사실만으로 PDF OCR까지 된다고 간주하지 않습니다.
- 배포 성숙도: 조사 당시 최신 GitHub release 항목은 v1.9.2이며 assets 목록은 비어 있었습니다. README에는 별도 배포·플랫폼 빌드 안내가 있으나 실제 다운로드·설치 시험은 안 했습니다. macOS 현재 main 문서는 Apple Silicon/macOS 15+를 명시합니다.
- 라이선스: AGPL-3.0. 코드 재사용·포크·네트워크 제공은 해당 의무를 검토해야 하며 MIT 앱 안으로 무심코 복사하지 않습니다.
- 출처: [공식 README](https://github.com/hyperfield/ai-file-sorter), [라이선스](https://github.com/hyperfield/ai-file-sorter/blob/main/LICENSE), [릴리스](https://github.com/hyperfield/ai-file-sorter/releases)

## 3. organize: 정해진 규칙이 많은 경우

- 공식 README상 Windows·macOS·Linux, 본문 추출 기반 규칙, rename/move/copy, 충돌 처리, simulate를 제공합니다. MIT입니다.
- 장점: 제목/기관/유형이 일정한 서식에 규칙을 쓰면 적은 개발로 처리할 수 있습니다. 일반 사용자가 설정할 검토 GUI, 내용 제목 추론, OCR, 트레이 앱은 별도 검증·보완이 필요합니다.
- 판단: PoC용 조합 후보입니다. OCR 명령 연결만으로 트랜잭션·원본 보존·Undo까지 해결된 것으로 간주하지 않습니다. 설정의 Python/shell 실행은 신뢰한 작성자에게만 허용해야 합니다.
- 출처: [README·라이선스](https://github.com/tfeldmann/organize)

## 4. OCRmyPDF: 앱이 아니라 OCR 엔진 부품

- PDF에 OCR 텍스트층을 추가하는 엔진으로 시험합니다. 제목 결정·관련 폴더 분류·감시·완료함 UI는 별도입니다. 프로젝트 라이선스는 MPL-2.0입니다.
- 공식 main 설치 문서는 Windows 네이티브와 macOS 설치를 모두 다룹니다. **Windows는 반드시 WSL이어야 한다는 가정은 하지 않습니다.**
- main 문서의 최근 설치 안내에는 PDFium 대체 래스터화와 선택적 Ghostscript 설명이 있습니다. 실제 정식 버전과 의존성, PDF/A 필요 여부를 고정한 뒤 확인해야 합니다. OCRmyPDF 라이선스만 보고 Ghostscript 등 외부 바이너리의 재배포 의무를 판단하지 않습니다.
- 한국어 언어팩, 회전·저해상도·혼합 페이지 품질은 별도로 시험합니다.
- 출처: [저장소](https://github.com/ocrmypdf/OCRmyPDF), [공식 설치 문서 소스](https://github.com/ocrmypdf/OCRmyPDF/blob/main/docs/installation.md), [라이선스](https://github.com/ocrmypdf/OCRmyPDF/blob/main/LICENSE)

## 결론: OSS부터 시험하고, 부족한 경계만 만든다

- 자체 검색 보관소를 받아들일 수 있으면 **Paperless-ngx 채택**을 우선합니다. 해당 구성으로 입력·보존·이름·분류 요구를 만족하면 새 문서관리 앱 개발은 하지 않습니다.
- 기존 폴더 중심 UX가 필수이면 **AI File Sorter + 로컬 OCR의 적합성 시험**을 합니다. 한글 스캔 처리와 완료함 정책만 보완하면 되는지 확인합니다.
- 고정 서식 중심이면 **organize + OCR**를 더 작은 PoC로 시험합니다.
- 세 경로 모두에서 설치 부담, 완료 상태·복구, 기존 폴더 연계가 과하면 로컬 우선 경량 조율 앱을 개발합니다. OCR·PDF 파서·모델 추론은 검증된 부품을 사용하고 새로 만들지 않습니다.

### 신규 개발 착수 조건

1. 합성 테스트와 승인된 대표 복사본으로 동일한 이름·OCR·최종 위치·복구 시나리오를 비교합니다.
2. Windows 네이티브 사용 경험과 macOS 설치를 각각 확인합니다. 개발환경 실행만으로 배포 가능하다고 판단하지 않습니다.
3. 원본 보존, 입력 정리, 충돌 회피, 재시작·Undo 중 하나라도 필수 기준을 충족하지 못하면 설정으로 해결 가능한지 먼저 확인합니다.
4. 결과를 기능별 `충족 / 설정 필요 / 확장 필요 / 불가 / 미시험`으로 기록하고, 유지관리 비용과 함께 사용자가 채택 방향을 결정합니다.
