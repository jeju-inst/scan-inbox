import pytest

from scaninbox_corpus.fonts import find_font
from scaninbox_corpus.generate import generate
from scaninbox_corpus.pathutil import extended


@pytest.fixture(scope="session")
def font_available():
    try:
        find_font()
    except FileNotFoundError:
        pytest.skip("한글 TTF 폰트 없음(SCANINBOX_FONT 지정 필요)")


@pytest.fixture(scope="session")
def corpus(tmp_path_factory, font_available):
    out = extended(tmp_path_factory.mktemp("corpus") / "out")  # Windows 에서 MAX_PATH 회피
    manifest = generate(out, dpi=72)
    return out, manifest
