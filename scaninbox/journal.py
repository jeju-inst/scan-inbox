"""SQLite journal: 상태 전이 강제, 단일 실행 잠금, 복구, ledger 내보내기 (core).

- journal 은 소스(스캔) 폴더 밖에 둔다(검증은 config.validate).
- `transition()` 한 번이 SQLite 트랜잭션 한 번이다. 상태·필드·이벤트가 함께 커밋되거나 함께 취소된다.
- 파일을 바꾸는 단계는 **먼저 journal 에 의도를 적고(PUBLISHING 등) → 파일 작업 → 결과를 적는** 순서로 하고,
  중간에 죽으면 `recover()` 가 실제 파일·해시와 대조해 다음 상태를 정한다. recover 는 입력·완료함 파일을
  삭제하지 않는다(정리는 파이프라인 몫). 여러 번 불러도 같은 결과(멱등)다.
- OCR 본문은 저장하지 않는다.

pipeline 이 지켜야 할 규약 (총괄 승인 2026-11-05):
1. Undo: 원본을 입력에 되돌린 뒤 `COMPLETED → DISCOVERED` 로만 재등록한다(UNDONE 상태는 없다).
2. 사용자 승인: `approved_by_user` 가 참이 아니면 `COMPLETED` 전이는 거절된다. 승인은 `APPROVED` 전이에
   `approved_by_user=True` 로 명시해야 한다.
3. recover 는 입력 파일을 지우지 않는다. 정리 대기 중 입력이 같은 내용으로 남아 있으면 `needs_source_cleanup`
   으로 알리기만 하므로, 입력 삭제 후 `COMPLETED` 로 올리는 것은 pipeline 의 일이다.
4. 기록 순서: `BACKED_UP` 전이에 `backup_rel`(backup_dir 기준 상대경로)을 기록한다. 최종본을 임시로 만들어
   검증해 해시를 안 뒤 `PUBLISHING` 전이에 `final_rel`(done_dir 기준)·`final_sha256` 을 함께 기록하고,
   그 다음에 파일을 배치(`pathsafe.move_no_overwrite`)한 뒤 `PUBLISHED` 로 기록한다.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Optional

from .contracts import State
from .pathsafe import collision_key

S = State

# 허용 전이표. 여기에 없는 전이는 IllegalTransition.
TRANSITIONS: dict[State, frozenset[State]] = {
    S.DISCOVERED: frozenset({S.WAITING_STABLE, S.BACKED_UP, S.HELD, S.FAILED}),
    S.WAITING_STABLE: frozenset({S.DISCOVERED, S.BACKED_UP, S.HELD, S.FAILED}),
    S.BACKED_UP: frozenset({S.DISCOVERED, S.EXTRACTED, S.HELD, S.FAILED}),
    # 백업 훼손 시 recover 가 DISCOVERED 로 되돌려 다시 보호한다
    S.EXTRACTED: frozenset({S.DISCOVERED, S.REVIEW_PENDING, S.HELD, S.FAILED}),
    S.REVIEW_PENDING: frozenset({S.DISCOVERED, S.APPROVED, S.HELD, S.FAILED}),
    S.APPROVED: frozenset({S.DISCOVERED, S.PUBLISHING, S.REVIEW_PENDING, S.HELD, S.FAILED}),
    S.PUBLISHING: frozenset({S.PUBLISHED, S.APPROVED, S.FAILED}),
    S.PUBLISHED: frozenset({S.SOURCE_CLEANUP_PENDING, S.FAILED}),
    S.SOURCE_CLEANUP_PENDING: frozenset({S.COMPLETED, S.HELD, S.FAILED}),
    S.COMPLETED: frozenset({S.DISCOVERED}),   # Undo 로 원본을 입력에 되돌린 뒤 다시 등록
    S.HELD: frozenset({S.DISCOVERED, S.REVIEW_PENDING}),
    S.FAILED: frozenset({S.DISCOVERED}),
}
REASON_REQUIRED = frozenset({S.HELD, S.FAILED})
TERMINAL_FOR_LEDGER = (S.COMPLETED, S.HELD, S.FAILED)

# transition(**fields) 로 갱신할 수 있는 열
_FIELDS = frozenset({"backup_rel", "final_rel", "final_sha256", "approved_by_user", "reason",
                     "size", "mtime_ns", "plan_json"})

SCHEMA_VERSION = 1
_DDL = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS files (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  input_rel TEXT NOT NULL,
  original_sha256 TEXT NOT NULL,
  size INTEGER,
  mtime_ns INTEGER,
  state TEXT NOT NULL,
  backup_rel TEXT,
  final_rel TEXT,
  final_sha256 TEXT,
  approved_by_user INTEGER NOT NULL DEFAULT 0,
  reason TEXT,
  plan_json TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  UNIQUE (input_rel, original_sha256)
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  file_id INTEGER NOT NULL REFERENCES files(id),
  at REAL NOT NULL,
  from_state TEXT,
  to_state TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS run_lock (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  owner TEXT NOT NULL,
  expires_at REAL NOT NULL
);
"""


