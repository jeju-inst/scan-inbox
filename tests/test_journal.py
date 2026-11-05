import hashlib
import itertools
import json
from pathlib import Path

import pytest

from scaninbox.contracts import State as S
from scaninbox import journal as J

SCHEMA = Path(J.__file__).resolve().parent.parent / "scaninbox_corpus" / "schema" / "ledger.schema.json"


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


class Env:
    """입력·백업·완료함 폴더와 journal 을 가진 시험 환경."""

    def __init__(self, tmp: Path, **jkw):
        self.inp, self.done, self.bak = tmp / "in", tmp / "done", tmp / "bak"
        for d in (self.inp, self.done, self.bak):
            d.mkdir()
        self.jpath = tmp / "app" / "journal.sqlite"
        self.j = J.Journal(self.jpath, **jkw)

    def reopen(self, **jkw):
        """프로세스 재시작을 흉내: 연결을 닫고 새로 연다(잠금은 그대로 DB 에 남는다)."""
        self.j.close()
        self.j = J.Journal(self.jpath, **jkw)
        return self.j

    def add(self, rel="a.pdf", data=b"orig"):
        (self.inp / rel).parent.mkdir(parents=True, exist_ok=True)
        (self.inp / rel).write_bytes(data)
        return self.j.register(rel, sha(data), size=len(data), mtime_ns=1)

    def protect(self, rec):
        (self.bak / rec.original_sha256).write_bytes((self.inp / rec.input_rel).read_bytes())
        return self.j.transition(rec.id, S.BACKED_UP, backup_rel=rec.original_sha256)

    def to_approved(self, rec, user=True):
        rec = self.protect(rec)
        rec = self.j.transition(rec.id, S.EXTRACTED)
        rec = self.j.transition(rec.id, S.REVIEW_PENDING)
        return self.j.transition(rec.id, S.APPROVED, approved_by_user=user)

    def final_bytes(self):
        return b"final-with-text-layer"

    def to_publishing(self, rec, name=None, write=False):
        name = name or f"2026-11-05_x__{rec.original_sha256[:8]}.pdf"
        fb = self.final_bytes()
        rec = self.j.transition(rec.id, S.PUBLISHING, final_rel=name, final_sha256=sha(fb))
        if write:
            (self.done / name).write_bytes(fb)
        return rec


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.j.close()


def recover(env):
    return env.j.recover(input_dir=env.inp, done_dir=env.done, backup_dir=env.bak)


# ------------------------------------------------------------ 전이표

def test_transition_table_is_total_and_consistent():
    assert set(J.TRANSITIONS) == set(S)
    for frm, tos in J.TRANSITIONS.items():
        assert frm not in tos
        assert tos <= set(S)
    # 완료 경로는 승인 단계를 거쳐야만 닿는다
    assert S.COMPLETED not in J.TRANSITIONS[S.REVIEW_PENDING]
    assert S.COMPLETED not in J.TRANSITIONS[S.APPROVED]
    assert S.PUBLISHING not in J.TRANSITIONS[S.REVIEW_PENDING]


def test_every_disallowed_transition_raises(env):
    """전이표 밖의 모든 (from, to) 쌍이 IllegalTransition 이다."""
    for frm, to in itertools.product(S, S):
        if to in J.TRANSITIONS[frm]:
            continue
        rec = env.add(f"{frm.value}_{to.value}.pdf", f"{frm}{to}".encode())
        _force_state(env, rec.id, frm)
        with pytest.raises(J.IllegalTransition):
            env.j.transition(rec.id, to, reason="r", approved_by_user=True,
                             final_rel="x", final_sha256="0" * 64)
        assert env.j.get(rec.id).state is frm


def _force_state(env, fid, state):
    env.j._db.execute("UPDATE files SET state=?, approved_by_user=1 WHERE id=?", (state.value, fid))


def test_happy_path_to_completed_and_history(env):
    rec = env.to_approved(env.add())
    rec = env.to_publishing(rec, write=True)
    rec = env.j.transition(rec.id, S.PUBLISHED)
    rec = env.j.transition(rec.id, S.SOURCE_CLEANUP_PENDING)
    rec = env.j.transition(rec.id, S.COMPLETED)
    assert rec.state is S.COMPLETED and rec.approved_by_user
    h = [t for _, t, _ in env.j.history(rec.id)]
    assert h == ["discovered", "backed_up", "extracted", "review_pending", "approved", "publishing",
                 "published", "source_cleanup_pending", "completed"]


