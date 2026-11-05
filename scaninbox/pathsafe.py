"""파일명·경로 안전 (core).

규칙 함수(금지문자·예약이름·길이·충돌 키)는 순수 문자열 연산이라 OS와 무관하게 시험할 수 있다.
파일시스템을 건드리는 함수(`ensure_within`, `dir_keys`, `move_no_overwrite`)는 아래쪽에 따로 둔다.

제안 이름: `YYYY-MM-DD_제목__짧은ID.pdf`, 날짜 불명은 `날짜미상_제목__짧은ID.pdf`. 짧은 ID는 원본 SHA-256 앞 8자.
"""
from __future__ import annotations

import os
import re
import shutil
import unicodedata
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Iterable, Optional

UNKNOWN_DATE = "날짜미상"
SHORT_ID_LEN = 8

# Windows 금지문자 + 제어문자(0x00-0x1f). 슬래시류는 경로 구분이라 파일명 한 조각에선 항상 금지.
_FORBIDDEN_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
# 예약 장치 이름. 확장자가 붙어도(`CON.txt`) 예약이다. 위첨자 숫자(COM¹)도 Windows 에서 예약이다.
_RESERVED_RE = re.compile(r"^(con|prn|aux|nul|conin\$|conout\$|(com|lpt)[0-9\u00b9\u00b2\u00b3])$", re.IGNORECASE)

# 구성 요소 한 개의 상한. NTFS 255 UTF-16 단위, APFS·ext4 255 UTF-8 바이트 → 가장 엄격한 쪽(바이트)에 맞추고 여유를 둔다.
MAX_NAME_BYTES = 240
# 확장 경로(`\\?\`)를 쓰지 않는 Windows 의 경로 상한(MAX_PATH 260, 종결 NUL 포함 → 문자 259).
MAX_PATH_PLAIN = 259
# 확장 경로 상한(약 32767 UTF-16 단위)에 여유.
MAX_PATH_EXTENDED = 32000


class PathSafetyError(ValueError):
    """경로·이름이 안전 규칙을 어김."""


class PathTooLong(PathSafetyError):
    """제목이 들어갈 자리가 없을 만큼 경로가 김."""


class EscapesRoot(PathSafetyError):
    """루트 밖으로 나가는 경로(.., 절대경로, symlink·junction 탈출)."""


# ---------------------------------------------------------------- 순수 규칙 함수

def nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


def utf16_len(s: str) -> int:
    """Windows 경로 길이 단위(UTF-16 코드 단위)."""
    return len(s.encode("utf-16-le")) // 2


def collision_key(name: str) -> str:
    """충돌 판정 키. NFC/NFD 와 대소문자만 다른 이름, 후행 점·공백만 다른 이름을 같은 이름으로 본다.

    NTFS 는 NFC/NFD 를 별개 파일로 두지만 대소문자 무시·후행 점·공백 제거를 하고, APFS 는 정규화 무시라서,
    어느 쪽에서도 안전하도록 **가장 넓게 같다고 본다**.
    """
    s = nfc(name).rstrip(" .")
    return nfc(s.casefold())


def short_id(sha256_hex: str) -> str:
    h = sha256_hex.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", h):
        raise PathSafetyError("SHA-256 형식이 아님")
    return h[:SHORT_ID_LEN]


def sanitize_component(text: str, *, replacement: str = "_") -> str:
    """파일명 한 조각을 Windows·macOS 양쪽에서 안전하게. 빈 결과·예약이름도 안전한 이름으로 바꾼다."""
    s = nfc(text)
    s = _FORBIDDEN_RE.sub(replacement, s)
    # 연속 공백 정리, 앞뒤 공백·후행 점 제거(Windows 는 후행 점·공백을 조용히 떼어 다른 이름이 된다)
    s = re.sub(r"\s+", " ", s).strip(" .")
    if not s:
        return replacement
    stem = s.split(".", 1)[0].rstrip(" ")
    if _RESERVED_RE.match(stem):
        s = replacement + s
    return s


