"""Privacy-safe descriptions of concrete Codex activity."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import PurePath

from openai_codex.generated.v2_all import (
    CollabAgentToolCallThreadItem,
    CommandExecutionThreadItem,
    DynamicToolCallThreadItem,
    FileChangeThreadItem,
    ImageGenerationThreadItem,
    ImageViewThreadItem,
    McpToolCallThreadItem,
    WebSearchThreadItem,
)

MCP_SERVER_NAME = "ariadne"
TELEGRAM_TOOLS = ("send_telegram_message", "ask_telegram_question")


@dataclass(frozen=True, slots=True)
class ActivityDescription:
    """Safe labels for the active and just-completed phases of one item."""

    started: str
    completed: str


def _activity(
    started: str, completed: str = "Reviewing results…"
) -> ActivityDescription:
    return ActivityDescription(started, completed)


_KNOWLEDGE_ACTIVITY = {
    "search_knowledge": _activity("Searching memory…", "Using remembered context…"),
    "list_knowledge": _activity("Browsing memory…", "Using remembered context…"),
    "read_knowledge": _activity("Reading memory…", "Using remembered context…"),
    "create_knowledge": _activity("Remembering…", "Checking memory…"),
    "update_knowledge": _activity("Updating memory…", "Checking memory…"),
    "archive_knowledge": _activity("Organising memory…", "Checking memory…"),
}
_REVISIT_ACTIVITY = {
    "schedule_wakeup": _activity(
        "Scheduling a future wake-up…", "Checking the schedule…"
    ),
    "list_wakeups": _activity(
        "Checking scheduled wake-ups…", "Reviewing the schedule…"
    ),
    "update_wakeup": _activity(
        "Updating a scheduled wake-up…", "Checking the schedule…"
    ),
    "cancel_wakeup": _activity(
        "Cancelling a scheduled wake-up…", "Checking the schedule…"
    ),
}
_LOCAL_ACTIVITY = {
    "read_recent_telegram_messages": _activity(
        "Reading recent messages…", "Using recent context…"
    ),
    "request_telegram_file_delivery": _activity(
        "Preparing files…", "Checking the files…"
    ),
    "record_current_mail_decision": _activity(
        "Triaging mail…", "Checking the mail decision…"
    ),
}

_ARIADNE_COMMAND_ACTIVITY: dict[tuple[str, ...], ActivityDescription] = {
    ("config", "check"): _activity(
        "Checking Ariadne's configuration…", "Reviewing configuration…"
    ),
    ("config", "show"): _activity(
        "Reading Ariadne's configuration…", "Reviewing configuration…"
    ),
    ("mail", "search"): _activity("Searching mail…", "Reviewing mail…"),
    ("mail", "read"): _activity("Reading mail…", "Reviewing mail…"),
    ("mail", "thread"): _activity("Reading a mail thread…", "Reviewing mail…"),
    ("calendar", "list"): _activity("Listing calendars…", "Reviewing calendars…"),
    ("calendar", "search"): _activity(
        "Searching the calendar…", "Reviewing calendar events…"
    ),
    ("calendar", "read"): _activity(
        "Reading a calendar event…", "Reviewing the calendar event…"
    ),
    ("calendar", "availability"): _activity(
        "Checking calendar availability…", "Reviewing availability…"
    ),
    ("calendar", "create"): _activity(
        "Creating a calendar event…", "Checking the calendar update…"
    ),
    ("calendar", "update"): _activity(
        "Updating a calendar event…", "Checking the calendar update…"
    ),
    ("calendar", "delete"): _activity(
        "Deleting a calendar event…", "Checking the calendar update…"
    ),
    ("calendar", "respond"): _activity(
        "Responding to a calendar invitation…", "Checking the response…"
    ),
    ("health", "workouts", "search"): _activity(
        "Searching workout history…", "Reviewing workouts…"
    ),
    ("health", "workouts", "summarize"): _activity(
        "Summarising workouts…", "Reviewing workout totals…"
    ),
    ("health", "workouts", "show"): _activity(
        "Reading a workout…", "Reviewing workout details…"
    ),
}

_SHELL_METACHARACTERS = frozenset("|&;<>()`$\n\r")
_ENVIRONMENT_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*", re.DOTALL)
_UV_VALUE_OPTIONS = frozenset(("--directory", "--project", "--python"))
_UV_FLAG_OPTIONS = frozenset(("--frozen", "--locked", "--no-sync", "--offline"))


def _simple_tokens(command: str) -> tuple[str, ...]:
    """Tokenize only a single uncomplicated command, never a shell program."""
    if any(character in command for character in _SHELL_METACHARACTERS):
        return ()
    try:
        tokens = tuple(shlex.split(command))
    except ValueError:
        return ()
    return tokens


def _program(value: str) -> str:
    return PurePath(value).name


def _strip_environment(tokens: tuple[str, ...]) -> tuple[str, ...]:
    """Ignore conventional leading environment assignments without reading values."""
    index = 0
    if tokens and _program(tokens[0]) == "env":
        index = 1
    while index < len(tokens) and _ENVIRONMENT_ASSIGNMENT.fullmatch(tokens[index]):
        index += 1
    return tokens[index:]


def _unwrap_runner(tokens: tuple[str, ...]) -> tuple[str, ...]:
    """Recognize conventional Python and uv launchers without exposing arguments."""
    if not tokens:
        return ()
    program = _program(tokens[0])
    if program.startswith("python") and len(tokens) >= 3 and tokens[1] == "-m":
        return (tokens[2], *tokens[3:])
    if program != "uv" or len(tokens) < 3 or tokens[1] != "run":
        return tokens

    index = 2
    while index < len(tokens):
        option = tokens[index]
        if option in _UV_FLAG_OPTIONS:
            index += 1
            continue
        if option in _UV_VALUE_OPTIONS and index + 1 < len(tokens):
            index += 2
            continue
        break
    return tokens[index:]


def _ariadne_activity(tokens: tuple[str, ...]) -> ActivityDescription | None:
    if not tokens or _program(tokens[0]) != "ariadne":
        return None
    index = 1
    while index < len(tokens):
        option = tokens[index]
        if option == "--pretty":
            index += 1
            continue
        if option == "--config" and index + 1 < len(tokens):
            index += 2
            continue
        if option.startswith("--config="):
            index += 1
            continue
        break
    for width in (3, 2):
        key = tokens[index : index + width]
        if len(key) == width and key in _ARIADNE_COMMAND_ACTIVITY:
            return _ARIADNE_COMMAND_ACTIVITY[key]
    return None


def _common_command_activity(tokens: tuple[str, ...]) -> ActivityDescription | None:
    if not tokens:
        return None
    program = _program(tokens[0])
    arguments = tokens[1:]

    package_command = program in {"npm", "pnpm", "yarn", "bun"}
    if (
        program == "pytest"
        or (program in {"cargo", "go"} and arguments[:1] == ("test",))
        or (
            package_command
            and (arguments[:1] == ("test",) or arguments[:2] == ("run", "test"))
        )
    ):
        return _activity("Running tests…", "Reviewing test results…")
    if program in {"mypy", "pyright", "pyrefly", "ty"}:
        return _activity("Checking types…", "Reviewing type-check results…")
    if program == "ruff":
        if arguments[:1] == ("format",):
            verb = (
                "Checking formatting…" if "--check" in arguments else "Formatting code…"
            )
            return _activity(verb, "Reviewing formatting…")
        return _activity("Checking code quality…", "Reviewing code-quality results…")
    if program in {"eslint", "biome"}:
        return _activity("Checking code quality…", "Reviewing code-quality results…")
    if program == "git" and arguments:
        action = arguments[0]
        if action in {"status", "diff", "log", "show", "rev-parse"}:
            started = "Inspecting the repository…"
        elif action in {"fetch", "pull"}:
            started = "Syncing the repository…"
        elif action == "push":
            started = "Pushing changes…"
        elif action == "commit":
            started = "Committing changes…"
        elif action == "add":
            started = "Staging changes…"
        elif action in {"switch", "checkout"}:
            started = "Switching branches…"
        elif action in {"merge", "rebase"}:
            started = "Updating the branch…"
        else:
            started = "Working with Git…"
        return _activity(started, "Checking the repository…")
    if program == "gh" and arguments:
        if arguments[:2] == ("pr", "create"):
            return _activity("Opening a pull request…", "Checking the pull request…")
        if arguments[:2] in {
            ("pr", "checks"),
            ("run", "watch"),
        }:
            return _activity("Waiting for checks…", "Reviewing checks…")
        return _activity("Checking GitHub…", "Reviewing GitHub…")
    if (
        program == "make"
        or (program == "cmake" and arguments[:1] == ("--build",))
        or (program in {"cargo", "go"} and arguments[:1] == ("build",))
        or (package_command and arguments[:1] == ("build",))
        or (package_command and arguments[:2] == ("run", "build"))
    ):
        return _activity("Building the project…", "Reviewing the build…")
    return None


def _structured_command_activity(
    item: CommandExecutionThreadItem,
) -> ActivityDescription | None:
    kinds = {action.root.type for action in item.command_actions}
    if not kinds or "unknown" in kinds:
        return None
    if kinds == {"search"}:
        return _activity("Searching files…", "Reviewing matches…")
    if kinds == {"read"}:
        return _activity("Reading files…", "Reviewing files…")
    if kinds == {"listFiles"}:
        return _activity("Browsing files…", "Reviewing files…")
    if kinds <= {"search", "read", "listFiles"}:
        return _activity("Inspecting files…", "Reviewing files…")
    return None


def _command_activity(item: CommandExecutionThreadItem) -> ActivityDescription:
    tokens = _unwrap_runner(_strip_environment(_simple_tokens(item.command)))
    return (
        _ariadne_activity(tokens)
        or _common_command_activity(tokens)
        or _structured_command_activity(item)
        or _activity("Running a command…", "Reviewing command results…")
    )


def describe_activity(item: object) -> ActivityDescription | None:
    """Return useful activity labels without exposing item arguments or results."""
    if isinstance(item, WebSearchThreadItem):
        return _activity("Searching the web…", "Reviewing sources…")
    if isinstance(item, McpToolCallThreadItem):
        if item.server == MCP_SERVER_NAME and item.tool in TELEGRAM_TOOLS:
            return None
        if item.server == MCP_SERVER_NAME:
            return (
                _KNOWLEDGE_ACTIVITY.get(item.tool)
                or _REVISIT_ACTIVITY.get(item.tool)
                or _LOCAL_ACTIVITY.get(item.tool)
                or _activity("Using Ariadne's local capability…", "Reviewing results…")
            )
        return _activity("Using a capability…", "Reviewing results…")
    if isinstance(item, CommandExecutionThreadItem):
        return _command_activity(item)
    if isinstance(item, FileChangeThreadItem):
        return _activity("Editing files…", "Checking changes…")
    if isinstance(item, ImageViewThreadItem):
        return _activity("Inspecting an image…", "Reviewing the image…")
    if isinstance(item, ImageGenerationThreadItem):
        return _activity("Creating an image…", "Finishing the image…")
    if isinstance(item, DynamicToolCallThreadItem):
        return _activity("Using a capability…", "Reviewing results…")
    if isinstance(item, CollabAgentToolCallThreadItem):
        return _activity("Coordinating work…", "Reviewing delegated work…")
    return None