class JournalError(RuntimeError):
    pass


class IllegalTransition(JournalError):
    pass


class LockHeld(JournalError):
    """다른 실행이 유효한 잠금을 쥐고 있음."""


class LockLost(JournalError):
    """내 잠금이 만료돼 다른 실행이 가져갔거나 해제됨. 더 이상 쓰면 안 된다."""


@dataclass(frozen=True)
class FileRecord:
    id: int
    input_rel: str
    original_sha256: str
    size: Optional[int]
    mtime_ns: Optional[int]
    state: State
    backup_rel: Optional[str]
    final_rel: Optional[str]
    final_sha256: Optional[str]
    approved_by_user: bool
    reason: Optional[str]
    plan_json: Optional[str]


@dataclass(frozen=True)
class RecoveryAction:
    file_id: int
    input_rel: str
    from_state: State
    to_state: State
    action: str      # 예: published_verified, rollback_to_approved, needs_source_cleanup, held_*
    detail: str = ""


def sha256_file(path: Path | str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _row(r: sqlite3.Row) -> FileRecord:
    return FileRecord(
        id=r["id"], input_rel=r["input_rel"], original_sha256=r["original_sha256"], size=r["size"],
        mtime_ns=r["mtime_ns"], state=State(r["state"]), backup_rel=r["backup_rel"], final_rel=r["final_rel"],
        final_sha256=r["final_sha256"], approved_by_user=bool(r["approved_by_user"]), reason=r["reason"],
        plan_json=r["plan_json"],
    )


class Journal:
    def __init__(self, path: Path | str, *, clock: Callable[[], float] = time.time,
                 fault: Optional[Callable[[str], None]] = None):
        """`fault(point)` 는 장애 주입용 훅. 점 이름: `before_commit`, `after_commit`. 예외를 던지면 그 지점에서 중단한다."""
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self.fault = fault
        self._owner: Optional[str] = None
        self._lease_s = 300.0
        self._db = sqlite3.connect(str(self.path), isolation_level=None, timeout=30)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.executescript(_DDL)
        self._db.execute("INSERT OR IGNORE INTO meta(k, v) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
        ver = int(self._db.execute("SELECT v FROM meta WHERE k='schema_version'").fetchone()["v"])
        if ver != SCHEMA_VERSION:
            raise JournalError(f"지원하지 않는 journal 버전: {ver}")

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> "Journal":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------ 트랜잭션

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        self._db.execute("BEGIN IMMEDIATE")
        try:
            self._check_lock_still_mine()
            yield self._db
            if self.fault:
                self.fault("before_commit")
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        self._db.execute("COMMIT")
        if self.fault:
            self.fault("after_commit")

    # ------------------------------------------------------------ 잠금

    def acquire_lock(self, owner: Optional[str] = None, *, lease_s: float = 300.0) -> str:
        """단일 실행 잠금. 다른 소유자의 잠금이 유효하면 LockHeld. 만료됐으면 가져온다."""
        owner = owner or uuid.uuid4().hex
        now = self._clock()
        self._db.execute("BEGIN IMMEDIATE")
        try:
            cur = self._db.execute("SELECT owner, expires_at FROM run_lock WHERE id=1").fetchone()
            if cur and cur["owner"] != owner and cur["expires_at"] > now:
                raise LockHeld("다른 실행이 진행 중입니다(임대 만료 전)")
            self._db.execute(
                "INSERT INTO run_lock(id, owner, expires_at) VALUES (1, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET owner=excluded.owner, expires_at=excluded.expires_at",
                (owner, now + lease_s))
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        self._db.execute("COMMIT")
        self._owner, self._lease_s = owner, lease_s
        return owner

    def renew_lock(self) -> None:
        """임대 연장. 내 잠금이 아니면 LockLost."""
        if self._owner is None:
            raise LockLost("잠금을 잡지 않았음")
        now = self._clock()
        with self._tx() as db:
            db.execute("UPDATE run_lock SET expires_at=? WHERE id=1 AND owner=?", (now + self._lease_s, self._owner))

    def release_lock(self) -> None:
        if self._owner is None:
            return
        self._db.execute("DELETE FROM run_lock WHERE id=1 AND owner=?", (self._owner,))
        self._owner = None

    @contextmanager
    def run_lock(self, *, lease_s: float = 300.0) -> Iterator[str]:
        owner = self.acquire_lock(lease_s=lease_s)
        try:
            yield owner
        finally:
            self.release_lock()

    def _check_lock_still_mine(self) -> None:
        if self._owner is None:
            return
        cur = self._db.execute("SELECT owner, expires_at FROM run_lock WHERE id=1").fetchone()
        if not cur or cur["owner"] != self._owner:
            raise LockLost("잠금을 잃었음(만료 후 다른 실행이 가져감)")
        # 만료됐어도 아무도 가져가지 않았다면 계속 유효로 본다(가져가는 쪽이 acquire 에서 판단).

    # ------------------------------------------------------------ 기록

    def register(self, input_rel: str, original_sha256: str, *, size: Optional[int] = None,
                 mtime_ns: Optional[int] = None) -> FileRecord:
        """입력 파일 등록(DISCOVERED). 같은 (경로, 해시)가 이미 있으면 그 기록을 돌려준다(멱등)."""
        if len(original_sha256) != 64 or any(c not in "0123456789abcdef" for c in original_sha256):
            raise JournalError("SHA-256 형식 오류")
        now = self._clock()
        with self._tx() as db:
            row = db.execute("SELECT * FROM files WHERE input_rel=? AND original_sha256=?",
                             (input_rel, original_sha256)).fetchone()
            if row is None:
                cur = db.execute(
                    "INSERT INTO files(input_rel, original_sha256, size, mtime_ns, state, created_at, updated_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (input_rel, original_sha256, size, mtime_ns, S.DISCOVERED.value, now, now))
                fid = cur.lastrowid
                db.execute("INSERT INTO events(file_id, at, from_state, to_state, note) VALUES (?,?,?,?,?)",
                           (fid, now, None, S.DISCOVERED.value, "register"))
                row = db.execute("SELECT * FROM files WHERE id=?", (fid,)).fetchone()
        return _row(row)

    def transition(self, file_id: int, to: State, *, note: str = "", **fields) -> FileRecord:
        """전이표를 검사하고 상태·필드·이벤트를 한 트랜잭션으로 기록한다."""
        to = State(to)
        bad = set(fields) - _FIELDS
        if bad:
            raise JournalError(f"갱신할 수 없는 필드: {sorted(bad)}")
        now = self._clock()
        with self._tx() as db:
            row = db.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
            if row is None:
                raise JournalError(f"없는 file_id: {file_id}")
            frm = State(row["state"])
            if to not in TRANSITIONS[frm]:
                raise IllegalTransition(f"{frm.value} → {to.value} 는 허용되지 않음")
            if to in REASON_REQUIRED and not (fields.get("reason") or "").strip():
                raise IllegalTransition(f"{to.value} 로 가려면 reason 이 필요함")
            if to is S.PUBLISHING and not (fields.get("final_rel") or row["final_rel"]) :
                raise IllegalTransition("publishing 은 final_rel(예정 경로)을 먼저 기록해야 함")
            if to is S.PUBLISHING and not (fields.get("final_sha256") or row["final_sha256"]):
                raise IllegalTransition("publishing 은 final_sha256 을 먼저 기록해야 함")
            if to is S.APPROVED and "approved_by_user" not in fields:
                raise IllegalTransition("approved 는 approved_by_user 를 명시해야 함")
            if to is S.COMPLETED and not (fields.get("approved_by_user", bool(row["approved_by_user"]))):
                raise IllegalTransition("사용자 승인 없이 completed 로 갈 수 없음(G1: 자동 완료 없음)")
            sets = {"state": to.value, "updated_at": now}
            for k, v in fields.items():
                sets[k] = int(v) if k == "approved_by_user" else v
            if to in (S.DISCOVERED,):   # 재시도·Undo 후 재등록: 이전 결과 흔적을 비운다
                sets.update(final_rel=None, final_sha256=None, approved_by_user=0, plan_json=None)
                if "reason" not in fields:
                    sets["reason"] = None
            elif to not in REASON_REQUIRED and "reason" not in fields:
                sets["reason"] = None
            cols = ", ".join(f"{k}=?" for k in sets)
            db.execute(f"UPDATE files SET {cols} WHERE id=?", (*sets.values(), file_id))
            db.execute("INSERT INTO events(file_id, at, from_state, to_state, note) VALUES (?,?,?,?,?)",
                       (file_id, now, frm.value, to.value, note))
            row = db.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
        return _row(row)

    # ------------------------------------------------------------ 조회

    def get(self, file_id: int) -> FileRecord:
        r = self._db.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
        if r is None:
            raise JournalError(f"없는 file_id: {file_id}")
        return _row(r)

    def list(self, state: Optional[State] = None) -> list[FileRecord]:
        if state is None:
            rows = self._db.execute("SELECT * FROM files ORDER BY id").fetchall()
        else:
            rows = self._db.execute("SELECT * FROM files WHERE state=? ORDER BY id", (State(state).value,)).fetchall()
        return [_row(r) for r in rows]

    def history(self, file_id: int) -> list[tuple[Optional[str], str, str]]:
        rows = self._db.execute("SELECT from_state, to_state, note FROM events WHERE file_id=? ORDER BY id",
                                (file_id,)).fetchall()
        return [(r["from_state"], r["to_state"], r["note"]) for r in rows]

    # ------------------------------------------------------------ 복구

    def recover(self, *, input_dir: Path | str, done_dir: Path | str, backup_dir: Path | str,
                hash_fn: Callable[[Path], str] = sha256_file) -> list[RecoveryAction]:
        """실제 파일·해시와 journal 을 대조해 중간 상태를 정리한다. 파일은 지우거나 덮어쓰지 않는다.

        - PUBLISHING: 최종본이 있고 해시가 맞으면 PUBLISHED. 없으면 APPROVED 로 되돌림(중복 생성 없음).
          같은 짧은 ID·해시의 최종본이 다른 이름으로 있으면 그것을 채택. 해시가 다른 파일이 그 자리에 있으면 FAILED.
        - PUBLISHED: 최종본 검증 후 SOURCE_CLEANUP_PENDING. 최종본이 사라졌으면 FAILED.
        - SOURCE_CLEANUP_PENDING: 입력이 이미 없고 백업·최종본이 검증되면 COMPLETED. 입력이 아직 같은 내용으로
          있으면 그대로 두고 `needs_source_cleanup` 으로 알린다(삭제는 파이프라인). 입력이 바뀌었거나 백업·최종본이
          없으면 HELD.
        - BACKED_UP~APPROVED: 백업이 없거나 해시가 다르면 입력이 원본 그대로일 때 DISCOVERED(다시 보호),
          아니면 FAILED. 입력이 바뀌었으면 HELD.
        """
        in_dir, dn_dir, bk_dir = Path(input_dir), Path(done_dir), Path(backup_dir)
        actions: list[RecoveryAction] = []

        def act(rec: FileRecord, to: State, action: str, detail: str = "", **fields) -> None:
            if to != rec.state:
                self.transition(rec.id, to, note=f"recover:{action}", **fields)
            actions.append(RecoveryAction(rec.id, rec.input_rel, rec.state, to, action, detail))

        def file_hash(p: Path) -> Optional[str]:
            return hash_fn(p) if p.is_file() else None

        def backup_ok(rec: FileRecord) -> bool:
            return bool(rec.backup_rel) and file_hash(bk_dir / rec.backup_rel) == rec.original_sha256

        def input_state(rec: FileRecord) -> str:
            h = file_hash(in_dir / rec.input_rel)
            return "missing" if h is None else ("same" if h == rec.original_sha256 else "changed")

        def find_final(rec: FileRecord) -> Optional[str]:
            """기록된 final_rel, 없으면 같은 짧은 ID 와 해시의 최종본을 같은 폴더에서 찾는다."""
            if rec.final_rel and file_hash(dn_dir / rec.final_rel) == rec.final_sha256:
                return rec.final_rel
            if rec.final_rel and (dn_dir / rec.final_rel).exists():
                return None  # 같은 이름에 다른 내용: 호출 쪽에서 구분한다
            if rec.final_sha256 and rec.final_rel:
                parent = (dn_dir / rec.final_rel).parent
                if parent.is_dir():
                    tag = f"__{rec.original_sha256[:8]}"
                    for n in sorted(os.listdir(parent)):
                        if tag in n and n.lower().endswith(".pdf") and file_hash(parent / n) == rec.final_sha256:
                            return (Path(rec.final_rel).parent / n).as_posix()
            return None

        def process(rec: FileRecord) -> None:
            st = rec.state
            if st is S.PUBLISHING:
                found = find_final(rec)
                if found:
                    act(rec, S.PUBLISHED, "published_verified", found, final_rel=found)
                elif rec.final_rel and (dn_dir / rec.final_rel).exists():
                    act(rec, S.FAILED, "final_hash_mismatch", "최종 경로에 해시가 다른 파일이 있음(삭제하지 않음)",
                        reason="최종 경로에 내용이 다른 파일이 있어 배치를 확정하지 못함")
                else:
                    act(rec, S.APPROVED, "rollback_to_approved", "최종본 없음: 승인 상태로 되돌림", approved_by_user=bool(rec.approved_by_user))
            elif st is S.PUBLISHED:
                found = find_final(rec)
                if found:
                    act(rec, S.SOURCE_CLEANUP_PENDING, "to_source_cleanup", found, final_rel=found)
                else:
                    act(rec, S.FAILED, "final_missing", "published 인데 최종본이 없음",
                        reason="최종본이 사라졌거나 해시가 달라짐(백업은 보존)")
            elif st is S.SOURCE_CLEANUP_PENDING:
                inp = input_state(rec)
                found = find_final(rec)
                if not backup_ok(rec) or not found:
                    act(rec, S.HELD, "held_unverified", "백업 또는 최종본을 검증하지 못함(입력은 건드리지 않음)",
                        reason="입력 정리 전 백업·최종본 검증 실패")
                elif inp == "changed":
                    act(rec, S.HELD, "held_input_changed", "정리 대기 중 입력이 바뀜",
                        reason="입력 정리 대기 중 입력 내용이 바뀌어 보류")
                elif not rec.approved_by_user:
                    act(rec, S.HELD, "held_unapproved", "승인 기록 없이 정리 단계에 도달함",
                        reason="사용자 승인 기록이 없어 completed 로 확정하지 않음")
                elif inp == "missing":
                    act(rec, S.COMPLETED, "completed_verified", found, final_rel=found)
                else:
                    actions.append(RecoveryAction(rec.id, rec.input_rel, st, st, "needs_source_cleanup",
                                                  "입력이 아직 있음: 파이프라인이 정리 후 completed"))
            elif st in (S.BACKED_UP, S.EXTRACTED, S.REVIEW_PENDING, S.APPROVED):
                inp = input_state(rec)
                if inp == "changed":
                    act(rec, S.HELD, "held_input_changed", "입력이 원본과 다름",
                        reason="입력 내용이 등록 시점과 달라 보류")
                elif not backup_ok(rec):
                    if inp == "same":
                        act(rec, S.DISCOVERED, "reprotect", "백업이 없거나 훼손: 다시 보호")
                    else:
                        act(rec, S.FAILED, "backup_lost", "백업도 입력도 원본이 아님",
                            reason="원본 백업과 입력 모두 확인되지 않음")
            elif st is S.COMPLETED:
                if not find_final(rec):
                    actions.append(RecoveryAction(rec.id, rec.input_rel, st, st, "warn_final_missing",
                                                  "completed 인데 최종본이 없거나 달라짐"))

        for rec in self.list():
            # PUBLISHING → PUBLISHED → SOURCE_CLEANUP_PENDING 는 같은 호출에서 이어서 검증한다(멱등 보장)
            for _ in range(3):
                before = rec.state
                process(rec)
                rec = self.get(rec.id)
                if rec.state is before or rec.state not in (S.PUBLISHED, S.SOURCE_CLEANUP_PENDING):
                    break
        return actions

    # ------------------------------------------------------------ ledger

    def export_ledger(self, path: Optional[Path | str] = None) -> dict:
        """reconcile 호환 ledger(`scaninbox_corpus/schema/ledger.schema.json`). 종결 상태만 담는다.

        completed 는 해시·최종 경로·승인 여부를, held·failed 는 reason 을 담는다. 진행 중 상태는 ledger 에 없고
        reconcile 이 '미착수'로 센다. 같은 입력에 기록이 여러 개면(재시도·Undo) 가장 최근 기록 하나만 낸다.
        """
        latest: dict[str, FileRecord] = {}
        for rec in self.list():
            latest[rec.input_rel] = rec  # id 오름차순이므로 마지막이 최신
        entries = []
        for rel, rec in latest.items():
            if rec.state not in TERMINAL_FOR_LEDGER:
                continue
            e: dict = {"input": rel, "status": rec.state.value, "original_sha256": rec.original_sha256}
            if rec.state is S.COMPLETED:
                e.update(final_path=rec.final_rel, final_sha256=rec.final_sha256,
                         approved_by_user=rec.approved_by_user)
            else:
                e["reason"] = rec.reason or "사유 미기록"
            entries.append(e)
        ledger = {"entries": sorted(entries, key=lambda x: collision_key(x["input"]))}
        if path is not None:
            p = Path(path)
            tmp = p.with_name(p.name + ".tmp")
            tmp.write_text(json.dumps(ledger, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, p)
        return ledger
