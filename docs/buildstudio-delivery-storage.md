# BuildStudio chat deliverable storage

The API gateway can copy validated output to the document volume before issuing
a download link. Configure `platforms.api_server.extra.file_delivery.snapshot_files`
as `true`. It defaults to `false` for existing installations. Published copies live
in the active profile's `cache/documents/deliveries` directory. Production mounts
the document volume from the central storage host; the Agent unit requires that
mount. A missing volume must stop the service instead of creating local fallback
output.

This separates local execution workspaces, which require POSIX permissions, from
durable delivered output. It does not expand the media path allowlist or bypass
user/chat ownership. Source validation still happens before publication and the
download endpoint still checks authentication, ownership, file metadata and the
media policy. The Web proxy independently checks the business chat.

Copies are bounded by the configured delivery size, written privately, checked for
changes during copying, then published using an atomic hard link without replacing
an existing copy. The storage filesystem must support hard links. Owner, chat,
filename and content determine the storage name; original filenames remain in
the protected PostgreSQL index. Opening an SMB file can refresh cached timestamps,
so copying pins inode identity and compares metadata on the same open descriptor.

Expired links do not delete durable copies. The ordinary one-day media-cache
cleanup only processes top-level files and does not remove this directory. Include
the directory in paired PostgreSQL/files backups and apply any future retention
policy to both artifacts and their recovery requirements. Do not remove local
workspaces as part of enabling snapshots: scripts and active jobs may still use
them.

Validation includes concurrent duplicate publication, original-file removal,
owner/chat separation, source size limits, failure cleanup, changed copies,
symlink rejection, SMB timestamp refresh and native authenticated HTTP delivery.
# Event-loop isolation

API response finalization and streaming MEDIA resolution dispatch potentially
blocking file copies and PostgreSQL publication to worker threads. Ordinary
text chunks remain on the event loop. Request owner and chat ContextVars follow
the worker, and stream ordering and path redaction remain unchanged. Session
agent delta callbacks already execute in the agent worker; their final flush
uses the asynchronous resolver as well.
