"""Compose Ariadne's local FastMCP server."""

from fastmcp import FastMCP

from .calendar import register_tools as register_calendar_tools
from .errors import install_safe_tool_error_handling
from .knowledge import register_tools as register_knowledge_tools
from .mail import register_tools as register_mail_tools
from .runtime import register_tools as register_runtime_tools
from .telegram import register_tools as register_telegram_tools


def create_server() -> FastMCP:
    """Create a server containing every Ariadne capability."""
    server = FastMCP(
        "Ariadne",
        instructions=(
            "Private knowledge, local runtime inspection, mail, calendar, and "
            "explicitly authorized Telegram capabilities."
        ),
        version="0.1.0",
        strict_input_validation=True,
    )
    install_safe_tool_error_handling(server)
    register_runtime_tools(server)
    register_telegram_tools(server)
    register_mail_tools(server)
    register_calendar_tools(server)
    register_knowledge_tools(server)
    return server


mcp = create_server()


def main() -> None:
    """Run the local server over FastMCP's default stdio transport."""
    mcp.run(show_banner=False)
