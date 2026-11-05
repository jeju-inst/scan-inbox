"""합성 문서에 쓰는 한글 폰트 탐색.

사용 폰트는 재배포 제한이 없는 오픈 라이선스(SIL OFL) 폰트여야 한다.
기본 후보는 나눔바른고딕(Naver, SIL OFL 1.1). 폰트 파일 자체는 저장소에 넣지 않는다.
`SCANINBOX_FONT` 환경변수 또는 --font 로 TTF 경로를 지정할 수 있다.
reportlab 은 CFF 기반 OTF 를 임베드하지 못하므로 TrueType(.ttf)만 지원한다.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

FONT_FAMILY = "NanumBarunGothic"
FONT_LICENSE = "SIL Open Font License 1.1"

_CANDIDATES = [
    "/Library/Fonts/NanumBarunGothic.ttf",
    "/System/Library/Fonts/Supplemental/NanumBarunGothic.ttf",
    str(Path.home() / "Library/Fonts/NanumBarunGothic.ttf"),
    "/usr/share/fonts/truetype/nanum/NanumBarunGothic.ttf",
    "C:/Windows/Fonts/NanumBarunGothic.ttf",
]


def find_font(explicit: str | None = None) -> Path:
    paths = [explicit] if explicit else []
    env = os.environ.get("SCANINBOX_FONT")
    if env:
        paths.append(env)
    paths += _CANDIDATES
    for p in paths:
        if p and Path(p).is_file():
            return Path(p)
    raise FileNotFoundError(
        "한글 TTF 폰트를 찾지 못했습니다. 나눔바른고딕(OFL) 등을 설치하거나 "
        "SCANINBOX_FONT=<경로.ttf> 로 지정하세요."
    )


def font_info(path: Path) -> dict:
    """manifest 에 기록할 폰트 정보. 로컬 경로는 기록하지 않고 파일명·해시만 남긴다."""
    return {
        "family": FONT_FAMILY,
        "file": path.name,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "license": FONT_LICENSE,
    }
