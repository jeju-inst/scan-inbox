"""Windows 긴 경로(MAX_PATH 260자) 대응.

`LongPathsEnabled=0` 인 Windows 에서는 260자를 넘는 경로를 일반 경로로 열 수 없다. 코퍼스의 "긴 한글 경로"
시험 폴더(228자)는 임시 폴더 접두와 합치면 이 한계를 쉽게 넘는다. 루트 경로를 `\\\\?\\` 확장 경로로 바꿔 쓰면
그 아래에서 만든 모든 경로가 확장 경로가 되어 한계를 피한다. Windows 가 아니면 아무것도 바꾸지 않는다.
"""
from __future__ import annotations

import ntpath
import os
from pathlib import Path

_EXT = "\\\\?\\"
_EXT_UNC = "\\\\?\\UNC\\"


def to_extended_str(s: str) -> str:
    """Windows 경로 문자열을 `\\\\?\\` 확장 경로로. 순수 문자열 변환이라 어느 OS 에서도 시험할 수 있다."""
    if s.startswith(_EXT):
        return s
    s = ntpath.abspath(s)
    if s.startswith("\\\\"):
        return _EXT_UNC + s[2:]
    return _EXT + s


def strip_extended_str(s: str) -> str:
    """`\\\\?\\` 접두를 떼어 일반 표기로. 링크 대상 문자열 비교에 쓴다."""
    if s.startswith(_EXT_UNC):
        return "\\\\" + s[len(_EXT_UNC):]
    if s.startswith(_EXT):
        return s[len(_EXT):]
    return s


def extended(p: str | os.PathLike) -> Path:
    """Windows 면 확장 경로 Path, 아니면 그대로."""
    if os.name != "nt":
        return Path(p)
    return Path(to_extended_str(os.fspath(p)))
