"""Validation gate against IW's canonical schemas (vendored into ./schemas/).
The vendored copies ride into the image via the existing `COPY sandbox/ .`,
and a CI drift test asserts they stay byte-identical to jira-chatops-gateway/schemas/.
"""

import json
from pathlib import Path
from jsonschema import Draft202012Validator

_SCHEMA_DIR = Path(__file__).resolve().parent / "schemas"


def _validator(name: str) -> Draft202012Validator:
    return Draft202012Validator(json.loads((_SCHEMA_DIR / name).read_text()))


_RESP = _validator("runtime_response.schema.json")
_REQ = _validator("runtime_request.schema.json")


def _fmt(errors) -> list[str]:
    return [f"{'/'.join(map(str, e.path)) or '<root>'}: {e.message}" for e in errors]


def response_errors(obj: dict) -> list[str]:
    return _fmt(_RESP.iter_errors(obj))


def request_errors(obj: dict) -> list[str]:
    return _fmt(_REQ.iter_errors(obj))
