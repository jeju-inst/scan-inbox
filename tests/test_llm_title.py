"""llm_title.OllamaTitleExtractor 시험. 가짜 Ollama 서버(로컬 스레드)로 응답을 흉내 낸다. 문장은 모두 가상."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from scaninbox import cli
from scaninbox.llm_title import LLM_REASON, OllamaTitleExtractor, date_supported, title_supported
from scaninbox.pathsafe import sanitize_component
from scaninbox.title import RuleTitleExtractor

PAGE = "가 상 시 설 종 합 계 획 수 정 보 완 추 진 상 황 보 고 | QX ol\n작성 2026. 3. 5.\n가상 본문 내용\n"


class _Fake:
    answer: object = None
    requests: list = []


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        _Fake.requests.append(json.loads(self.rfile.read(n)))
        body = json.dumps({"response": _Fake.answer if isinstance(_Fake.answer, str)
                           else json.dumps(_Fake.answer, ensure_ascii=False)}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture()
def server():
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    _Fake.requests = []
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def _ex(url):
    return OllamaTitleExtractor(url=url, model="fake", timeout_s=5)


def test_supported_title_replaces_rule_title_and_is_flagged(server):
    _Fake.answer = {"title": "가상 시설 종합계획 수정 보완 추진 상황보고", "date": "2026-03-05", "confidence": "high"}
    p = _ex(server).propose(None, [PAGE])
    assert p.title == "가상 시설 종합계획 수정 보완 추진 상황보고"
    assert p.doc_date == "2026-03-05"
    assert LLM_REASON in p.review_reasons
    assert _Fake.requests[0]["model"] == "fake" and len(_Fake.requests[0]["prompt"]) < 2500


def test_invented_title_is_rejected(server):
    _Fake.answer = {"title": "전혀 다른 연간 예산 편성 지침", "date": None, "confidence": "high"}
    p = _ex(server).propose(None, [PAGE])
    rule = RuleTitleExtractor().propose(None, [PAGE])
    assert p.title == rule.title and LLM_REASON not in p.review_reasons


def test_low_confidence_falls_back(server):
    _Fake.answer = {"title": "가상 시설 종합계획", "date": None, "confidence": "low"}
    assert LLM_REASON not in _ex(server).propose(None, [PAGE]).review_reasons


def test_invented_date_is_dropped(server):
    _Fake.answer = {"title": "가상 시설 종합계획 수정 보완 추진 상황보고", "date": "2023-10-15", "confidence": "high"}
    p = _ex(server).propose(None, ["가 상 시 설 종 합 계 획 수 정 보 완 추 진 상 황 보 고 | QX ol\n가상 본문\n"])
    assert p.title and p.doc_date is None


def test_garbage_response_falls_back(server):
    _Fake.answer = "not json"
    p = _ex(server).propose(None, [PAGE])
    assert LLM_REASON not in p.review_reasons


def test_unreachable_server_falls_back(capsys):
    ex = OllamaTitleExtractor(url="http://127.0.0.1:9", model="fake", timeout_s=2)
    p = ex.propose(None, [PAGE])
    ex.propose(None, [PAGE])
    assert LLM_REASON not in p.review_reasons
    assert capsys.readouterr().err.count("로컬 LLM 호출 실패") == 1


def test_bad_url_rejected():
    with pytest.raises(ValueError):
        OllamaTitleExtractor(url="file:///etc/passwd")


def test_guards():
    assert not title_supported("2026-03-05", "2026-03-05 가상")
    assert not title_supported("가" * 151, "가" * 200)
    assert title_supported("Quarterly Facilities Summary", "Quarterly  Facilities Summary\nbody")
    assert date_supported("2026-03-05", "작성일 2026. 3. 5.") == "2026-03-05"
    assert date_supported("2026-03-05", "26.03.05 작성") == "2026-03-05"
    assert date_supported("2026-03-05", "2026. 3. 15.") is None
    assert date_supported("bad", "2026. 3. 5.") is None
    assert date_supported("2026-02-30", "작성일 2026. 2. 30.") is None


def test_llm_title_with_range_marks_is_filename_safe():
    name = sanitize_component("제3차 계획(22~31년)/보완: 상황?")
    assert "/" not in name and ":" not in name and "?" not in name


def test_cli_engine_selection(monkeypatch):
    p = cli.build_parser()
    args = p.parse_args(["status"])
    monkeypatch.delenv("SCANINBOX_TITLE_ENGINE", raising=False)
    assert isinstance(cli._create_title_extractor(args), RuleTitleExtractor)
    args = p.parse_args(["--title-engine", "ollama", "--ollama-url", "http://127.0.0.1:1", "status"])
    ex = cli._create_title_extractor(args)
    assert isinstance(ex, OllamaTitleExtractor) and ex.url == "http://127.0.0.1:1"
    monkeypatch.setenv("SCANINBOX_TITLE_ENGINE", "bogus")
    with pytest.raises(ValueError):
        cli._create_title_extractor(p.parse_args(["status"]))


def test_confident_rule_title_skips_llm(server):
    _Fake.answer = {"title": "다른 제목", "date": None, "confidence": "high"}
    p = _ex(server).propose(None, ["가상 시설 운영 계획\n2026. 3. 5.\n1. 배경: 가상.\n"])
    assert p.title == "가상 시설 운영 계획" and not _Fake.requests


@pytest.mark.parametrize("raw", ["[]", '{"response": {"title": "x"}}', '{"other": 1}'])
def test_unexpected_envelope_falls_back(raw, monkeypatch):
    import io
    import urllib.request as ur

    class _R(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(ur, "urlopen", lambda *a, **k: _R(raw.encode()))
    p = OllamaTitleExtractor(url="http://127.0.0.1:1", model="fake").propose(None, [PAGE])
    assert LLM_REASON not in p.review_reasons
