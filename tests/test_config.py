import json
import os

import pytest

from scaninbox import config as C


@pytest.fixture
def base(tmp_path, monkeypatch):
    monkeypatch.setenv(C.ENV_HOME, str(tmp_path / "appdata"))
    scan = tmp_path / "scan"
    scan.mkdir()
    return tmp_path, scan


def mk(base, **kw):
    tmp, scan = base
    return C.Config.with_defaults(scan, **kw)


def test_defaults_are_siblings_and_journal_in_appdata(base):
    tmp, scan = base
    cfg = mk(base)
    assert cfg.done_dir == tmp / "scan_처리완료"
    assert cfg.backup_dir == tmp / "scan_원본보관"
    assert cfg.journal_path == tmp / "appdata" / "journal.sqlite"
    C.validate(cfg)


def test_save_load_roundtrip_in_appdata(base):
    tmp, scan = base
    cfg = mk(base, ocr_timeout_s=900)
    dest = C.save(cfg)
    assert dest == tmp / "appdata" / "config.json" and dest.exists()
    assert C.load() == cfg
    assert not [p for p in dest.parent.iterdir() if p.name.endswith(".tmp")]


def test_save_refuses_invalid_and_keeps_old_file(base):
    tmp, scan = base
    C.save(mk(base))
    bad = mk(base, done_dir=scan / "done")
    with pytest.raises(C.ConfigError):
        C.save(bad)
    assert C.load().done_dir == tmp / "scan_처리완료"


def test_load_errors(base):
    tmp, scan = base
    with pytest.raises(C.ConfigError, match="설정 파일 없음"):
        C.load()
    p = C.config_path(); p.parent.mkdir(parents=True)
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(C.ConfigError, match="형식 오류"):
        C.load()
    p.write_text(json.dumps({"scan_dir": str(scan)}), encoding="utf-8")
    with pytest.raises(C.ConfigError, match="필수"):
        C.load()
    d = mk(base).to_json(); d["surprise"] = 1
    p.write_text(json.dumps(d), encoding="utf-8")
    with pytest.raises(C.ConfigError, match="알 수 없는"):
        C.load()


@pytest.mark.parametrize("field,sub", [("done_dir", "d"), ("backup_dir", "b")])
def test_target_inside_scan_rejected(base, field, sub):
    tmp, scan = base
    cfg = mk(base, **{field: scan / sub})
    with pytest.raises(C.ConfigError, match="겹침"):
        C.validate(cfg)


def test_scan_inside_done_rejected_and_equal_rejected(base):
    tmp, scan = base
    with pytest.raises(C.ConfigError, match="겹침"):
        C.validate(mk(base, done_dir=tmp))      # 스캔이 완료함 안에 있음
    with pytest.raises(C.ConfigError, match="겹침"):
        C.validate(mk(base, done_dir=scan))
    with pytest.raises(C.ConfigError, match="겹침"):
        C.validate(mk(base, done_dir=tmp / "x", backup_dir=tmp / "x"))
    with pytest.raises(C.ConfigError, match="겹침"):
        C.validate(mk(base, done_dir=tmp / "x", backup_dir=tmp / "x" / "y"))


def test_overlap_is_case_and_normalization_insensitive(base):
    tmp, scan = base
    import unicodedata
    with pytest.raises(C.ConfigError, match="겹침"):
        C.validate(mk(base, done_dir=tmp / "SCAN" / "d"))
    with pytest.raises(C.ConfigError, match="겹침"):
        C.validate(mk(base, done_dir=tmp / unicodedata.normalize("NFD", "스캔") / "d",
                      backup_dir=tmp / unicodedata.normalize("NFC", "스캔") / "d"))


def test_journal_must_be_outside_all_dirs(base):
    tmp, scan = base
    for where in (scan / "j.sqlite", tmp / "scan_처리완료" / "j.sqlite", tmp / "scan_원본보관" / "j.sqlite"):
        with pytest.raises(C.ConfigError, match="journal_path"):
            C.validate(mk(base, journal_path=where))


def test_symlink_escape_rejected(base):
    tmp, scan = base
    link = tmp / "linkdone"
    try:
        os.symlink(scan, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink 생성 권한 없음")
    with pytest.raises(C.ConfigError, match="symlink"):
        C.validate(mk(base, done_dir=link))
    # 부모가 링크라서 실제로는 스캔 안인 경우도 겹침으로 잡는다
    with pytest.raises(C.ConfigError, match="겹침"):
        C.validate(mk(base, done_dir=link / "sub"))
    # journal 이 링크를 통해 스캔 폴더 안으로 들어가는 경우
    with pytest.raises(C.ConfigError, match="journal_path"):
        C.validate(mk(base, journal_path=link / "j.sqlite"))


def test_relative_paths_and_missing_scan_rejected(base):
    tmp, scan = base
    cfg = mk(base)
    cfg.done_dir = C.Path("relative/done")
    with pytest.raises(C.ConfigError, match="절대경로"):
        C.validate(cfg)
    with pytest.raises(C.ConfigError, match="scan_dir"):
        C.validate(C.Config.with_defaults(tmp / "nope"))
    C.validate(C.Config.with_defaults(tmp / "nope"), require_scan_exists=False)


def test_all_problems_reported_together(base):
    tmp, scan = base
    cfg = mk(base, done_dir=scan / "d", backup_dir=scan / "b", ocr_timeout_s=0)
    with pytest.raises(C.ConfigError) as e:
        C.validate(cfg)
    assert len(e.value.problems) >= 3


def test_bad_values(base):
    for kw in ({"ocr_langs": "kor eng; rm"}, {"ocr_langs": ""}, {"lease_seconds": 0}, {"stable_seconds": -1}):
        with pytest.raises(C.ConfigError):
            C.validate(mk(base, **kw))


def test_app_data_dir_fallbacks(monkeypatch, tmp_path):
    monkeypatch.delenv(C.ENV_HOME, raising=False)
    p = C.app_data_dir()
    assert p.name == C.APP_NAME and p.is_absolute()
    monkeypatch.setenv(C.ENV_HOME, str(tmp_path))
    assert C.app_data_dir() == tmp_path