def test_reason_required_for_held_and_failed(env):
    rec = env.add()
    for to in (S.HELD, S.FAILED):
        with pytest.raises(J.IllegalTransition, match="reason"):
            env.j.transition(rec.id, to)
        with pytest.raises(J.IllegalTransition, match="reason"):
            env.j.transition(rec.id, to, reason="  ")
    assert env.j.transition(rec.id, S.HELD, reason="암호화").reason == "암호화"


def test_publishing_requires_final_path_and_hash_first(env):
    rec = env.to_approved(env.add())
    with pytest.raises(J.IllegalTransition, match="final_rel"):
        env.j.transition(rec.id, S.PUBLISHING)
    with pytest.raises(J.IllegalTransition, match="final_sha256"):
        env.j.transition(rec.id, S.PUBLISHING, final_rel="x.pdf")


def test_no_completion_without_user_approval(env):
    rec = env.to_approved(env.add(), user=False)
    rec = env.to_publishing(rec, write=True)
    rec = env.j.transition(rec.id, S.PUBLISHED)
    rec = env.j.transition(rec.id, S.SOURCE_CLEANUP_PENDING)
    with pytest.raises(J.IllegalTransition, match="승인"):
        env.j.transition(rec.id, S.COMPLETED)


def test_approved_requires_explicit_flag(env):
    rec = env.add()
    rec = env.protect(rec)
    rec = env.j.transition(rec.id, S.EXTRACTED)
    rec = env.j.transition(rec.id, S.REVIEW_PENDING)
    with pytest.raises(J.IllegalTransition):
        env.j.transition(rec.id, S.APPROVED)


def test_unknown_field_and_unknown_id(env):
    rec = env.add()
    with pytest.raises(J.JournalError):
        env.j.transition(rec.id, S.BACKED_UP, state="completed")
    with pytest.raises(J.JournalError):
        env.j.transition(9999, S.BACKED_UP)


def test_register_idempotent_and_new_content_new_record(env):
    a = env.add("x.pdf", b"one")
    assert env.j.register("x.pdf", sha(b"one")).id == a.id
    b = env.j.register("x.pdf", sha(b"two"))
    assert b.id != a.id
    with pytest.raises(J.JournalError):
        env.j.register("y.pdf", "nothex")


def test_retry_clears_previous_result_traces(env):
    rec = env.add()
    rec = env.j.transition(rec.id, S.FAILED, reason="엔진 오류")
    rec = env.j.transition(rec.id, S.DISCOVERED)
    assert rec.reason is None and rec.final_rel is None and not rec.approved_by_user


# ------------------------------------------------------------ 영속화·원자성·장애 주입

def test_state_persists_across_reopen(env):
    rec = env.to_approved(env.add())
    j2 = env.reopen()
    assert j2.get(rec.id).state is S.APPROVED and j2.get(rec.id).approved_by_user


def test_crash_before_commit_leaves_state_unchanged(tmp_path):
    def boom(point):
        if point == "before_commit":
            raise RuntimeError("전원 차단")
    e = Env(tmp_path)
    rec = e.add()
    e.j.fault = boom
    with pytest.raises(RuntimeError):
        e.protect(rec)
    e.j.fault = None
    j = e.reopen()
    r = j.get(rec.id)
    assert r.state is S.DISCOVERED and r.backup_rel is None
    assert [t for _, t, _ in j.history(rec.id)] == ["discovered"]   # 이벤트도 같이 취소
    e.j.close()


def test_crash_after_commit_state_is_durable(tmp_path):
    def boom(point):
        if point == "after_commit":
            raise RuntimeError("커밋 직후 중단")
    e = Env(tmp_path)
    rec = e.add()
    (e.bak / rec.original_sha256).write_bytes(b"orig")
    e.j.fault = boom
    with pytest.raises(RuntimeError):
        e.j.transition(rec.id, S.BACKED_UP, backup_rel=rec.original_sha256)
    e.j.fault = None
    assert e.reopen().get(rec.id).state is S.BACKED_UP
    e.j.close()


# ------------------------------------------------------------ 잠금

