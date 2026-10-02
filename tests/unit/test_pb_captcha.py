"""pb.nalog.ru captcha (#532): six-digit CTC decoding and the repeat of a refused request with pbCaptchaToken."""

import contextlib
import json

import httpx
import numpy as np
import pytest

from src.sources import pb_captcha, pb_nalog

INN = "7707083893"
CAPTCHA_400 = json.dumps({"ERRORS": {"pbSearchCaptcha": ["Требуется ввести цифры с картинки"]}, "STATUS": 400})


def _logp(path: list[int], t_len: int) -> np.ndarray:
    """Log-probabilities where `path` (0 blank, 1..10 digits) is the clear argmax at every step."""
    p = np.full((t_len, 11), 0.01)
    for t, s in enumerate(path):
        p[t, s] = 0.9
    return np.log(p / p.sum(axis=1, keepdims=True))


def test_decode_reads_six_digits_with_a_repeat_split_by_blank():
    # columns: 0 blank, 1 → "0", 2 → "1" …; "1 _ 1" is two ones, "2 2" one two
    assert pb_captcha.decode(_logp([2, 0, 2, 3, 3, 0, 4, 5, 6], 9)) == "112345"
    assert pb_captcha.decode(_logp([0, 1, 0, 2, 0, 3, 0, 4, 0, 5, 0, 6, 0], 13)) == "012345"


def test_decode_forces_exactly_six_when_the_argmax_path_has_fewer():
    out = pb_captcha.decode(_logp([0, 8, 8, 0, 9, 0, 0, 0, 0, 0, 0, 0], 12))
    assert len(out) == 6 and out.isdigit()


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _pb_handler(seen: list[str], *, solved: str = "TOKEN-1"):
    def handler(req: httpx.Request) -> httpx.Response:
        body = req.content.decode()
        seen.append(f"{req.method} {req.url.path} {body}")
        if req.url.path == "/static/captcha.bin":
            return httpx.Response(200, text="CAPTOKEN" if "a=" not in str(req.url) else "JPEG")
        if req.url.path == "/captcha-proc.json":
            ok = "captcha=123456" in body
            return httpx.Response(200, json=solved) if ok else httpx.Response(400, json={"ERRORS": {"captcha": ["x"]}})
        if req.url.path == "/search-proc.json" and "pbCaptchaToken" not in body and "get-response" not in body:
            return httpx.Response(400, text=CAPTCHA_400)
        if req.url.path == "/search-proc.json" and "get-response" not in body:
            return httpx.Response(200, json={"id": "S1", "captchaRequired": False})
        return httpx.Response(500)

    return handler


def test_refused_search_is_solved_and_repeated_with_the_token(monkeypatch):
    monkeypatch.setenv("PB_CAPTCHA_SOLVE", "1")
    monkeypatch.setattr(pb_nalog, "MIN_INTERVAL_S", 0.0)
    monkeypatch.setattr(pb_captcha, "available", lambda: True)
    monkeypatch.setattr(pb_captcha, "read", lambda jpg: "123456")
    seen: list[str] = []
    with _client(_pb_handler(seen)) as c:
        r = pb_nalog._post(c, "/search-proc.json", {"mode": "search-ul", "queryUl": INN})
    assert r.json()["id"] == "S1"
    assert seen[-1].endswith("pbCaptchaToken=TOKEN-1") and "captcha=123456&captchaToken=CAPTOKEN" in seen[-2]
    assert not pb_nalog.blocked()


def test_wrong_answers_run_out_and_the_host_is_blocked(monkeypatch):
    monkeypatch.setenv("PB_CAPTCHA_SOLVE", "1")
    monkeypatch.setattr(pb_nalog, "MIN_INTERVAL_S", 0.0)
    monkeypatch.setattr(pb_captcha, "available", lambda: True)
    monkeypatch.setattr(pb_captcha, "read", lambda jpg: "999999")
    seen: list[str] = []
    with _client(_pb_handler(seen)) as c, pytest.raises(pb_nalog.CaptchaRequired):
        pb_nalog._post(c, "/search-proc.json", {"mode": "search-ul", "queryUl": INN})
    assert sum("/captcha-proc.json" in s for s in seen) == pb_captcha.ATTEMPTS
    assert pb_nalog.blocked()


def test_without_the_switch_a_captcha_stops_as_before(monkeypatch):
    monkeypatch.delenv("PB_CAPTCHA_SOLVE", raising=False)
    monkeypatch.setattr(pb_nalog, "MIN_INTERVAL_S", 0.0)
    monkeypatch.setattr(pb_captcha, "solve", lambda *a, **k: pytest.fail("solver called with the switch off"))
    seen: list[str] = []
    with _client(_pb_handler(seen)) as c, pytest.raises(pb_nalog.CaptchaRequired):
        pb_nalog._post(c, "/search-proc.json", {"mode": "search-ul", "queryUl": INN})
    assert len(seen) == 1


def test_solve_paces_every_request():
    entered = []

    @contextlib.contextmanager
    def slot():
        entered.append(1)
        yield

    seen: list[str] = []
    with _client(_pb_handler(seen)) as c:
        assert pb_captcha.solve(c, "https://pb.nalog.ru", slot, read_fn=lambda jpg: "123456") == "TOKEN-1"
    assert len(entered) == len(seen) == 3
