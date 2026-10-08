"""Reversible PostgreSQL TEXT representation of legacy message content.

SQLite's structured-content marker starts with NUL, which PostgreSQL TEXT
cannot contain. Escape only content needing it, including literal occurrences
of our reserved prefix. Do not silently delete bytes or change user content.
"""
import base64
import json

PREFIX = '\x1ehermes-pg-content-v1:'


def _structured_search_text(value):
    """Readable index material; the original legacy bytes stay authoritative."""
    if not value.startswith('\0json:'):
        return None
    try:
        content = json.loads(value[6:])
    except (ValueError, TypeError, RecursionError):
        return None
    parts = content if isinstance(content, list) else [content]
    texts = [p['text'] for p in parts if isinstance(p, dict)
             and isinstance(p.get('text'), str)]
    if not texts:
        return None
    # PostgreSQL cannot store NUL or unpaired surrogate code points. They
    # remain reversible in the original JSON; search material is derived only.
    return ' '.join(texts).replace('\0', ' ').encode('utf-8', errors='replace').decode('utf-8')


def encode_legacy(value):
    if isinstance(value, str) and ('\0' in value or value.startswith(PREFIX)):
        envelope = ['text', value]
        search_text = _structured_search_text(value)
        if search_text is not None:
            envelope.append(search_text)
        return PREFIX + json.dumps(envelope, ensure_ascii=False)
    if isinstance(value, bytes):
        return PREFIX + json.dumps(['bytes', base64.b64encode(value).decode('ascii')])
    return value


def decode_legacy(value):
    if not isinstance(value, str) or not value.startswith(PREFIX):
        return value
    try:
        envelope = json.loads(value[len(PREFIX):])
        if not isinstance(envelope, list) or len(envelope) not in (2, 3):
            raise ValueError('Invalid content envelope')
        kind, payload = envelope[:2]
        if not isinstance(payload, str):
            raise ValueError('Invalid content payload')
        if len(envelope) == 3 and (kind != 'text' or envelope[2] != _structured_search_text(payload)):
            raise ValueError('Invalid derived search material')
        if kind == 'text':
            return payload
        if kind == 'bytes':
            return base64.b64decode(payload, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError('Invalid PostgreSQL message content encoding') from None
    raise ValueError('Unknown PostgreSQL message content encoding')
