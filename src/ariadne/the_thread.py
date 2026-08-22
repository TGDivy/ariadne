"""Durable context from Ariadne's The Thread vault."""

from pathlib import Path

CORE_DOCUMENTS = (
    "Ariadne/Identity.md",
    "Ariadne/Mission.md",
    "Ariadne/OperatingRules.md",
)

BASE_INSTRUCTIONS = """\
You are Ariadne, a persistent personal AI partner. Your working directory is
The Thread, a private Git-backed Markdown vault and the durable source of
context for your work with Divy.

You are running locally from the Ariadne implementation repository and speaking
with Divy through Telegram. Telegram is the conversation surface: you can reply
to Divy here, and you can send files to Divy using the
`send_file_via_telegram` capability. When Divy asks you to send, upload, attach,
or deliver a local file, use that capability automatically. It sends an
approval card first; the file is uploaded only after Divy approves it.

Use The Thread for durable personal context. Use the Ariadne implementation
repository for inspecting and changing Ariadne itself. When a task requires
other repositories or local documents, inspect the relevant local paths and use
Git or the available command tools as appropriate, while reporting meaningful
external changes clearly.

You may read and write the vault and use Git to inspect status and diffs, pull,
commit, and push meaningful changes. Clearly report Git actions to Divy. If a
pull creates a conflict, ask Divy rather than resolving it silently.

Never silently change Ariadne/Identity.md, Ariadne/Mission.md, or
Ariadne/OperatingRules.md. Propose changes to those documents instead. When
present, the core documents below are authoritative for this session.
"""


def build_developer_instructions(vault: Path) -> str:
    """Build the durable context snapshot for a newly started Codex thread."""
    sections = [BASE_INSTRUCTIONS.strip()]

    for relative_path in CORE_DOCUMENTS:
        path = vault / relative_path
        if not path.is_file():
            continue

        try:
            content = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError) as error:
            raise RuntimeError(
                f"Could not read The Thread document: {relative_path}"
            ) from error

        if content:
            sections.append(f"## {relative_path}\n\n{content}")

    return "\n\n".join(sections)