def test_lock_exclusive_and_lease_expiry(tmp_path):
    t = [1000.0]
    e = Env(tmp_path, clock=lambda: t[0])
    other = J.Journal(e.jpath, clock=lambda: t[0])
    e.j.acquire_lock("A", lease_s=60)
    with pytest.raises(J.LockHeld):
        other.acquire_lock("B")
    t[0] += 61                                  # 임대 만료
    other.acquire_lock("B", lease_s=60)
    rec = other.register("a.pdf", sha(b"x"))   # B 는 쓸 수 있다
    with pytest.raises(J.LockLost):             # 잠금을 뺏긴 A 는 쓰면 안 된다
        e.j.register("b.pdf", sha(b"y"))
    with pytest.raises(J.LockLost):
        e.j.transition(rec.id, S.BACKED_UP, backup_rel="x")
    assert other.get(rec.id).state is S.DISCOVERED
    other.close(); e.j.close()


def test_lock_renew_release_and_reacquire_same_owner(tmp_path):
    t = [0.0]
    e = Env(tmp_path, clock=lambda: t[0])
    e.j.acquire_lock("A", lease_s=10)
    t[0] += 9; e.j.renew_lock()
    t[0] += 9
    other = J.Journal(e.jpath, clock=lambda: t[0])
    with pytest.raises(J.LockHeld):
        other.acquire_lock("B")                 # 연장돼서 아직 유효
    assert e.j.acquire_lock("A") == "A"         # 같은 소유자 재획득
    e.j.release_lock()
    other.acquire_lock("B")
    other.release_lock()
    other.close(); e.j.close()


def test_run_lock_contextmanager_releases_on_error(env):
    with pytest.raises(ZeroDivisionError):
        with env.j.run_lock():
            1 / 0
    j2 = J.Journal(env.jpath)
    j2.acquire_lock("X")
    j2.close()


def test_crashed_run_lock_blocks_until_lease_expires(tmp_path):
    t = [0.0]
    e = Env(tmp_path, clock=lambda: t[0])
    e.j.acquire_lock("dead", lease_s=30)
    e.j.close()                                  # 해제 없이 죽음
    j = J.Journal(e.jpath, clock=lambda: t[0])
    with pytest.raises(J.LockHeld):
        j.acquire_lock("new")
    t[0] += 31
    j.acquire_lock("new")
    j.close()


# ------------------------------------------------------------ recover

def test_recover_publishing_with_verified_final_goes_published(env):
    rec = env.to_publishing(env.to_approved(env.add()), write=True)
    acts = recover(env)
    assert [(a.action, a.to_state) for a in acts] == [
        ("published_verified", S.PUBLISHED), ("to_source_cleanup", S.SOURCE_CLEANUP_PENDING)]
    assert env.j.get(rec.id).state is S.SOURCE_CLEANUP_PENDING


def test_recover_publishing_without_final_rolls_back_and_makes_no_duplicate(env):
    rec = env.to_publishing(env.to_approved(env.add()), write=False)
    acts = recover(env)
    assert acts[0].action == "rollback_to_approved"
    r = env.j.get(rec.id)
    assert r.state is S.APPROVED and r.approved_by_user
    assert list(env.done.iterdir()) == []            # 최종본을 만들지 않았다
    # 재개: 다시 PUBLISHING → 정상 완료
    env.to_publishing(r, write=True)
    assert recover(env)[0].to_state is S.PUBLISHED
    assert env.j.get(rec.id).state is S.SOURCE_CLEANUP_PENDING
    assert len(list(env.done.iterdir())) == 1


def test_recover_publishing_adopts_same_hash_final_under_other_name(env):
    rec = env.add()
    rec = env.to_publishing(env.to_approved(rec), name="2026-11-05_x__%s.pdf" % rec.original_sha256[:8])
    other = env.done / ("2026-11-05_x__%s_2.pdf" % rec.original_sha256[:8])
    other.write_bytes(env.final_bytes())
    a = recover(env)[0]
    assert a.action == "published_verified"
    assert env.j.get(rec.id).final_rel == other.name
    assert len(list(env.done.iterdir())) == 1


def test_recover_publishing_with_foreign_file_at_final_path_fails_without_deleting(env):
    rec = env.to_publishing(env.to_approved(env.add()))
    foreign = env.done / env.j.get(rec.id).final_rel
    foreign.write_bytes(b"someone else")
    a = recover(env)[0]
    assert a.action == "final_hash_mismatch" and env.j.get(rec.id).state is S.FAILED
    assert foreign.read_bytes() == b"someone else"


