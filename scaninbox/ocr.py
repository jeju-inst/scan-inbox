"""OCR 엔진: OCRmyPDF + Tesseract 를 하위 프로세스로 실행하고 결과를 직접 검증한다.

설계 원칙(docs/G1-DESIGN.md §3 engines, docs/g0 결과 반영):

- **셸을 쓰지 않는다.** 모든 외부 명령은 인자 배열로 실행한다(Windows 의 ``%VAR%`` 치환·따옴표 문제 회피).
- **종료코드 0 을 성공으로 믿지 않는다.** 출력 PDF 에서 쪽별 텍스트를 다시 추출해 쪽 수 보존과 빈 쪽·인식
  불량 쪽을 앱이 판정한다(G0: PDF ``/Rotate`` 쪽이 텍스트 0자인데 종료코드 0 이던 사례, 회전 쪽이 쓰레기
  문자로 인식되던 사례).
- **시간 초과·취소 시 프로세스 트리 전체를 종료한다**(맥·리눅스: 프로세스 그룹, Windows: ``taskkill /T /F``).
  부모만 종료하면 자식이 고아로 남는 것이 G0 Windows 시험에서 확인됐다.
- 기본 옵션은 ``--skip-text --rotate-pages --deskew``. ``--oversample 300`` 과 ``psm 6`` 은 쓰지 않는다
  (G0: 합성 코퍼스에서 인식률 악화).
- 로그와 사유 문자열에 OCR 본문을 넣지 않는다(건수·쪽 번호·종료코드만).

회전 임계값(``--rotate-pages-threshold``) 선택 근거는 ``DEFAULT_ROTATE_THRESHOLD`` 주석과
``tests/test_ocr.py::test_rotate_threshold_band`` 를 본다.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import unicodedata
import uuid
from pathlib import Path
from typing import Optional, Sequence

from pypdf import PdfReader

from .contracts import OcrResult, OcrStatus

log = logging.getLogger("scaninbox.ocr")

#: 검증한 OCRmyPDF 버전(pyproject 고정값과 같아야 한다). 다른 버전이면 엔진 문자열에 그대로 기록된다.
OCRMYPDF_PINNED = "17.13.0"

#: ``--rotate-pages-threshold`` 기본값(방향 신뢰도 하한, OCRmyPDF 기본 12).
#:
#: 합성 코퍼스 ``rotated_blank``(빈 쪽 2, 이미지 90도 회전 쪽 1, PDF /Rotate 90 쪽 1)에서 macOS arm64,
#: OCRmyPDF 17.13.0, Tesseract 5.5.3, tessdata_fast kor 4.1.0, ``--deskew`` 로 잰 값(``tests/test_ocr.py`` 에 재현):
#:
#: ===========  ==========================  =============================
#: 임계값       이미지 회전 쪽 글자 재현율  /Rotate 90 쪽 글자 재현율
#: ===========  ==========================  =============================
#: 12 ~ 4       0.06 (쓰레기 문자 112자)    1.00
#: 2 ~ 1.0      0.89                        1.00
#: 0.8 ~ 0.5    0.89                        0.03 (과회전으로 악화)
#: ===========  ==========================  =============================
#:
#: 즉 1.0~2.0 이 양쪽을 모두 살린다. Paperless 시험에서 잰 방향 신뢰도는 회전 쪽 1.43, 빈 쪽 0.83 이었으므로
#: 두 값의 중간 근처인 1.2 를 골랐다(빈 쪽 과회전 여유 0.37, 회전 쪽 교정 여유 0.23). **합성 소표본 관찰값이며 실스캔
#: 일반값이 아니다**(실문서 재측정 필요). 앱은 이 값과 무관하게 출력 텍스트를 다시 검증한다.
DEFAULT_ROTATE_THRESHOLD = 1.2

#: 이 글자 수(공백 제외) 이상이면 그 쪽에 텍스트층이 있다고 본다.
MIN_PAGE_CHARS = 20

#: ``page_text_quality`` 가 이 값 미만이면 "인식 품질 의심". 약한 절대 지표(``suspect_pages`` 참조).
MIN_PAGE_QUALITY = 0.45

_KILL_GRACE_S = 3.0
_POLL_S = 0.2

# OCRmyPDF 종료코드(공식 ExitCode).
_RC_INPUT_FILE = 2
_RC_MISSING_DEP = 3
_RC_ENCRYPTED = 8
_RC_INVALID_CONFIG = 9

_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_WS_RE = re.compile(r"\s+")


class OcrEnvironmentError(RuntimeError):
    """OCR 실행 환경(OCRmyPDF·Tesseract·언어팩)이 준비되지 않았다. 메시지는 설치 안내를 담는다."""


def normalize_text(s: str) -> str:
    """NUL 등 제어 문자를 제거하고 NFC 로 정규화한다(OCR 층에 끼는 U+0000 대비)."""
    return unicodedata.normalize("NFC", _CTRL_RE.sub("", s))


def compact_len(s: str) -> int:
    return len(_WS_RE.sub("", s))


def hangul_share(text: str) -> Optional[float]:
    """글자(한글 음절+영문자) 중 한글 비율. 글자가 없으면 None."""
    h = l = 0
    for ch in unicodedata.normalize("NFC", text):
        o = ord(ch)
        if 0xAC00 <= o <= 0xD7A3:
            h += 1
        elif ch.isascii() and ch.isalpha():
            l += 1
    return None if h + l == 0 else h / (h + l)


def page_text_quality(text: str) -> float:
    """쪽 텍스트 모양이 '글'다운 정도(0~1). **약한 절대 지표**이며 단독 판정에 쓰지 않는다.

    낱글자 단어 비율과 이상 문자(NUL·기호) 비율만 본다. G0 의 90도 회전 쪽 쓰레기
    (``튜 1 아이 kein lolel2 SBR ...``)는 이 점수로는 정상 쪽과 구분되지 않았다(0.78 대 0.6~0.9). 그래서 쓰레기
    쪽 판정은 아래 ``suspect_pages`` 의 문서 내 문자 구성 비교를 주로 쓴다.
    """
    t = unicodedata.normalize("NFC", text)
    raw_len = len(t)
    t_clean = _CTRL_RE.sub("", t)
    if compact_len(t_clean) == 0:
        return 0.0
    words = t_clean.split()
    single_ratio = sum(1 for w in words if len(w) == 1) / max(1, len(words))
    allowed = total = 0
    for ch in t_clean:
        if ch.isspace():
            continue
        total += 1
        if (0xAC00 <= ord(ch) <= 0xD7A3) or (ch.isascii() and (ch.isalnum() or ch in ".,:;()-/%+&'\"*#?!~=<>[]")):
            allowed += 1
    odd_ratio = 1.0 - allowed / max(1, total)
    nul_ratio = (raw_len - len(t_clean)) / max(1, raw_len)
    return max(0.0, min(1.0, 1.0 - (single_ratio * 0.7 + (odd_ratio + nul_ratio) * 1.5)))


def suspect_pages(texts: Sequence[str]) -> list[int]:
    """텍스트는 있으나 읽을 수 없는 글일 가능성이 큰 쪽(1부터).

    규칙(둘 중 하나):

    1. 절대 하한: ``page_text_quality < MIN_PAGE_QUALITY``.
    2. 문자 구성 이탈: 같은 문서의 다른 쪽들은 한글 위주(한글 비율 ≥ 0.3)인데 이 쪽은 한글이 거의 없고(< 0.1)
       2자 이하 조각이 많음(≥ 0.3). Tesseract 가 한글 쪽을 잘못된 방향으로 읽으면 라틴 문자 쓰레기가 나오는
       G0 사례를 겨냥한다. 한 쪽짜리 문서나 영문 문서에는 적용되지 않는다(비교 대상이 없음). 영문 쪽이 섞인 묶음은
       조각 비율이 낮아(코퍼스 0.14~0.17, 쓰레기 쪽 0.44) 오탐하지 않았다. **쓰레기 표본은 1쪽뿐이다.**

    합성 코퍼스의 정상 쪽을 오탐하지 않고 G0 쓰레기 쪽(임계값 12 출력)을 잡는 것을 ``tests/test_ocr.py`` 로
    확인했다. **실문서에서의 오탐·미탐은 미측정이다.**
    """
    n = len(texts)
    shares = [hangul_share(t) if compact_len(t) >= MIN_PAGE_CHARS else None for t in texts]
    out: list[int] = []
    for i, t in enumerate(texts):
        if compact_len(t) == 0:
            continue
        bad = page_text_quality(t) < MIN_PAGE_QUALITY
        others = [shares[j] for j in range(n) if j != i and shares[j] is not None]
        if not bad and shares[i] is not None and others:
            others_sorted = sorted(others)
            med = others_sorted[len(others_sorted) // 2]
            words = t.split()
            short_ratio = sum(1 for w in words if len(w) <= 2) / max(1, len(words))
            # 영문 쪽(다른 언어 문서가 섞인 묶음)을 오탐하지 않도록 짧은 조각 비율도 함께 본다.
            bad = med >= 0.3 and shares[i] < 0.1 and short_ratio >= 0.3
        if bad:
            out.append(i + 1)
    return out


def extract_pages(pdf: Path) -> tuple[list[str], list[int]]:
    """PDF 에서 쪽별 텍스트(정규화)와 쪽별 ``/Rotate`` 값을 뽑는다. 읽기 실패는 예외로 전달."""
    reader = PdfReader(str(pdf), strict=False)
    texts: list[str] = []
    rotates: list[int] = []
    for page in reader.pages:
        try:
            texts.append(normalize_text(page.extract_text() or ""))
        except Exception:  # 개별 쪽 추출 실패는 텍스트 없음으로 본다(쪽 수는 유지)
            texts.append("")
        try:
            rotates.append(int(page.get("/Rotate", 0) or 0) % 360)
        except Exception:
            rotates.append(0)
    return texts, rotates


def _arg_path(p: Path) -> str:
    """하위 프로세스 인자용 경로. Windows 에서 260자에 가까우면 ``\\\\?\\`` 접두(G0: 접두 입력은 성공)."""
    s = str(p)
    if os.name == "nt" and len(s) >= 240 and not s.startswith("\\\\?\\"):
        s = "\\\\?\\" + os.path.abspath(s)
    return s


def kill_process_tree(proc: subprocess.Popen) -> None:
    """프로세스와 모든 자손을 종료한다. Windows 는 ``taskkill /T /F``, 그 외는 프로세스 그룹."""
    if os.name == "nt":  # 부모가 이미 끝났어도 자식이 남았을 수 있어 taskkill 을 시도한다
        try:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True,
                           stdin=subprocess.DEVNULL, timeout=15, check=False)
        except Exception:  # taskkill 실패 시 부모만이라도 종료
            pass
        try:
            proc.kill()
        except OSError:
            pass
        return
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, OSError):
        pgid = proc.pid
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    deadline = time.monotonic() + _KILL_GRACE_S
    while time.monotonic() < deadline:
        if proc.poll() is not None and not _group_alive(pgid):
            return
        time.sleep(0.05)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def run_tree(argv: Sequence[str], *, env: dict[str, str], timeout_s: float,
             cancel: Optional[threading.Event] = None) -> tuple[Optional[int], str, str]:
    """인자 배열로 실행하고 (종료코드, 종료사유, stderr 꼬리)를 돌려준다.

    종료사유: ``"exit"`` 정상 종료, ``"timeout"`` 시간 초과, ``"cancel"`` 취소. 시간 초과·취소 땐 트리를 종료한다.
    stdin 은 닫아 둔다(입력 대기 방지). 셸을 쓰지 않는다.
    """
    kwargs: dict = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=env)
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(list(argv), **kwargs)
    deadline = time.monotonic() + timeout_s
    err = b""
    reason = "exit"
    while True:
        try:
            _, e = proc.communicate(timeout=_POLL_S)
            err = e or b""
            break
        except subprocess.TimeoutExpired:
            pass
        if cancel is not None and cancel.is_set():
            reason = "cancel"
        elif time.monotonic() >= deadline:
            reason = "timeout"
        if reason != "exit":
            kill_process_tree(proc)
            try:
                _, e = proc.communicate(timeout=10)
                err = e or b""
            except Exception:
                pass
            break
    if reason == "exit" and os.name != "nt":
        # 정상 종료 뒤 같은 그룹에 남은 자손이 있으면 정리한다(고아 방지).
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    tail = err.decode("utf-8", "replace").strip().splitlines()
    return proc.returncode, reason, (tail[-1] if tail else "")[:200]


class OcrmypdfEngine:
    """``contracts.OcrEngine`` 구현. 상태 판정은 출력 PDF 를 다시 읽어 앱이 한다.

    :param ocrmypdf_cmd: OCRmyPDF 실행 명령(인자 배열). 기본은 PATH 의 ``ocrmypdf``, 없으면 현재 인터프리터의
        ``-m ocrmypdf``.
    :param tessdata_dir: ``TESSDATA_PREFIX`` 로 줄 폴더(사용자 홈 아래 격리 설치용). None 이면 환경값을 그대로 쓴다.
    :param tesseract_dir: ``tesseract`` 실행 파일이 있는 폴더. 주면 하위 프로세스 PATH 앞에만 붙인다(전역 변경 없음).
    :param rotate_threshold: ``--rotate-pages-threshold`` 값. ``DEFAULT_ROTATE_THRESHOLD`` 주석 참조.
    :param jobs: ``-j`` 값. None 이면 OCRmyPDF 기본.
    :param extra_args: 추가 옵션. ``--oversample``·``--tesseract-pagesegmode`` 는 거절한다(G0 결과).
    """

    FORBIDDEN_OPTIONS = ("--oversample", "--tesseract-pagesegmode")

    def __init__(self, ocrmypdf_cmd: Optional[Sequence[str]] = None, *, tessdata_dir: Optional[Path] = None,
                 tesseract_dir: Optional[Path] = None, rotate_threshold: float = DEFAULT_ROTATE_THRESHOLD,
                 jobs: Optional[int] = None, extra_args: Sequence[str] = ()) -> None:
        for a in extra_args:
            if a.split("=", 1)[0] in self.FORBIDDEN_OPTIONS:
                raise ValueError(f"금지된 OCR 옵션: {a} (G0 에서 인식률 악화 확인)")
        if ocrmypdf_cmd is None:
            found = shutil.which("ocrmypdf")
            ocrmypdf_cmd = [found] if found else [sys.executable, "-m", "ocrmypdf"]
        self._cmd = [str(c) for c in ocrmypdf_cmd]
        self._tessdata = Path(tessdata_dir) if tessdata_dir else None
        self._tess_dir = Path(tesseract_dir) if tesseract_dir else None
        self._rotate_threshold = rotate_threshold
        self._jobs = jobs
        self._extra = list(extra_args)
        self._env_cache: dict[str, str] = {}

    # ---- 환경 ---------------------------------------------------------------------------------

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        if self._tessdata is not None:
            env["TESSDATA_PREFIX"] = str(self._tessdata)
        if self._tess_dir is not None:
            env["PATH"] = str(self._tess_dir) + os.pathsep + env.get("PATH", "")
        return env

    def _capture(self, argv: Sequence[str], timeout: float = 30.0) -> tuple[int, str]:
        p = subprocess.run(list(argv), env=self._env(), capture_output=True, stdin=subprocess.DEVNULL,
                           timeout=timeout, check=False)
        return p.returncode, (p.stdout + p.stderr).decode("utf-8", "replace")

    def check_environment(self, langs: str = "kor+eng") -> str:
        """OCRmyPDF·Tesseract·언어팩을 확인하고 엔진 문자열을 돌려준다. 미비하면 ``OcrEnvironmentError``."""
        if langs in self._env_cache:
            return self._env_cache[langs]
        try:
            rc, out = self._capture([*self._cmd, "--version"])
        except (OSError, subprocess.TimeoutExpired) as e:
            raise OcrEnvironmentError(f"OCRmyPDF 를 실행할 수 없음({type(e).__name__}). "
                                      f"`pip install ocrmypdf=={OCRMYPDF_PINNED}` 로 설치하세요.") from e
        if rc != 0:
            raise OcrEnvironmentError(f"OCRmyPDF --version 실패(종료코드 {rc}). "
                                      f"`pip install ocrmypdf=={OCRMYPDF_PINNED}` 로 설치하세요.")
        ocr_version = out.strip().splitlines()[-1].strip() if out.strip() else "?"
        tess = shutil.which("tesseract", path=self._env().get("PATH"))
        if tess is None:
            raise OcrEnvironmentError("Tesseract 를 찾을 수 없음. macOS: `brew install tesseract`, "
                                      "Windows: UB-Mannheim 빌드를 설치하거나 `tesseract_dir` 로 지정하세요.")
        try:
            rc, out = self._capture([tess, "--list-langs"])
        except (OSError, subprocess.TimeoutExpired) as e:
            raise OcrEnvironmentError(f"Tesseract 실행 실패({type(e).__name__})") from e
        lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
        header = next((ln for ln in lines if ln.lower().startswith("list of available languages")), "")
        m = re.search(r'in "(.*?)/?"', header)
        tessdata_found = Path(m.group(1)) if m else None
        available = {ln for ln in lines if not ln.lower().startswith("list of") and " " not in ln}
        wanted = [x for x in langs.split("+") if x]
        missing = [x for x in wanted if x not in available]
        if missing:
            raise OcrEnvironmentError(
                f"Tesseract 언어팩 없음: {'+'.join(missing)}. 설치 안내: tessdata_fast 의 "
                f"`{missing[0]}.traineddata`(https://github.com/tesseract-ocr/tessdata_fast) 를 사용자 폴더의 "
                f"tessdata 디렉터리(`eng`·`osd`·`configs`·`tessconfigs` 포함)에 두고 `tessdata_dir` 로 지정하거나 "
                f"`TESSDATA_PREFIX` 를 설정하세요. macOS 는 `brew install tesseract-lang` 도 가능합니다. "
                f"현재 인식되는 언어: {', '.join(sorted(available)) or '없음'}")
        rc, tv = self._capture([tess, "--version"])
        tess_version = (tv.strip().splitlines() or ["tesseract ?"])[0].replace("tesseract", "").strip().lstrip("v")
        packs = []
        base = self._tessdata or tessdata_found
        for lang in wanted:
            h = "?"
            if base is not None:
                f = Path(base) / f"{lang}.traineddata"
                if f.is_file():
                    h = hashlib.sha256(f.read_bytes()).hexdigest()[:8]
            packs.append(f"{lang}:{h}")
        engine = f"ocrmypdf {ocr_version} / tesseract {tess_version} {langs} ({' '.join(packs)})"
        self._env_cache[langs] = engine
        return engine

    # ---- 실행 ---------------------------------------------------------------------------------

    def run(self, src: Path, dst_dir: Path, *, langs: str = "kor+eng", timeout_s: int = 600,
            cancel: Optional[threading.Event] = None) -> OcrResult:
        src = Path(src)
        dst_dir = Path(dst_dir)
        t0 = time.monotonic()
        pre = self._precheck(src)
        if isinstance(pre, OcrResult):
            return pre
        texts_in, pages_in = pre
        if pages_in > 0 and all(compact_len(t) >= MIN_PAGE_CHARS for t in texts_in):
            return self._already_text(src, dst_dir, texts_in, pages_in)
        try:
            engine = self.check_environment(langs)
        except OcrEnvironmentError as e:
            return OcrResult(OcrStatus.FAILED, page_count_in=pages_in, reason=str(e))
        dst_dir.mkdir(parents=True, exist_ok=True)
        out = dst_dir / f"ocr-{uuid.uuid4().hex[:12]}.pdf"
        argv = [*self._cmd, "-q", "--no-progress-bar", "-l", langs, "--skip-text", "--rotate-pages",
                "--rotate-pages-threshold", str(self._rotate_threshold), "--deskew", "--output-type", "pdf"]
        if self._jobs:
            argv += ["-j", str(self._jobs)]
        argv += self._extra + [_arg_path(src), _arg_path(out)]
        try:
            rc, why, tail = run_tree(argv, env=self._env(), timeout_s=timeout_s, cancel=cancel)
        except OSError as e:
            return OcrResult(OcrStatus.FAILED, page_count_in=pages_in, engine=engine,
                             reason=f"OCRmyPDF 실행 실패({type(e).__name__})")
        if why != "exit":
            self._discard(out)
            msg = f"시간 초과({timeout_s}초)로 중단" if why == "timeout" else "취소됨"
            log.info("ocr %s pages=%d", why, pages_in)
            return OcrResult(OcrStatus.FAILED, page_count_in=pages_in, engine=engine, reason=msg)
        if rc != 0:
            self._discard(out)
            return self._map_failure(rc, tail, src, pages_in, engine)
        result = self._verify(out, pages_in, engine)
        log.info("ocr status=%s pages=%d->%d empty=%d sec=%.1f", result.status.value, result.page_count_in,
                 result.page_count_out, len(result.empty_pages), time.monotonic() - t0)
        return result

    # ---- 단계 구현 ----------------------------------------------------------------------------

    def _precheck(self, src: Path) -> "OcrResult | tuple[list[str], int]":
        """사전 판별: 열 수 없음→CORRUPT, 암호화→ENCRYPTED. 통과하면 (쪽별 텍스트, 쪽 수)."""
        try:
            reader = PdfReader(str(src), strict=False)
            if reader.is_encrypted:
                try:
                    ok = reader.decrypt("")
                except Exception:
                    ok = 0
                if not ok:
                    return OcrResult(OcrStatus.ENCRYPTED, reason="열기 암호가 있는 PDF(암호 해제 필요)")
            pages = len(reader.pages)
            if pages == 0:
                return OcrResult(OcrStatus.CORRUPT, reason="쪽이 없는 PDF")
            texts, _ = extract_pages(src)
        except Exception as e:  # pypdf 는 손상 PDF 에 다양한 예외를 낸다
            return OcrResult(OcrStatus.CORRUPT, reason=f"PDF 를 열 수 없음({type(e).__name__})")
        return texts, pages

    def _already_text(self, src: Path, dst_dir: Path, texts: list[str], pages: int) -> OcrResult:
        """모든 쪽에 충분한 텍스트층이 있으면 OCR 을 건너뛴다. 출력은 원본 바이트의 복사본(파이프라인 일관성)."""
        dst_dir.mkdir(parents=True, exist_ok=True)
        out = dst_dir / f"ocr-{uuid.uuid4().hex[:12]}.pdf"
        shutil.copyfile(src, out)
        return OcrResult(OcrStatus.ALREADY_TEXT, output=out, page_count_in=pages, page_count_out=pages,
                         text_by_page=texts, empty_pages=[], reason="이미 텍스트층이 있어 OCR 생략",
                         engine="(OCR 생략)")

    def _verify(self, out: Path, pages_in: int, engine: str) -> OcrResult:
        if not out.is_file() or out.stat().st_size == 0:
            return OcrResult(OcrStatus.FAILED, page_count_in=pages_in, engine=engine,
                             reason="OCRmyPDF 가 출력 파일을 만들지 않음")
        try:
            texts, rotates = extract_pages(out)
        except Exception as e:
            self._discard(out)
            return OcrResult(OcrStatus.FAILED, page_count_in=pages_in, engine=engine,
                             reason=f"출력 PDF 를 읽을 수 없음({type(e).__name__})")
        if len(texts) != pages_in:
            self._discard(out)
            return OcrResult(OcrStatus.FAILED, page_count_in=pages_in, page_count_out=len(texts), engine=engine,
                             reason=f"쪽 수가 바뀜({pages_in}→{len(texts)})")
        empty = [i + 1 for i, t in enumerate(texts) if compact_len(t) == 0]
        suspect = suspect_pages(texts)
        reasons = []
        if empty:
            rot = [p for p in empty if rotates[p - 1] != 0]
            detail = f"쪽 {_fmt_pages(empty)} 텍스트 0자"
            if rot:
                detail += f"(그중 /Rotate 쪽 {_fmt_pages(rot)})"
            reasons.append(detail)
        if suspect:
            reasons.append(f"쪽 {_fmt_pages(suspect)} 인식 품질 의심")
        status = OcrStatus.PARTIAL if reasons else OcrStatus.OK
        return OcrResult(status, output=out, page_count_in=pages_in, page_count_out=len(texts), text_by_page=texts,
                         empty_pages=empty, reason="; ".join(reasons), engine=engine)

    def _map_failure(self, rc: int, tail: str, src: Path, pages_in: int, engine: str) -> OcrResult:
        if rc == _RC_ENCRYPTED:
            return OcrResult(OcrStatus.ENCRYPTED, page_count_in=pages_in, engine=engine,
                             reason="OCRmyPDF 가 암호화된 PDF 로 거부(종료코드 8)")
        if rc == _RC_INPUT_FILE:
            return OcrResult(OcrStatus.CORRUPT, page_count_in=pages_in, engine=engine,
                             reason="OCRmyPDF 가 입력 PDF 를 열지 못함(종료코드 2)")
        if rc in (_RC_MISSING_DEP, _RC_INVALID_CONFIG):
            return OcrResult(OcrStatus.FAILED, page_count_in=pages_in, engine=engine,
                             reason=f"OCR 환경 오류(종료코드 {rc}): 언어팩·tessdata 의 configs 폴더·의존성을 확인하세요")
        tail = tail.replace(str(src), "<input>")[:160]
        return OcrResult(OcrStatus.FAILED, page_count_in=pages_in, engine=engine,
                         reason=f"OCRmyPDF 실패(종료코드 {rc}) {tail}".strip())

    @staticmethod
    def _discard(p: Path) -> None:
        try:
            p.unlink()
        except OSError:
            pass


def _fmt_pages(pages: Sequence[int]) -> str:
    return ",".join(str(p) for p in pages)
