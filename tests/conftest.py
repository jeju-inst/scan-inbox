import pytest

from scaninbox_corpus.fonts import find_font
from scaninbox_corpus.generate import generate


@pytest.fixture(scope="session")
def font_available():
    try:
        find_font()
    except FileNotFoundError:
        pytest.skip("한글 TTF 폰트 없음(SCANINBOX_FONT 지정 필요)")


@pytest.fixture(scope="session")
def corpus(tmp_path_factory, font_available):
    out = tmp_path_factory.mktemp("corpus") / "out"
    manifest = generate(out, dpi=72)
    return out, manifest
