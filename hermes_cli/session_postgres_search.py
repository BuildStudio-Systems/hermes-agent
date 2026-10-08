"""PostgreSQL transcript search with bounded, parameterized term expressions.

Searches the complete canonical fields (including large tool outputs). Native
pg_trgm indexes can accelerate the regular expressions without tsvector's size
or position limits. Ranking is term frequency normalized by field length; it is
not SQLite BM25 and must not be presented as identical relevance scores.
"""
from __future__ import annotations

import re


def compile_terms(database, query):
    groups = [[]]
    negate = False
    for token in re.findall(r'"[^\"]+"\*?|\S+', database._sanitize_fts5_query(query)):
        op = token.upper()
        if op == 'OR':
            if groups[-1]:
                groups.append([])
            negate = False
            continue
        if op in ('AND', 'NEAR'):
            continue
        if op == 'NOT':
            negate = True
            continue
        prefix = token.endswith('*')
        term = token.rstrip('*').strip('"').strip()
        if not term:
            continue
        if database._contains_cjk(term):
            # CJK uses literal substring matching, including % and _.
            pattern = re.escape(term)
        else:
            words = re.findall(r'[^\W_]+', term, re.UNICODE)
            if not words:
                continue
            # Test adjacent characters instead of requiring the term's own
            # first/last characters to be locale-classified as word characters.
            # A C-locale database otherwise cannot find literal "résumé".
            pattern = r'(?<![[:alnum:]_])' + r'[^[:alnum:]]+'.join(re.escape(w) for w in words)
            if not prefix:
                pattern += r'(?![[:alnum:]_])'
        groups[-1].append((pattern, negate))
        negate = False
    return [group for group in groups if any(not negative for _, negative in group)]


def search(database, query, source_filter=None, exclude_sources=None,
           role_filter=None, limit=20, offset=0, sort=None,
           include_inactive=False, fields=None):
    result_fields = database._search_message_fields(fields)
    if not query or not query.strip() or source_filter == []:
        return []
    groups = compile_terms(database, query)
    if not groups:
        return []
    parameters, predicates, positives = [], [], []
    for group in groups:
        clauses = []
        for pattern, negative in group:
            clause = "(coalesce(m.content,'') ~* ? OR coalesce(m.tool_name,'') ~* ? OR coalesce(m.tool_calls,'') ~* ?)"
            clauses.append('NOT ' + clause if negative else clause)
            parameters.extend([pattern] * 3)
            if not negative:
                positives.append(pattern)
        predicates.append('(' + ' AND '.join(clauses) + ')')
    where = ['(' + ' OR '.join(predicates) + ')']
    if not include_inactive:
        where.append('(m.active=1 OR m.compacted=1)')
    for column, values, operator in (
        ('s.source', source_filter, 'IN'), ('s.source', exclude_sources, 'NOT IN'),
        ('m.role', role_filter, 'IN'),
    ):
        if values:
            where.append(f"{column} {operator} ({','.join('?' for _ in values)})")
            parameters.extend(values)
    order = str(sort).strip().lower()
    ordering = ('timestamp ASC, rank DESC, id ASC' if order == 'oldest' else
                'timestamp DESC, rank DESC, id DESC' if order == 'newest' else
                'rank DESC, id ASC')
    positive = '(?:' + '|'.join(dict.fromkeys(positives)) + ')'
    # Only snippets leave the server. No clipping of the searchable transcript.
    text = "coalesce(m.content,'') || E'\\n' || coalesce(m.tool_name,'') || E'\\n' || coalesce(m.tool_calls,'')"
    sql = f"""SELECT m.id,m.session_id,m.role,m.timestamp,m.tool_name,
                     s.source,s.model,s.started_at AS session_started,
                     substring({text} FROM greatest(1,regexp_instr({text}, ?, 1, 1, 0, 'i')-40) FOR 160) AS snippet,
                     regexp_count({text}, ?, 1, 'i')::float8 / sqrt(1+length({text})) AS rank
              FROM messages m JOIN sessions s ON s.id=m.session_id
              WHERE {' AND '.join(where)}
              ORDER BY {ordering} LIMIT ? OFFSET ?"""
    with database._read_ctx() as conn:
        matches = [dict(row) for row in conn.execute(
            sql, [positive, positive, *parameters, None if limit < 0 else limit, max(0, offset)]
        ).fetchall()]
    if result_fields is None or 'context' in result_fields:
        for match in matches:
            visible = '' if include_inactive else ' AND (active=1 OR compacted=1)'
            with database._read_ctx() as conn:
                rows = conn.execute(f"""
                    SELECT role,content FROM (
                      (SELECT id,timestamp,role,content FROM messages
                       WHERE session_id=? AND (timestamp,id)<(?,?) {visible}
                       ORDER BY timestamp DESC,id DESC LIMIT 1)
                      UNION ALL
                      (SELECT id,timestamp,role,content FROM messages WHERE id=? {visible})
                      UNION ALL
                      (SELECT id,timestamp,role,content FROM messages
                       WHERE session_id=? AND (timestamp,id)>(?,?) {visible}
                       ORDER BY timestamp,id LIMIT 1)
                    ) AS neighbors ORDER BY timestamp,id
                """, [match['session_id'],match['timestamp'],match['id'],match['id'],
                      match['session_id'],match['timestamp'],match['id']]).fetchall()
            context = []
            for row in rows:
                content = database._decode_content(row['content'])
                if isinstance(content, list):
                    content = ' '.join(str(p.get('text','')) for p in content
                                       if isinstance(p,dict) and p.get('type')=='text') or '[multimodal content]'
                context.append({'role':row['role'], 'content':content[:200] if isinstance(content,str) else ''})
            match['context'] = context
    if result_fields is not None:
        return [{key:row[key] for key in result_fields if key in row} for row in matches]
    return matches
