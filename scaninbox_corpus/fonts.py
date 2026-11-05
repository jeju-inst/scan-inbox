"""합성 문서에 쓰는 한글 폰트 탐색.

기본 후보는 재배포 제한이 없는 오픈 라이선스(SIL OFL) 폰트다(나눔바른고딕·나눔고딕·Noto Sans KR). 폰트 파일 자체는
저장소에 넣지 않는다. `SCANINBOX_FONT` 환경변수 또는 --font 로 TTF 경로를 지정할 수 있다.
reportlab 은 CFF 기반 OTF 를 임베드하지 못하므로 TrueType(.ttf)만 지원한다.

OFL 폰트가 없는 머신(예: 깨끗한 Windows)에서는 마지막 수단으로 시스템 폰트(맑은 고딕)를 쓰되, 생성물이 그 폰트의
글리프를 담으므로 **공개 저장소에 올리면 안 된다**. 이때 manifest 의 라이선스 필드는 OFL 이 아니라 "비OFL"로 기록한다.
"""
from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

FONT_LICENSE_OFL = "SIL Open Font License 1.1"
FONT_LICENSE_UNKNOWN = "NOT-OFL: 시스템 폰트 또는 사용자 지정 폰트, 재배포 불가로 간주(생성물 비공개)"

# 파일명(소문자) -> (family, license). 여기에 없는 폰트는 라이선스를 알 수 없는 것으로 기록한다.
_KNOWN = {
    "nanumbarungothic.ttf": ("NanumBarunGothic", FONT_LICENSE_OFL),
    "nanumgothic.ttf": ("NanumGothic", FONT_LICENSE_OFL),
    "notosanskr-regular.ttf": ("Noto Sans KR", FONT_LICENSE_OFL),
}
_SYSTEM_FALLBACK = {"malgun.ttf": "Malgun Gothic"}  # Windows 기본 한글 폰트(OFL 아님)

FONT_FAMILY = "NanumBarunGothic"  # 하위 호환(기본 후보 이름)


def _dirs() -> list[Path]:
    home = Path.home()
    dirs = [Path("/Library/Fonts"), Path("/System/Library/Fonts/Supplemental"), home / "Library/Fonts",
            Path("/usr/share/fonts/truetype/nanum"), Path("/usr/share/fonts/truetype/noto"), home / ".fonts"]
    windir = os.environ.get("WINDIR") or os.environ.get("SystemRoot") or "C:/Windows"
    dirs.append(Path(windir) / "Fonts")
    local = os.environ.get("LOCALAPPDATA")
    if local:  # Windows 10 1809+ 사용자 단위 설치 폰트
        dirs.append(Path(local) / "Microsoft/Windows/Fonts")
    return dirs


def candidates() -> list[Path]:
    """우선순위 순: OFL 폰트 전부를 시스템 폰트보다 먼저."""
    ds = _dirs()
    out = [d / n for n in ("NanumBarunGothic.ttf", "NanumGothic.ttf", "NotoSansKR-Regular.ttf") for d in ds]
    out += [d / "malgun.ttf" for d in ds]
    return out


def find_font(explicit: str | None = None) -> Path:
    paths: list[str | Path] = [explicit] if explicit else []
    env = os.environ.get("SCANINBOX_FONT")
    if env:
        paths.append(env)
    paths += candidates()
    for p in paths:
        if p and Path(p).is_file():
            found = Path(p)
            if found.name.lower() in _SYSTEM_FALLBACK and not explicit and not env:
                print(f"경고: OFL 폰트를 찾지 못해 시스템 폰트 {found.name} 로 생성합니다. "
                      "생성물은 공개 저장소에 올리지 마세요(나눔바른고딕 설치 권장).", file=sys.stderr)
            return found
    raise FileNotFoundError(
        "한글 TTF 폰트를 찾지 못했습니다. 나눔바른고딕(OFL) 등을 설치하거나 "
        "SCANINBOX_FONT=<경로.ttf> 로 지정하세요."
    )


def font_info(path: Path) -> dict:
    """manifest 에 기록할 폰트 정보. 로컬 경로는 기록하지 않고 파일명·해시만 남긴다.

    라이선스는 알려진 OFL 폰트 파일명일 때만 OFL 로 기록한다. 그 밖의 폰트(맑은 고딕, --font 로 지정한 임의 폰트)를
    OFL 로 잘못 적지 않도록 비OFL 로 기록한다.
    """
    name = path.name.lower()
    family, lic = _KNOWN.get(name) or (_SYSTEM_FALLBACK.get(name) or path.stem, FONT_LICENSE_UNKNOWN)
    return {
        "family": family,
        "file": path.name,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "license": lic,
    }
