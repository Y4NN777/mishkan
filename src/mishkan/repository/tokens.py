"""Shared deterministic tokens binding read evidence to repository content."""

from __future__ import annotations

import hashlib
import json


def content_base_revision_token(
    *,
    repository_id: str,
    repository_revision: str,
    path: str,
    content_digest: str,
) -> str:
    payload = {
        "schema": "mishkan.content-base.v1",
        "repository_id": repository_id,
        "repository_revision": repository_revision,
        "path": path,
        "content_digest": content_digest,
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return f"mishkan-base-v1:{digest}"
