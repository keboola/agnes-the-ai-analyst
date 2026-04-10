"""Generate OpenAPI snapshot from the current FastAPI app."""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("JWT_SECRET_KEY", "snapshot-generation-key-32-chars-min!!")

from app.main import create_app  # noqa: E402

app = create_app()
schema = app.openapi()
json.dump(schema, sys.stdout, indent=2, sort_keys=True)
sys.stdout.write("\n")