def is_safe_component(name: str) -> bool:
    """이미 만들어진 이름이 규칙을 지키는가(금지문자·예약이름·후행 점·공백·NFC·길이)."""
    if not name or name in (".", ".."):
        return False
    if _FORBIDDEN_RE.search(name):
        return False
    if name != name.rstrip(" .") or name != name.lstrip(" "):
        return False
    if _RESERVED_RE.match(name.split(".", 1)[0].rstrip(" ")):
        return False
    if name != nfc(name):
        return False
    return len(name.encode("utf-8")) <= MAX_NAME_BYTES


def fit_title(title: str, budget_name_bytes: int, budget_path_units: Optional[int]) -> str:
    """두 예산(바이트·UTF-16 단위) 안에 들어가도록 제목을 뒤에서 자른다."""
    t = sanitize_component(title)
    while t and (len(t.encode("utf-8")) > budget_name_bytes
                 or (budget_path_units is not None and utf16_len(t) > budget_path_units)):
        t = t[:-1]
    t = t.rstrip(" .")
    if not t:
        raise PathTooLong("경로가 너무 길어 제목을 넣을 수 없음")
    return t


def proposed_name(
    title: str,
    sha256_hex: str,
    doc_date: Optional[str] = None,
    *,
    dir_path: str = "",
    max_path: Optional[int] = None,
    ext: str = ".pdf",
) -> str:
    """제안 파일명 `YYYY-MM-DD_제목__짧은ID.pdf` (날짜 불명이면 `날짜미상_…`).

    - `doc_date` 는 `YYYY-MM-DD` 만 받는다. 형식이 틀리면 거절한다(스캔일 등으로 대체하지 않는다).
    - `dir_path` 는 이 파일이 놓일 폴더 경로 문자열. 주어지면 `dir/이름` 전체가 `max_path`(기본: 일반 경로
      259, `\\\\?\\` 로 시작하면 확장 상한) 안에 들도록 제목을 줄인다. 충돌 접미사 여유 8자를 남긴다.
    """
    if doc_date is None:
        date_part = UNKNOWN_DATE
    else:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", doc_date):
            raise PathSafetyError("날짜는 YYYY-MM-DD 형식이어야 함")
        date_part = doc_date
    if not re.fullmatch(r"\.[A-Za-z0-9]{1,8}", ext):
        raise PathSafetyError("확장자 형식 오류")
    head = f"{date_part}_"
    tail = f"__{short_id(sha256_hex)}"
    reserve = len("_99")  # resolve_collision 접미사 자리
    name_budget = MAX_NAME_BYTES - len((head + tail + ext).encode("utf-8")) - reserve
    path_budget = None
    if dir_path:
        if max_path is None:
            max_path = MAX_PATH_EXTENDED if dir_path.startswith("\\\\?\\") else MAX_PATH_PLAIN
        path_budget = max_path - utf16_len(dir_path.rstrip("/\\")) - 1 - utf16_len(head + tail + ext) - reserve
    t = fit_title(title, name_budget, path_budget)
    name = f"{head}{t}{tail}{ext}"
    assert is_safe_component(name), name
    return name


def resolve_collision(name: str, taken_keys: Iterable[str]) -> str:
    """`taken_keys`(= collision_key 모음)와 겹치면 확장자 앞에 `_2`, `_3` … 을 붙인다. 덮어쓰기는 하지 않는다."""
    taken = set(taken_keys)
    if collision_key(name) not in taken:
        return name
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    ext = ("." + ext) if ext else ""
    for n in range(2, 1000):
        cand = f"{stem}_{n}{ext}"
        if collision_key(cand) not in taken:
            if len(cand.encode("utf-8")) > 255:
                raise PathTooLong("충돌 접미사를 붙이면 이름이 너무 김")
            return cand
    raise PathSafetyError("충돌 접미사를 찾지 못함")


# ---------------------------------------------------------------- 루트 포함 검사

