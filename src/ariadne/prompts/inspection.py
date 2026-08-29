"""Inspection-safe views of the exact resolved prompts and turn profile."""

from __future__ import annotations

from typing import Any

from ..codex.models import ResolvedTurnProfile


def profile_payload(profile: ResolvedTurnProfile) -> dict[str, Any]:
    """Return one resolved profile without environment values."""
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
    """Render an inspectable profile and both exact instruction layers."""
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
