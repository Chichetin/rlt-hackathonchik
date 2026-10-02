"""pb.nalog.ru captcha (#532, D-085 allows it): six digits on a JPEG, read by ddddocr with our own decoding.

Protocol (the site's own captcha-dialog.html and pb-commons.js, 02.10): GET /static/captcha.bin → a token;
GET /static/captcha.bin?a=<token>&version=4 → JPEG 200×100; POST /captcha-proc.json `captcha`, `captchaToken` →
200 and a JSON string — the `pbCaptchaToken` the refused request is repeated with; wrong digits → 400
`ERRORS.captcha`.

Reading: the blue digits are kept (HSV mask, thin noise lines opened away), the slant is undone, the crop is
stretched ×1.5 so the model has more time steps, and the CTC output of ddddocr is decoded over {blank, 0–9} with
exactly six digits (its own `set_ranges` drops letters after argmax, which leaves an empty string here). Measured
02.10 on live captchas: 3 of 12 answers accepted, so solve() tries up to ATTEMPTS images.

Optional: the extra `captcha` (ddddocr, opencv). Without it, or with PB_CAPTCHA_SOLVE unset, pb_nalog stops on a
captcha as before.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any

import httpx
import numpy as np

log = logging.getLogger(__name__)

DIGITS = "0123456789"
N_DIGITS = 6
ATTEMPTS = 8
STRETCH = 1.5
_ocr: Any = None


def decode(logp: np.ndarray, n: int = N_DIGITS) -> str:
    """Best CTC path with exactly `n` digits. `logp`: (T, 11) log-probabilities, column 0 blank, 1..10 digits."""
    t_len, s_len = logp.shape
    neg = -1e18
    score = np.full((n + 1, s_len), neg)
    back = np.zeros((t_len, n + 1, s_len, 2), dtype=np.int64)
    score[0, 0] = logp[0, 0]
    score[1, 1:] = logp[0, 1:]
    for t in range(1, t_len):
        new = np.full((n + 1, s_len), neg)
        for k in range(n + 1):
            for s in range(s_len):
                if s == 0:  # blank: after anything with the same count
                    j = int(np.argmax(score[k]))
                    best, src = score[k, j], (k, j)
                else:  # the same digit goes on, or a new one is emitted
                    best, src = score[k, s], (k, s)
                    if k > 0:
                        prev = score[k - 1].copy()
                        prev[s] = neg  # the same digit twice needs a blank between
                        j = int(np.argmax(prev))
                        if prev[j] > best:
                            best, src = prev[j], (k - 1, j)
                if best > neg / 2:
                    new[k, s] = best + logp[t, s]
                    back[t, k, s] = src
        score = new
    k, s = n, int(np.argmax(score[n]))
    if score[n, s] <= neg / 2:
        return ""
    out = []
    for t in range(t_len - 1, 0, -1):
        pk, ps = (int(v) for v in back[t, k, s])
        if s != 0 and (pk, ps) != (k, s):
            out.append(DIGITS[s - 1])
        k, s = pk, ps
    if s != 0:
        out.append(DIGITS[s - 1])
    return "".join(reversed(out))


def available() -> bool:
    try:
        import cv2  # noqa: F401  # pyright: ignore[reportMissingImports]
        import ddddocr  # noqa: F401  # pyright: ignore[reportMissingImports]
    except ImportError:
        return False
    return True


def _prepare(jpg: bytes) -> bytes | None:
    import cv2  # pyright: ignore[reportMissingImports]

    img = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
    if img is None:  # not an image (an error page instead of the JPEG)
        return None
    mask = cv2.inRange(cv2.cvtColor(img, cv2.COLOR_BGR2HSV), (95, 90, 40), (130, 255, 230))  # the blue digits
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))  # thin noise lines go
    n, labels, stats, _ = cv2.connectedComponentsWithStats(mask)
    keep = np.zeros_like(mask)
    for i in range(1, n):
        if stats[i, cv2.CC_STAT_AREA] >= 40 and stats[i, cv2.CC_STAT_HEIGHT] >= 15:
            keep[labels == i] = 255
    h, w = keep.shape
    best, best_score = keep, -1.0
    for k in np.linspace(0.0, 0.6, 13):  # italic: the shear that makes the column profile sharpest
        m = np.array([[1.0, k, -k * h / 2], [0.0, 1.0, 0.0]], dtype=np.float32)
        sheared = cv2.warpAffine(keep, m, (w, h))
        score = float(((sheared > 0).sum(axis=0).astype(float) ** 2).sum())
        if score > best_score:
            best, best_score = sheared, score
    ys, xs = np.nonzero(best)
    if len(xs):
        best = best[max(ys.min() - 4, 0):ys.max() + 5, max(xs.min() - 4, 0):xs.max() + 5]
    best = cv2.resize(best, (int(best.shape[1] * STRETCH), best.shape[0]), interpolation=cv2.INTER_NEAREST)
    best = cv2.copyMakeBorder(best, 6, 6, 6, 6, cv2.BORDER_CONSTANT, value=0)
    return cv2.imencode(".png", 255 - best)[1].tobytes()


def read(jpg: bytes) -> str:
    """Six digits read from the captcha JPEG ("" when the model gives no six-digit path)."""
    global _ocr
    import ddddocr  # pyright: ignore[reportMissingImports]

    if _ocr is None:
        _ocr = ddddocr.DdddOcr(show_ad=False)
    png = _prepare(jpg)
    if png is None:
        return ""
    res = _ocr.classification(png, probability=True)
    p = np.asarray(res["probabilities"], dtype=np.float64)
    p = p.reshape(-1, p.shape[-1])
    cols = [0] + [res["charset"].index(d) for d in DIGITS]
    return decode(np.log(p[:, cols] + 1e-12))


def solve(client: httpx.Client, base: str, slot: Callable[[], AbstractContextManager[Any]],
          read_fn: Callable[[bytes], str] | None = None) -> str | None:
    """A `pbCaptchaToken`, or None after ATTEMPTS wrong answers. Every request goes through `slot` (the pb pacer)."""
    read_fn = read_fn or read
    for attempt in range(1, ATTEMPTS + 1):
        with slot():
            token = client.get(f"{base}/static/captcha.bin", params={"r": str(int(time.time() * 1000))}).text.strip()
        with slot():
            jpg = client.get(f"{base}/static/captcha.bin",
                             params={"r": str(int(time.time() * 1000)), "a": token, "version": "4"}).content
        digits = read_fn(jpg)
        if len(digits) != N_DIGITS:
            continue
        with slot():
            r = client.post(f"{base}/captcha-proc.json", data={"captcha": digits, "captchaToken": token})
        if r.status_code == 200:
            answer = r.json()
            if isinstance(answer, str) and answer:
                log.info("pb.nalog.ru: капча решена с попытки %d", attempt)
                return answer
    log.warning("pb.nalog.ru: капча не решена за %d попыток", ATTEMPTS)
    return None
