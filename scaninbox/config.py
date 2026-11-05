"""설정 검증과 저장 (core).

경로 네 개(스캔 폴더, 완료함, 원본 백업, journal)가 서로 겹치거나 포함하지 않는지, symlink·junction 으로
다른 경로 안으로 새지 않는지 검사한다. 설정 파일은 앱 데이터 경로에 저장한다.
`platformdirs` 가 설치돼 있으면 쓰고, 없으면 표준 라이브러리만으로 같은 위치를 계산한다.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from .pathsafe import is_within, nfc

APP_NAME = "scaninbox"
ENV_HOME = "SCANINBOX_HOME"  # 시험·격리 실행용: 앱 데이터 폴더를 직접 지정
DEFAULT_DONE_NAME = "scan_처리완료"
DEFAULT_BACKUP_NAME = "scan_원본보관"


class ConfigError(ValueError):
    """설정 검증 실패. `problems` 에 모든 문제를 담는다."""

    def __init__(self, problems: list[str]):
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


def app_data_dir() -> Path:
    env = os.environ.get(ENV_HOME)
    if env:
        return Path(env)
    try:
        import platformdirs  # type: ignore
        return Path(platformdirs.user_data_dir(APP_NAME, appauthor=False))
    except ImportError:
        pass
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / APP_NAME


def config_path() -> Path:
    return app_data_dir() / "config.json"


def default_journal_path() -> Path:
    return app_data_dir() / "journal.sqlite"


def default_done_dir(scan_dir: Path | str) -> Path:
    """스캔 폴더의 형제 `scan_처리완료`."""
    p = Path(scan_dir)
    return p.parent / DEFAULT_DONE_NAME


def default_backup_dir(scan_dir: Path | str) -> Path:
    p = Path(scan_dir)
    return p.parent / DEFAULT_BACKUP_NAME


@dataclass
class Config:
    scan_dir: Path
    done_dir: Path
    backup_dir: Path
    journal_path: Path
    ocr_langs: str = "kor+eng"
    ocr_timeout_s: int = 600
    stable_seconds: int = 10
    lease_seconds: int = 300

    @classmethod
    def with_defaults(cls, scan_dir: Path | str, *, done_dir=None, backup_dir=None, journal_path=None, **kw) -> "Config":
        scan = Path(scan_dir)
        return cls(
            scan_dir=scan,
            done_dir=Path(done_dir) if done_dir else default_done_dir(scan),
            backup_dir=Path(backup_dir) if backup_dir else default_backup_dir(scan),
            journal_path=Path(journal_path) if journal_path else default_journal_path(),
            **kw,
        )

    def to_json(self) -> dict:
        d = asdict(self)
        for k in ("scan_dir", "done_dir", "backup_dir", "journal_path"):
            d[k] = str(d[k])
        return d

    @classmethod
    def from_json(cls, d: dict) -> "Config":
        known = {f for f in cls.__dataclass_fields__}
        extra = set(d) - known
        if extra:
            raise ConfigError([f"알 수 없는 설정 항목: {sorted(extra)}"])
        missing = [k for k in ("scan_dir", "done_dir", "backup_dir", "journal_path") if k not in d]
        if missing:
            raise ConfigError([f"필수 설정 항목 없음: {missing}"])
        kw = dict(d)
        for k in ("scan_dir", "done_dir", "backup_dir", "journal_path"):
            kw[k] = Path(kw[k])
        return cls(**kw)


def _key(p: Path) -> str:
    """겹침 비교 키: 실제 경로(링크 해소) + 대소문자·정규화 무시(가장 넓게 같다고 본다)."""
    return nfc(os.path.normcase(os.path.realpath(p))).casefold()


def _overlap(a: Path, b: Path) -> Optional[str]:
    ka, kb = _key(a), _key(b)
    if ka == kb:
        return "같은 경로"
    try:
        common = os.path.commonpath([ka, kb])
    except ValueError:
        return None
    if common == ka:
        return "앞쪽이 뒤쪽을 포함"
    if common == kb:
        return "뒤쪽이 앞쪽을 포함"
    return None


def _link_like(p: Path) -> bool:
    isj = getattr(os.path, "isjunction", None)
    return p.is_symlink() or bool(isj and isj(p))


def validate(cfg: Config, *, require_scan_exists: bool = True) -> None:
    """문제가 있으면 모든 문제를 담아 ConfigError. 없으면 반환."""
    problems: list[str] = []
    named = {"scan_dir": cfg.scan_dir, "done_dir": cfg.done_dir,
             "backup_dir": cfg.backup_dir, "journal_path": cfg.journal_path}
    for n, p in named.items():
        if not Path(p).is_absolute():
            problems.append(f"{n}: 절대경로여야 함")
    if problems:
        raise ConfigError(problems)

    # 폴더 자체가 링크이면 어디로 가는지 설정만 봐서는 알 수 없으므로 거절
    for n in ("scan_dir", "done_dir", "backup_dir"):
        p = named[n]
        if _link_like(p):
            problems.append(f"{n}: symlink·junction 은 허용하지 않음(실제 폴더를 지정)")
        elif p.exists() and not p.is_dir():
            problems.append(f"{n}: 폴더가 아님")
    if require_scan_exists and not cfg.scan_dir.is_dir():
        problems.append("scan_dir: 존재하는 폴더여야 함")
    if cfg.journal_path.exists() and not cfg.journal_path.is_file():
        problems.append("journal_path: 파일이 아님")
    if _link_like(cfg.journal_path):
        problems.append("journal_path: symlink 는 허용하지 않음")

    dirs = [("scan_dir", cfg.scan_dir), ("done_dir", cfg.done_dir), ("backup_dir", cfg.backup_dir)]
    for i in range(len(dirs)):
        for j in range(i + 1, len(dirs)):
            why = _overlap(dirs[i][1], dirs[j][1])
            if why:
                problems.append(f"{dirs[i][0]}·{dirs[j][0]} 겹침({why})")
    # journal 은 세 폴더 어디의 안에도 있으면 안 된다(소스 폴더 밖, 처리 대상·결과물과도 분리)
    for n, d in dirs:
        if is_within(d, cfg.journal_path):
            problems.append(f"journal_path 가 {n} 안에 있음")
    if cfg.ocr_timeout_s <= 0 or cfg.stable_seconds < 0 or cfg.lease_seconds <= 0:
        problems.append("시간 설정은 양수여야 함(stable_seconds 는 0 이상)")
    if not cfg.ocr_langs or any(c not in "abcdefghijklmnopqrstuvwxyz_+" for c in cfg.ocr_langs):
        problems.append("ocr_langs 형식 오류(예: kor+eng)")
    if problems:
        raise ConfigError(problems)


def save(cfg: Config, path: Optional[Path] = None) -> Path:
    """검증 후 앱 데이터 경로(또는 `path`)에 원자적으로 저장."""
    validate(cfg)
    dest = Path(path) if path else config_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=".config.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cfg.to_json(), f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, dest)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return dest


def load(path: Optional[Path] = None, *, require_scan_exists: bool = True) -> Config:
    src = Path(path) if path else config_path()
    try:
        data = json.loads(src.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError([f"설정 파일 없음: {src.name} (scaninbox init 필요)"])
    except json.JSONDecodeError as e:
        raise ConfigError([f"설정 파일 형식 오류: {e.msg}"])
    if not isinstance(data, dict):
        raise ConfigError(["설정 파일 형식 오류: 객체가 아님"])
    cfg = Config.from_json(data)
    validate(cfg, require_scan_exists=require_scan_exists)
    return cfg