def test_recover_published_missing_final_fails_but_keeps_backup(env):
    rec = env.to_publishing(env.to_approved(env.add()), write=True)
    env.j.transition(rec.id, S.PUBLISHED)
    (env.done / env.j.get(rec.id).final_rel).unlink()
    assert recover(env)[0].action == "final_missing"
    assert env.j.get(rec.id).state is S.FAILED
    assert (env.bak / rec.original_sha256).exists()


def _to_cleanup_pending(env, user=True):
    rec = env.to_publishing(env.to_approved(env.add(), user=user), write=True)
    env.j.transition(rec.id, S.PUBLISHED)
    return env.j.transition(rec.id, S.SOURCE_CLEANUP_PENDING)


def test_recover_cleanup_pending_input_still_there_asks_for_cleanup(env):
    rec = _to_cleanup_pending(env)
    a = recover(env)[0]
    assert a.action == "needs_source_cleanup" and a.to_state is S.SOURCE_CLEANUP_PENDING
    assert (env.inp / rec.input_rel).exists()        # recover 는 입력을 지우지 않는다
    assert env.j.get(rec.id).state is S.SOURCE_CLEANUP_PENDING


def test_recover_cleanup_pending_input_gone_completes(env):
    rec = _to_cleanup_pending(env)
    (env.inp / rec.input_rel).unlink()               # 정리 직후·journal 기록 직전에 죽은 상황
    a = recover(env)[0]
    assert a.action == "completed_verified"
    assert env.j.get(rec.id).state is S.COMPLETED
    led = env.j.export_ledger()["entries"]
    assert led[0]["status"] == "completed" and led[0]["approved_by_user"] is True


def test_recover_cleanup_pending_never_completes_without_backup(env):
    rec = _to_cleanup_pending(env)
    (env.inp / rec.input_rel).unlink()
    (env.bak / rec.original_sha256).unlink()
    a = recover(env)[0]
    assert a.action == "held_unverified" and env.j.get(rec.id).state is S.HELD


def test_recover_cleanup_pending_input_changed_holds(env):
    rec = _to_cleanup_pending(env)
    (env.inp / rec.input_rel).write_bytes(b"edited meanwhile")
    assert recover(env)[0].action == "held_input_changed"
    assert (env.inp / rec.input_rel).read_bytes() == b"edited meanwhile"


def test_recover_unapproved_never_completes(env):
    rec = _to_cleanup_pending(env, user=False)
    (env.inp / rec.input_rel).unlink()
    assert recover(env)[0].action == "held_unapproved"
    assert env.j.get(rec.id).state is S.HELD


def test_recover_early_states_reprotect_or_fail_or_hold(env):
    a = env.protect(env.add("a.pdf", b"aa"))
    (env.bak / a.original_sha256).write_bytes(b"corrupt")          # 백업 훼손, 입력은 원본
    b = env.protect(env.add("b.pdf", b"bb"))
    (env.bak / b.original_sha256).unlink()
    (env.inp / "b.pdf").unlink()                                    # 백업도 입력도 없음
    c = env.protect(env.add("c.pdf", b"cc"))
    (env.inp / "c.pdf").write_bytes(b"changed")                     # 입력 변경
    d = env.to_approved(env.add("d.pdf", b"dd"))                    # 정상: 변화 없음
    acts = {x.input_rel: x.action for x in recover(env)}
    assert acts == {"a.pdf": "reprotect", "b.pdf": "backup_lost", "c.pdf": "held_input_changed"}
    st = {r.input_rel: r.state for r in env.j.list()}
    assert st == {"a.pdf": S.DISCOVERED, "b.pdf": S.FAILED, "c.pdf": S.HELD, "d.pdf": S.APPROVED}


def test_recover_is_idempotent(env):
    env.to_publishing(env.to_approved(env.add("a.pdf", b"a")), write=True)
    env.to_publishing(env.to_approved(env.add("b.pdf", b"b")), write=False)
    first = recover(env)
    assert len(first) == 3
    second = recover(env)
    # 두 번째는 상태를 바꾸지 않는다(정보성 알림 needs_source_cleanup 만 반복)
    assert all(a.from_state == a.to_state and a.action == "needs_source_cleanup" for a in second)
    states = {r.input_rel: r.state for r in env.j.list()}
    assert states == {"a.pdf": S.SOURCE_CLEANUP_PENDING, "b.pdf": S.APPROVED}


