"""iCloud Mail event source and deterministic routing."""

from .models import (
    BackfillSummary,
    Importance,
    MailMetadata,
    MailRoute,
    MailRoutes,
    SuggestedAction,
)
from .runtime import (
    FULL_QUERY,
    HEADER_QUERY,
    IMAP_HOST,
    MailLoop,
    MailProcessor,
    MailState,
    backfill_inbox,
    cheap_triage,
    ensure_folders,
    load_routes,
    move_messages,
    parse_metadata,
    record_current_mail_decision,
    render_message,
)

__all__ = [
    "FULL_QUERY",
    "HEADER_QUERY",
    "IMAP_HOST",
    "BackfillSummary",
    "Importance",
    "MailLoop",
    "MailMetadata",
    "MailProcessor",
    "MailRoute",
    "MailRoutes",
    "MailState",
    "SuggestedAction",
    "backfill_inbox",
    "cheap_triage",
    "ensure_folders",
    "load_routes",
    "move_messages",
    "parse_metadata",
    "record_current_mail_decision",
    "render_message",
]
