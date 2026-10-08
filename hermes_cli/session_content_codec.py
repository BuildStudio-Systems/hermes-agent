"""Reversible PostgreSQL TEXT representation of legacy message content.

SQLite's structured-content marker starts with NUL, which PostgreSQL TEXT
cannot contain. Escape only content needing it, including literal occurrences
of our reserved prefix. Do not silently delete bytes or change user content.
"""
import base64
import json

PREFIX = '\x1ehermes-pg-content-v1:'


def encode_legacy(value):
    if isinstance(value, str) and ('\0' in value or value.startswith(PREFIX)):
        return PREFIX + json.dumps(['text', value], ensure_ascii=False)
    if isinstance(value, bytes):
        return PREFIX + json.dumps(['bytes', base64.b64encode(value).decode('ascii')])
    return value


def decode_legacy(value):
    if not isinstance(value, str) or not value.startswith(PREFIX):
        return value
    try:
        kind, payload = json.loads(value[len(PREFIX):])
        if not isinstance(payload, str):
            raise ValueError('Invalid content payload')
        if kind == 'text':
            return payload
        if kind == 'bytes':
            return base64.b64decode(payload, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError('Invalid PostgreSQL message content encoding') from None
    raise ValueError('Unknown PostgreSQL message content encoding')
