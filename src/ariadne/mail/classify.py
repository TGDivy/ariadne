"""Answer what mail ingestion would do with one message, without a model turn."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .accounts import MailAccountRegistry
from .models import MailClassification, MailMetadata, MailRoutes, classify_message
from .reader import decode_mail_id
from .runtime import fetch_route_headers, parse_metadata

FILE_LIMIT = 25 * 1024 * 1024


class MailClassifier:
    """Run the shipped routing decision against a stored or supplied message."""

    def __init__(self, routes: MailRoutes, registry: MailAccountRegistry) -> None:
        self.routes = routes
        self.registry = registry

    def classify(
        self, *, mail_id: str | None = None, path: Path | None = None
    ) -> dict[str, Any]:
        if (mail_id is None) == (path is None):
            raise ValueError("Classify exactly one mail id or one message file.")
        if mail_id is not None:
            return self._classify_id(mail_id)
        assert path is not None
        return self._classify_file(path)

    def _classify_id(self, mail_id: str) -> dict[str, Any]:
        reference = decode_mail_id(mail_id)
        account = self.registry.get(reference.account_key)
        with self.registry.connect(account.key) as client:
            raw = fetch_route_headers(
                client, reference.folder, reference.uidvalidity, reference.uid
            )
        metadata = parse_metadata(raw)
        payload = _payload(
            self.routes, metadata, classify_message(self.routes, metadata)
        )
        return {
            "source": "mail_id",
            "id": mail_id,
            "account_key": account.key,
            "account_label": account.label,
            "folder": reference.folder,
            **payload,
        }

    def _classify_file(self, path: Path) -> dict[str, Any]:
        try:
            if path.stat().st_size > FILE_LIMIT:
                raise ValueError("That message file is too large to classify.")
            raw = path.read_bytes()
        except OSError as error:
            raise ValueError(f"That message file could not be read: {path}") from error
        metadata = parse_metadata(raw)
        payload = _payload(
            self.routes, metadata, classify_message(self.routes, metadata)
        )
        return {"source": "file", "path": str(path), **payload}


def _payload(
    routes: MailRoutes, metadata: MailMetadata, decision: MailClassification
) -> dict[str, Any]:
    return {
        "message": {
            "message_id": metadata.message_id,
            "subject": metadata.subject,
            "from": list(metadata.sender),
            "to": list(metadata.recipients),
            "date": metadata.date,
            "has_list_unsubscribe": metadata.has_list_unsubscribe,
        },
        "matched_route_ids": list(decision.matched_route_ids),
        "route_id": decision.route_id,
        "classification": decision.classification,
        "action": decision.action,
        "destination": decision.destination,
        "wakes_iris": decision.wakes_iris,
        "cheap_triage": decision.triage,
        "defaults": {
            "unmatched_action": routes.defaults.unmatched_action,
            "unmatched_keep_in_inbox": routes.defaults.unmatched_keep_in_inbox,
        },
    }