def test_recover_warns_when_completed_final_vanished(env):
    rec = _to_cleanup_pending(env)
    (env.inp / rec.input_rel).unlink()
    recover(env)
    (env.done / env.j.get(rec.id).final_rel).unlink()
    a = recover(env)
    assert [x.action for x in a] == ["warn_final_missing"]
    assert env.j.get(rec.id).state is S.COMPLETED    # 상태는 바꾸지 않고 알리기만


def test_full_crash_resume_cycle_after_reopen(tmp_path):
    """장애 주입: publishing 기록 후 최종 배치 전에 프로세스가 죽었다가 재시작해 복구·완료."""
    t = [100.0]
    e = Env(tmp_path, clock=lambda: t[0])
    rec = e.to_approved(e.add())
    e.j.acquire_lock("run1", lease_s=5)
    e.to_publishing(rec, write=False)
    e.j.close()                                      # 크래시
    j = e.reopen(clock=lambda: t[0])
    t[0] += 10                                       # 임대 만료 후 재실행
    j.acquire_lock("run2")
    assert recover(e)[0].action == "rollback_to_approved"
    r = j.get(rec.id)
    final = e.done / f"2026-11-05_x__{r.original_sha256[:8]}.pdf"
    j.transition(r.id, S.PUBLISHING, final_rel=final.name, final_sha256=sha(e.final_bytes()))
    final.write_bytes(e.final_bytes())
    j.transition(r.id, S.PUBLISHED)
    j.transition(r.id, S.SOURCE_CLEANUP_PENDING)
    (e.inp / r.input_rel).unlink()
    j.transition(r.id, S.COMPLETED)
    assert [p.name for p in e.done.iterdir()] == [final.name]
    e.j.close()


# ------------------------------------------------------------ ledger

def test_ledger_matches_schema_and_content(env):
    jsonschema = pytest.importorskip("jsonschema")
    done = env.to_publishing(env.to_approved(env.add("done.pdf", b"d")), write=True)
    env.j.transition(done.id, S.PUBLISHED)
    env.j.transition(done.id, S.SOURCE_CLEANUP_PENDING)
    env.j.transition(done.id, S.COMPLETED)
    held = env.add("held.pdf", b"h")
    env.j.transition(held.id, S.HELD, reason="암호화 PDF")
    failed = env.add("bad.pdf", b"f")
    env.j.transition(failed.id, S.FAILED, reason="손상")
    env.to_approved(env.add("inflight.pdf", b"i"))   # 진행 중은 ledger 에 없다
    out = env.jpath.parent / "ledger.json"
    led = env.j.export_ledger(out)
    jsonschema.validate(led, json.loads(SCHEMA.read_text(encoding="utf-8")))
    assert json.loads(out.read_text(encoding="utf-8")) == led
    by = {e["input"]: e for e in led["entries"]}
    assert set(by) == {"done.pdf", "held.pdf", "bad.pdf"}
    assert by["done.pdf"]["final_path"].startswith("2026-11-05_x__")
    assert by["done.pdf"]["final_sha256"] == sha(env.final_bytes())
    assert by["done.pdf"]["approved_by_user"] is True
    assert by["held.pdf"] == {"input": "held.pdf", "status": "held", "original_sha256": sha(b"h"), "reason": "암호화 PDF"}


def test_ledger_uses_latest_record_per_input(env):
    a = env.add("x.pdf", b"one")
    env.j.transition(a.id, S.FAILED, reason="1차 실패")
    b = env.j.register("x.pdf", sha(b"two"))         # 내용이 바뀐 재등록
    env.j.transition(b.id, S.HELD, reason="2차 보류")
    e = env.j.export_ledger()["entries"]
    assert len(e) == 1 and e[0]["status"] == "held" and e[0]["original_sha256"] == sha(b"two")


def test_ledger_completed_entry_has_fields_reconcile_reads(tmp_path):
    e = Env(tmp_path)
    data = b"%PDF-orig"
    rec = e.to_approved(e.add("doc.pdf", data))
    e.to_publishing(rec, write=True)
    e.j.transition(rec.id, S.PUBLISHED)
    e.j.transition(rec.id, S.SOURCE_CLEANUP_PENDING)
    (e.inp / "doc.pdf").unlink()
    e.j.transition(rec.id, S.COMPLETED)
    led = e.j.export_ledger()
    ent = led["entries"][0]
    assert set(ent) >= {"input", "status", "original_sha256", "final_path", "final_sha256", "approved_by_user"}
    e.j.close()
