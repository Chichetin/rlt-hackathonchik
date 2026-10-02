"""Serve the static demo stand (#283) locally: files of the directory, any other path → index.html (SPA routes).

    uv run python scripts/serve_static.py dist-static [--port 8080]
"""

from __future__ import annotations

import argparse
import functools
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class SpaHandler(SimpleHTTPRequestHandler):
    def send_head(self):  # noqa: ANN201 — the base class signature
        path = Path(self.translate_path(self.path))
        # /results, /search … are routes of the UI, not files; a missing FILE (with an extension) stays a 404,
        # so a stand without static-demo.json says so instead of handing index.html to the JSON parser
        if not path.exists() and not Path(self.path.split("?", 1)[0]).suffix:
            self.path = "/index.html"
        return super().send_head()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("root", type=Path, nargs="?", default=Path("dist-static"))
    ap.add_argument("--port", type=int, default=8080)
    args = ap.parse_args(argv)
    if not (args.root / "index.html").exists():
        print(f"{args.root}: нет index.html — сначала make static-demo")
        return 2
    handler = functools.partial(SpaHandler, directory=str(args.root))
    with ThreadingHTTPServer(("127.0.0.1", args.port), handler) as srv:
        print(f"статический стенд: http://127.0.0.1:{args.port} (Ctrl+C — остановить)")
        srv.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
