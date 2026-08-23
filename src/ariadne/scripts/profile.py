"""Inspect the complete effective configuration of an Ariadne turn profile."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from ariadne.codex.models import ResolvedTurnProfile
from ariadne.codex.resolver import resolve_profile
from ariadne.config import load_settings
from ariadne.profile import PROFILES


def profile_payload(profile: ResolvedTurnProfile) -> dict[str, Any]:
    """Return an inspection-safe profile with no environment values."""
    return {
        "name": profile.name,
        "model": profile.model,
        "reasoning_effort": profile.effort.value,
        "web_search": profile.web_search,
        "thread_policy": profile.thread_policy,
        "cwd": str(profile.cwd),
        "sandbox": profile.sandbox.value,
        "approval_mode": profile.approval_mode.value,
        "permission_profile": profile.permission_profile,
        "writable_roots": [str(root) for root in profile.writable_roots],
        "network_domains": list(profile.network_domains),
        "allow_local_binding": profile.allow_local_binding,
        "enabled_tools": list(profile.enabled_tools),
        "mcp_environment_names": list(profile.mcp_environment_names),
        "instruction_documents": list(profile.profile.instruction_documents),
        "developer_documents": list(profile.profile.developer_documents),
        "base_instruction_sources": list(profile.base_instruction_sources),
        "developer_instruction_sources": list(profile.developer_instruction_sources),
        "base_instructions": profile.base_instructions,
        "developer_instructions": profile.developer_instructions,
    }


def render_profile(profile: ResolvedTurnProfile) -> str:
    payload = profile_payload(profile)
    lines = [
        f"Profile: {payload['name']}",
        f"Model: {payload['model']}",
        f"Reasoning effort: {payload['reasoning_effort']}",
        f"Web search: {payload['web_search']}",
        f"Thread policy: {payload['thread_policy']}",
        f"Working directory: {payload['cwd']}",
        f"Sandbox: {payload['sandbox']}",
        f"Approval mode: {payload['approval_mode']}",
        f"Permission profile: {payload['permission_profile']}",
        "Writable roots: " + ", ".join(payload["writable_roots"]),
        "Network domains: " + ", ".join(payload["network_domains"]),
        f"Allow local binding: {payload['allow_local_binding']}",
        "Enabled MCP tools: " + ", ".join(payload["enabled_tools"]),
        "MCP environment names: " + ", ".join(payload["mcp_environment_names"]),
        "Instruction documents: " + ", ".join(payload["instruction_documents"]),
        "Developer documents: " + ", ".join(payload["developer_documents"]),
        "Base instruction sources: " + ", ".join(payload["base_instruction_sources"]),
        "Developer instruction sources: "
        + ", ".join(payload["developer_instruction_sources"]),
        "",
        "--- Base instructions ---",
        payload["base_instructions"],
        "",
        "--- Developer instructions ---",
        payload["developer_instructions"],
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", choices=tuple(PROFILES))
    parser.add_argument("--config", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    settings = load_settings(args.config)
    profile = resolve_profile(
        PROFILES[args.profile],
        vault=settings.vault,
        settings=settings.turn_settings(args.profile),
        human=settings.human_name,
        mcp_environment=settings.mcp_environment,
    )

    if args.json:
        print(json.dumps(profile_payload(profile), indent=2))
    else:
        print(render_profile(profile))


if __name__ == "__main__":
    main()
