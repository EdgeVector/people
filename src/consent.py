"""People consent-code codec and open-link identity bind.

The consent wrapper is unsigned hand-off material. Trust comes from the
independently verified publisher key and the open-link payload LastDB mints.
"""

from __future__ import annotations

import base64
import json

from lastdb import PeopleError

CONSENT_PREFIX = "people-consent-v1."
OPEN_PREFIX = "lastdb-read-v1."
MAX_CONSENT = 8192


def encode_consent(payload):
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    body = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    code = f"{CONSENT_PREFIX}{body}"
    if len(code) > MAX_CONSENT:
        raise PeopleError("consent code exceeds its size limit")
    return code


def decode_consent(code):
    if not isinstance(code, str) or not code.startswith(CONSENT_PREFIX):
        raise PeopleError("expected a People consent code")
    body = code[len(CONSENT_PREFIX):]
    if not body or len(code) > MAX_CONSENT:
        raise PeopleError("expected a People consent code")
    try:
        padded = body + "=" * (-len(body) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        payload = json.loads(raw)
    except (ValueError, json.JSONDecodeError) as error:
        raise PeopleError("expected a People consent code") from error
    if (not isinstance(payload, dict) or payload.get("v") != 1
            or not isinstance(payload.get("databaseId"), str)
            or not isinstance(payload.get("publisher"), str)
            or not isinstance(payload.get("entryHash"), str) or not payload["entryHash"]
            or not isinstance(payload.get("open"), str)
            or not payload["open"].startswith(OPEN_PREFIX)):
        raise PeopleError("expected a People consent code")
    if "writerSeq" in payload and type(payload["writerSeq"]) is not int:
        raise PeopleError("expected a People consent code")
    return payload


def open_link_identity(link):
    """Publisher and database id from a lastdb-read-v1 open link (pre-accept bind)."""
    if not isinstance(link, str) or not link.startswith(OPEN_PREFIX):
        raise PeopleError("expected an open read link")
    body = link[len(OPEN_PREFIX):]
    if not body or len(link) > MAX_CONSENT:
        raise PeopleError("expected an open read link")
    try:
        padded = body + "=" * (-len(body) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except (ValueError, json.JSONDecodeError) as error:
        raise PeopleError("expected an open read link") from error
    publisher, database = payload.get("publisher"), payload.get("database")
    if (not isinstance(payload, dict) or payload.get("version") != 1
            or not isinstance(publisher, str) or not publisher
            or not isinstance(database, str) or not database):
        raise PeopleError("expected an open read link")
    return publisher, database


def read_consent(stream):
    """Read a consent code: raw string, JSON string, or JSON object with `consent`."""
    raw = stream.read(MAX_CONSENT + 1)
    if len(raw) > MAX_CONSENT:
        raise PeopleError("consent code exceeds its size limit")
    text = raw.decode("utf-8").strip()
    if text.startswith(CONSENT_PREFIX):
        return text
    try:
        value = json.loads(text)
    except (ValueError, UnicodeError) as error:
        raise PeopleError("expected a People consent code") from error
    if isinstance(value, str) and value.startswith(CONSENT_PREFIX):
        return value
    if isinstance(value, dict) and isinstance(value.get("consent"), str):
        return value["consent"]
    raise PeopleError("expected a People consent code")
