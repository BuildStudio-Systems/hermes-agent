-- Native functions used by the shared SessionDB business queries.
-- Install offline in the profile schema; the runtime role has no DDL rights.
-- JSON paths intentionally cover only the single-key lineage markers used by
-- SessionDB, not an unrestricted imitation of SQLite's JSON extension.
CREATE FUNCTION session_marker_key(path text) RETURNS text
LANGUAGE plpgsql IMMUTABLE STRICT AS $$
BEGIN
    IF path !~ '^\$\.[A-Za-z_][A-Za-z_0-9]*$' THEN
        RAISE EXCEPTION 'Unsupported session marker path' USING ERRCODE='22023';
    END IF;
    RETURN substring(path FROM 3);
END $$;

CREATE FUNCTION json_extract(document text, path text) RETURNS text
LANGUAGE sql IMMUTABLE STRICT
RETURN document::jsonb ->> session_marker_key(path);

CREATE FUNCTION json_type(document text, path text) RETURNS text
LANGUAGE sql IMMUTABLE STRICT
RETURN CASE jsonb_typeof(document::jsonb -> session_marker_key(path))
    WHEN 'string' THEN 'text'
    WHEN 'boolean' THEN document::jsonb ->> session_marker_key(path)
    WHEN 'number' THEN CASE WHEN (document::jsonb ->> session_marker_key(path)) ~ '[.eE]'
        THEN 'real' ELSE 'integer' END
    ELSE jsonb_typeof(document::jsonb -> session_marker_key(path)) END;

CREATE FUNCTION json_remove(document text, path text) RETURNS text
LANGUAGE sql IMMUTABLE STRICT
RETURN (document::jsonb - session_marker_key(path))::text;

CREATE FUNCTION json_set(document text, path text, value text) RETURNS text
LANGUAGE sql IMMUTABLE
RETURN jsonb_set(document::jsonb, ARRAY[session_marker_key(path)],
                coalesce(to_jsonb(value), 'null'::jsonb), true)::text;

CREATE FUNCTION instr(document text, needle text) RETURNS integer
LANGUAGE sql IMMUTABLE STRICT
RETURN strpos(document, needle);

-- pg_trgm must be provisioned once by the DBA in public before this migration.
-- Expression indexes match the native search predicates. Unlike a tsvector,
-- these indexes do not truncate positions or reject multi-MB tool transcripts.
CREATE INDEX messages_content_trgm ON messages USING gin ((coalesce(content,'')) public.gin_trgm_ops);
CREATE INDEX messages_tool_name_trgm ON messages USING gin ((coalesce(tool_name,'')) public.gin_trgm_ops);
CREATE INDEX messages_tool_calls_trgm ON messages USING gin ((coalesce(tool_calls,'')) public.gin_trgm_ops);
