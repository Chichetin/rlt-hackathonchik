"""`make openapi`: docs/openapi.json from the FastAPI app (contracts -> the AIS GZ integration slide)."""

import json
from pathlib import Path

from src.api.app import app

out = Path(__file__).resolve().parents[1] / "docs" / "openapi.json"
out.write_text(json.dumps(app.openapi(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(f"-> {out}")