def check_relative(rel: str) -> list[str]:
    """상대경로 문자열을 **어느 OS 규칙으로 봐도** 안전한 조각 목록으로. 위반이면 EscapesRoot.

    절대경로(POSIX `/x`, Windows 드라이브 `C:\\x`·`C:x`·UNC·`\\\\?\\`), `..`, 빈 문자열, NUL 을 거절한다.
    """
    if not rel or "\x00" in rel:
        raise EscapesRoot("빈 경로 또는 NUL")
    if rel.startswith(("/", "\\")) or PurePosixPath(rel).is_absolute() or PureWindowsPath(rel).is_absolute():
        raise EscapesRoot("절대경로는 허용하지 않음")
    if PureWindowsPath(rel).drive or PureWindowsPath(rel).root:
        raise EscapesRoot("드라이브·루트가 있는 경로는 허용하지 않음")
    parts = [p for p in re.split(r"[\\/]", rel) if p not in ("", ".")]
    if not parts:
        raise EscapesRoot("빈 경로")
    if any(p == ".." for p in parts):
        raise EscapesRoot("'..' 은 허용하지 않음")
    return parts


def _norm(p: str) -> str:
    return nfc(os.path.normcase(p)).casefold()


def _is_link_like(p: Path) -> bool:
    if p.is_symlink():
        return True
    isj = getattr(os.path, "isjunction", None)
    return bool(isj and isj(p))


def is_within(root: Path, target: Path) -> bool:
    """`target` 의 실제 경로(symlink·junction 해소)가 `root` 의 실제 경로 아래(또는 같음)인가."""
    r = _norm(os.path.realpath(root))
    t = _norm(os.path.realpath(target))
    try:
        return os.path.commonpath([r, t]) == r
    except ValueError:  # 드라이브가 다름
        return False


def ensure_within(root: Path | str, rel: str) -> Path:
    """`root/rel` 을 돌려주되, 루트 밖으로 나가면 EscapesRoot.

    어휘 검사(`..`·절대경로) 후, 존재하는 앞부분은 realpath 로 풀어 symlink·junction 탈출도 거절한다.
    """
    root = Path(root)
    parts = check_relative(rel)
    target = root.joinpath(*parts)
    cur = root
    for p in parts:
        cur = cur / p
        if _is_link_like(cur):
            raise EscapesRoot(f"링크(symlink·junction)를 거치는 경로는 허용하지 않음: {p}")
    if not is_within(root, target):
        raise EscapesRoot("실제 경로가 루트 밖")
    return target


# ---------------------------------------------------------------- 파일시스템

def dir_keys(directory: Path | str) -> set[str]:
    """폴더 안 항목 이름의 collision_key 집합(없으면 빈 집합)."""
    d = Path(directory)
    if not d.is_dir():
        return set()
    return {collision_key(n) for n in os.listdir(d)}


def unique_name(directory: Path | str, name: str) -> str:
    """폴더의 기존 항목과 충돌하지 않는 이름(필요하면 접미사)."""
    return resolve_collision(name, dir_keys(directory))


def move_no_overwrite(src: Path | str, dst: Path | str) -> None:
    """`src` 를 `dst` 로 옮기되 `dst` 가 이미 있으면(대소문자·정규화만 달라도) FileExistsError. 덮어쓰지 않는다.

    같은 볼륨이면 hard link→unlink(POSIX) 또는 os.rename(Windows, 기존 있으면 실패)로 원자적으로 처리.
    다른 볼륨이면 `dst` 폴더의 임시 이름에 복사·fsync 후 같은 방식으로 최종 이름에 배치한다(임시 이름은
    최종 이름으로 보이지 않는다).
    """
    src, dst = Path(src), Path(dst)
    if collision_key(dst.name) in dir_keys(dst.parent):
        raise FileExistsError(str(dst))
    try:
        _atomic_place(src, dst)
        return
    except OSError as e:
        if isinstance(e, FileExistsError):
            raise
        import errno
        if e.errno != errno.EXDEV:
            raise
    tmp = dst.with_name(f".{dst.name}.{os.getpid()}.partial")
    try:
        with open(src, "rb") as fi, open(tmp, "xb") as fo:
            shutil.copyfileobj(fi, fo, 1 << 20)
            fo.flush()
            os.fsync(fo.fileno())
        _atomic_place(tmp, dst)
    finally:
        if tmp.exists():
            tmp.unlink()
    src.unlink()


def _atomic_place(src: Path, dst: Path) -> None:
    if os.name == "nt":
        os.rename(src, dst)  # Windows: 대상이 있으면 FileExistsError
        return
    os.link(src, dst)  # 대상이 있으면 FileExistsError, 다른 볼륨이면 EXDEV
    os.unlink(src)
