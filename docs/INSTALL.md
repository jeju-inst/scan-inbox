# 설치와 첫 실행 (G1 수동 CLI)

G1 단계는 **명령줄 도구**입니다. GUI·감시 실행·설치 프로그램은 G2 범위입니다. 외부 전송은 없고, 모든 처리는 이 PC 안에서 일어납니다.

## 1. 준비물

| 항목 | 버전 | 비고 |
| --- | --- | --- |
| Python | 3.11 이상 | 가상환경 사용 권장 |
| OCRmyPDF | 17.13.0 | `pip install -e .` 가 함께 설치합니다 |
| Tesseract | 5.4 이상 | macOS는 `brew install tesseract`, Windows는 공식 배포 설치 프로그램 또는 압축 해제 |
| 한국어 언어팩 `kor.traineddata` | tessdata_fast 4.1.0 | **Tesseract 에 포함되지 않습니다. 따로 받아야 합니다** |

`kor.traineddata` 가 없으면 OCR 엔진이 "언어팩 없음" 오류로 멈추고 **가짜 성공을 만들지 않습니다.** 영어만 있는 상태로는 한국어 문서를 처리할 수 없습니다.

## 2. 한국어 언어팩 설치 (시스템 폴더를 건드리지 않는 방법)

1. 사용자 폴더에 tessdata 폴더를 만들고 Tesseract 기본 `eng`·`osd`·`configs`·`tessconfigs` 를 복사합니다. 이 폴더가 `TESSDATA_PREFIX` 가 됩니다.
2. `https://github.com/tesseract-ocr/tessdata_fast/raw/4.1.0/kor.traineddata` 를 받아 같은 폴더에 둡니다. 이 파일의 SHA-256 은 `6b85e11d9bbf07863b97b3523b1b112844c43e713df8b66418a081fd1060b3b2` (1,677,415바이트)입니다.
3. `TESSDATA_PREFIX` 를 그 폴더로 지정한 뒤 `tesseract --list-langs` 에 `eng`·`kor`·`osd` 가 보이는지 확인합니다.

macOS 는 `brew install tesseract-lang` 으로도 설치할 수 있지만, 이는 시스템 전역 변경입니다.

## 3. 설치와 확인

```
python -m venv .venv
.venv/bin/pip install -e '.[dev]'         # Windows: .venv\Scripts\pip install -e ".[dev]"
TESSDATA_PREFIX=<위 폴더> .venv/bin/python -m pytest -q
```

이후 명령은 가상환경을 켜고 실행합니다. macOS·Linux 는 `source .venv/bin/activate`, Windows 는 `.venv\Scripts\activate` 입니다. 켜지 않으면 `scaninbox` 를 찾지 못하거나 다른 곳에 설치된 버전이 실행될 수 있습니다. 실행할 때마다 `TESSDATA_PREFIX` 도 같은 폴더로 지정해야 합니다.

한국어 팩이 연결되면 실제 OCR 코퍼스 e2e 까지 포함해 **skip 없이** 통과해야 합니다 (macOS 217개 통과, 0 skip 확인). 한국어 팩이 없으면 이 e2e 가 skip 됩니다.

## 4. 사용 순서

폴더 세 개를 정합니다. 서로 겹치거나 한쪽이 다른 쪽 안에 들어가면 안 됩니다.

- 입력 폴더(`--input-dir`): 스캔 PDF 가 들어오는 곳
- 완료 폴더(`--done-dir`): 승인된 **최종본** 보관처
- 백업 폴더(`--backup-dir`): 원본이 먼저 보관되는 곳

```
scaninbox -i <입력> -d <완료> -b <백업> scan            # 보호(백업) → OCR → 제목 제안
scaninbox -i <입력> -d <완료> -b <백업> plan            # 제안 확인 (입력 폴더 무변경)
scaninbox -i <입력> -d <완료> -b <백업> apply --approve-all   # 승인 후 최종본을 완료 폴더로
scaninbox -i <입력> -d <완료> -b <백업> undo [파일]     # 되돌리기
```

- 이름은 `YYYY-MM-DD_제목__짧은ID.pdf` 입니다. 날짜를 못 찾으면 `날짜미상_…` 입니다.
- OCR 이 성공한 문서만 완료됩니다. 암호화·손상·실패 문서는 입력 폴더에 그대로 남습니다.
- 검토가 필요한 항목은 `rename` 으로 이름을 정하거나 `apply --approve-all --force-review` 로 명시 승인해야 합니다.
- 중단되거나 이상하면 `recover` 를 먼저 실행하세요. 같은 작업을 동시에 두 번 실행하지 마세요(잠금으로 막힙니다).
- 저널(SQLite)에는 입력 폴더 기준 상대경로(원래 파일명 포함), 해시, 상태, 제안 제목이 저장됩니다. **파일명이 민감하면 저널·백업 폴더도 같은 수준으로 보호하세요.** OCR 본문과 본문 발췌는 저널·로그에 저장하지 않습니다.

## 5. (선택) 로컬 LLM 제목 제안

기본 제목 엔진은 규칙입니다. 실제 스캔에서는 OCR 이 제목을 깨뜨리는 일이 잦아, 사용자 자신의 머신에서 도는
[Ollama](https://ollama.com) 모델로 제목을 다시 묻는 선택 기능을 둡니다.

- 켜는 법: `scaninbox --title-engine ollama [--ollama-url http://<내 머신>:11434] [--ollama-model qwen3:30b-a3b] scan`
  (환경변수 `SCANINBOX_TITLE_ENGINE`·`SCANINBOX_OLLAMA_URL`·`SCANINBOX_OLLAMA_MODEL` 도 가능)
- 규칙이 검토 사유 없이 제목을 낸 문서는 LLM 을 부르지 않습니다.
- 그 밖의 문서는 **첫 쪽 OCR 텍스트(최대 1,500자)가 지정한 서버로 전송**됩니다. 서버는 반드시 본인 머신이어야 하며,
  외부 서비스 주소를 넣지 마십시오.
- LLM 답은 원문 대조(제목 글자 60% 이상이 원문에 있음, 날짜는 원문에 적힌 것만)를 통과해야 쓰이고,
  통과한 제목에도 "로컬 LLM 이 제안한 제목" 검토 사유가 붙습니다. 서버가 없거나 응답이 이상하면 규칙 결과로 돌아갑니다.
- 다른 머신의 Ollama 를 쓰려면 그 머신에서 Ollama 가 외부 접속을 받도록 설정하거나 SSH 터널을 씁니다.

## 6. 알려진 한계 (G1)

- 한국어 OCR 품질과 제목 제안 정확도는 **합성 문서로만** 측정했습니다. 실제 문서 성능은 G3 파일럿에서 확인합니다.
- 회전된 쪽의 인식은 Tesseract 버전과 글꼴에 따라 달라질 수 있습니다. 이상한 글자가 많은 쪽은 검토 대상으로 표시됩니다.
- LLM 제목 제안은 G1 에 없습니다.
