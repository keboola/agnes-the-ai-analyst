"""FastAPI JSON response class that labels datetime values with UTC offset.

DuckDB TIMESTAMP reads return naive datetimes whose clock value is UTC
(thanks to the SET GLOBAL TimeZone='UTC' pin in `src.db._open_duckdb`).
Pydantic and `jsonable_encoder` would serialize those as ISO strings
*without* an offset suffix, and `new Date(...)` in JS parses offset-less
ISO datetimes as local time per the ECMAScript spec — so an analyst in
Europe/Prague would see times 2 hours off. This response class assumes
naive → UTC and emits `...+00:00` (via `isoformat()`) so the browser
converts correctly.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from fastapi.encoders import ENCODERS_BY_TYPE, jsonable_encoder
from fastapi.responses import JSONResponse


def _encode_dt(dt: datetime) -> str:
    """Serialize a `datetime` to ISO 8601 with an explicit UTC offset.

    Naive inputs are assumed to be UTC. Aware inputs preserve their offset.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


# Override FastAPI's global datetime encoder so `serialize_response`
# (which runs before any JSONResponse.render) produces offset-bearing
# strings. The previous default (`datetime.isoformat()` on the raw
# value) emitted offset-less strings for naive datetimes.
ENCODERS_BY_TYPE[datetime] = _encode_dt


class AgnesJSONResponse(JSONResponse):
    """Default response class — labels naive datetimes as UTC.

    Setting this as `default_response_class` on the FastAPI app is the
    main hook for endpoints that return raw `Response`-bearing values
    without going through `serialize_response`. The `ENCODERS_BY_TYPE`
    override above covers everything else.
    """

    def render(self, content: Any) -> bytes:
        return json.dumps(
            jsonable_encoder(content, custom_encoder={datetime: _encode_dt}),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
