import os
import unicodedata

import pytest

from scaninbox import pathsafe as ps

SHA = "ab12cd34" + "0" * 56


# ---- 순수 규칙 (OS 무관)

@pytest.mark.parametrize("raw,expect", [
    ('a<b>c:d"e/f\\g|h?i*j', "a_b_c_d_e_f_g_h_i_j"),
    ("끝점.", "끝점"),
    ("끝공백 ", "끝공백"),
    ("  앞공백", "앞공백"),
    ("탭\t줄\n바꿈", "탭_줄_바꿈"),
    ("", "_"),
    ("...", "_"),
])
def test_sanitize_basic(raw, expect):
    assert ps.sanitize_component(raw) == expect


@pytest.mark.parametrize("name", ["CON", "nul", "Aux.txt", "COM1", "lpt9.pdf", "com\u00b9", "CONIN$"])
def test_reserved_names_get_prefixed(name):
    out = ps.sanitize_component(name)
    assert out.startswith("_") and ps.is_safe_component(out)


def test_reserved_only_when_whole_stem():
    assert ps.sanitize_component("CONSOLE") == "CONSOLE"
    assert ps.sanitize_component("com10") == "com10"


def test_is_safe_component_rules():
    assert ps.is_safe_component("2026-11-05_계약서__ab12cd34.pdf")
    for bad in ["", ".", "..", "a/b", "a:b", "x.", "x ", " x", "NUL.pdf", "e\u0301.pdf", "a" * 300]:
        assert not ps.is_safe_component(bad), bad


def test_nfc_normalization_in_sanitize():
    nfd = unicodedata.normalize("NFD", "한글제목")
    assert ps.sanitize_component(nfd) == "한글제목"


def test_collision_key_nfc_nfd_case_and_trailing():
    nfd = unicodedata.normalize("NFD", "문서.PDF")
    assert ps.collision_key(nfd) == ps.collision_key("문서.pdf")
    assert ps.collision_key("A.pdf") == ps.collision_key("a.PDF")
    assert ps.collision_key("a.pdf.") == ps.collision_key("a.pdf")
    assert ps.collision_key("a.pdf ") == ps.collision_key("a.pdf")
    assert ps.collision_key("a.pdf") != ps.collision_key("b.pdf")


def test_resolve_collision_suffix_and_no_overwrite():
    name = "2026-11-05_계약__ab12cd34.pdf"
    taken = {ps.collision_key(name)}
    n2 = ps.resolve_collision(name, taken)
    assert n2 == "2026-11-05_계약__ab12cd34_2.pdf"
    taken.add(ps.collision_key(n2))
    # NFD·대문자로 바뀐 기존 이름도 충돌로 본다
    taken.add(ps.collision_key(unicodedata.normalize("NFD", "2026-11-05_계약__AB12CD34_3.PDF")))
    assert ps.resolve_collision(name, taken) == "2026-11-05_계약__ab12cd34_4.pdf"
    assert ps.resolve_collision("다른.pdf", taken) == "다른.pdf"


def test_short_id():
    assert ps.short_id(SHA.upper()) == "ab12cd34"
    with pytest.raises(ps.PathSafetyError):
        ps.short_id("xyz")


def test_proposed_name_formats():
    assert ps.proposed_name("계약서", SHA, "2026-11-05") == "2026-11-05_계약서__ab12cd34.pdf"
    assert ps.proposed_name("계약서", SHA) == "날짜미상_계약서__ab12cd34.pdf"
    with pytest.raises(ps.PathSafetyError):
        ps.proposed_name("계약서", SHA, "2026/11/05")
    with pytest.raises(ps.PathSafetyError):
        ps.proposed_name("계약서", SHA, "2026-1-5")


def test_proposed_name_sanitizes_and_is_safe():
    n = ps.proposed_name('CON: "견적"?', SHA, "2026-11-05")
    assert ps.is_safe_component(n)
    assert n.endswith("__ab12cd34.pdf")
    n2 = ps.proposed_name("끝점...", SHA)
    assert "..__" not in n2 and ps.is_safe_component(n2)


def test_proposed_name_truncates_to_byte_limit_and_keeps_id():
    n = ps.proposed_name("가" * 500, SHA, "2026-11-05")
    assert len(n.encode("utf-8")) <= ps.MAX_NAME_BYTES
    assert n.endswith("__ab12cd34.pdf") and n.startswith("2026-11-05_")


def test_proposed_name_respects_total_path_budget_plain_windows():
    d = "C:\\" + "폴" * 160          # 163 UTF-16 단위
    n = ps.proposed_name("가" * 200, SHA, "2026-11-05", dir_path=d)
    total = ps.utf16_len(d) + 1 + ps.utf16_len(n)
    assert total + len("_99") <= ps.MAX_PATH_PLAIN
    # 확장 경로면 한도가 커서 이름 상한(바이트)만 적용
    ext = ps.proposed_name("가" * 200, SHA, "2026-11-05", dir_path="\\\\?\\" + d)
    assert len(ext) > len(n)


def test_proposed_name_path_too_long():
    d = "C:\\" + "a" * 240
    with pytest.raises(ps.PathTooLong):
        ps.proposed_name("제목", SHA, "2026-11-05", dir_path=d)


def test_utf16_len_counts_surrogates():
    assert ps.utf16_len("a가") == 2
    assert ps.utf16_len("\U0001F600") == 2


# ---- 상대경로 어휘 검사

@pytest.mark.parametrize("rel", [
    "../x", "a/../../x", "a/..", "/etc/passwd", "\\x", "C:\\x", "C:x", "\\\\srv\\share\\x", "\\\\?\\C:\\x",
    "", ".", "a\x00b", "a\\..\\b",
])
def test_check_relative_rejects(rel):
    with pytest.raises(ps.EscapesRoot):
        ps.check_relative(rel)


def test_check_relative_ok():
    assert ps.check_relative("a/b\\c.pdf") == ["a", "b", "c.pdf"]
    assert ps.check_relative("./a/./b") == ["a", "b"]


# ---- 파일시스템

def test_ensure_within_ok_and_nonexistent(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    assert ps.ensure_within(root, "a/b.pdf") == root / "a" / "b.pdf"


def test_ensure_within_rejects_symlink_escape(tmp_path):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir(); outside.mkdir()
    try:
        os.symlink(outside, root / "link", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink 생성 권한 없음")
    with pytest.raises(ps.EscapesRoot):
        ps.ensure_within(root, "link/x.pdf")
    # 루트 안으로만 가는 링크도 링크를 거치는 경로는 거절(예측 가능하게)
    (root / "real").mkdir()
    os.symlink(root / "real", root / "inner", target_is_directory=True)
    with pytest.raises(ps.EscapesRoot):
        ps.ensure_within(root, "inner/x.pdf")


def test_is_within_resolves_links(tmp_path):
    root = tmp_path / "root"; root.mkdir()
    out = tmp_path / "out"; out.mkdir()
    try:
        os.symlink(out, root / "l", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink 생성 권한 없음")
    assert not ps.is_within(root, root / "l" / "f")
    assert ps.is_within(root, root / "f")


def test_move_no_overwrite_basic_and_refuses_existing(tmp_path):
    src = tmp_path / "src.bin"; src.write_bytes(b"new")
    d = tmp_path / "dst"; d.mkdir()
    ps.move_no_overwrite(src, d / "a.pdf")
    assert (d / "a.pdf").read_bytes() == b"new" and not src.exists()
    src2 = tmp_path / "src2.bin"; src2.write_bytes(b"other")
    with pytest.raises(FileExistsError):
        ps.move_no_overwrite(src2, d / "a.pdf")
    # 대소문자·NFD 만 다른 이름도 거절
    with pytest.raises(FileExistsError):
        ps.move_no_overwrite(src2, d / "A.PDF")
    assert (d / "a.pdf").read_bytes() == b"new" and src2.exists()


def test_unique_name_uses_directory_listing(tmp_path):
    (tmp_path / "x__ab12cd34.pdf").write_bytes(b"1")
    assert ps.unique_name(tmp_path, "X__AB12CD34.PDF") == "X__AB12CD34_2.PDF"
    assert ps.unique_name(tmp_path / "없는폴더", "a.pdf") == "a.pdf"


def test_move_no_overwrite_cross_device_fallback(tmp_path, monkeypatch):
    import errno
    src = tmp_path / "s.bin"; src.write_bytes(b"data")
    d = tmp_path / "d"; d.mkdir()
    real = ps._atomic_place
    calls = {"n": 0}

    def flaky(a, b):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError(errno.EXDEV, "cross-device")
        return real(a, b)
    monkeypatch.setattr(ps, "_atomic_place", flaky)
    ps.move_no_overwrite(src, d / "f.pdf")
    assert (d / "f.pdf").read_bytes() == b"data" and not src.exists()
    assert [n for n in os.listdir(d)] == ["f.pdf"]  # 임시 파일이 남지 않음


def test_cross_device_temp_name_is_short(tmp_path, monkeypatch):
    import errno
    src = tmp_path / "s.bin"; src.write_bytes(b"d")
    d = tmp_path / "d"; d.mkdir()
    seen = []
    real_open = open

    def spy(path, mode="r", *a, **k):
        if "x" in mode:
            seen.append(os.path.basename(path))
        return real_open(path, mode, *a, **k)
    real = ps._atomic_place
    n = {"c": 0}

    def flaky(a, b):
        n["c"] += 1
        if n["c"] == 1:
            raise OSError(errno.EXDEV, "x")
        return real(a, b)
    monkeypatch.setattr(ps, "_atomic_place", flaky)
    monkeypatch.setattr("builtins.open", spy)
    ps.move_no_overwrite(src, d / ("가" * 70 + ".pdf"))
    assert seen and len(seen[0]) <= 16
