"""Keep repository fixtures synthetic and machine-independent."""

import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
EMAIL = re.compile(r"[A-Z0-9._%+-]+@([A-Z0-9.-]+\.[A-Z]{2,})", re.IGNORECASE)
PERSONAL_HOME = re.compile(r"/(?:home|Users)/[^/\s]+")


def repository_text_files() -> tuple[Path, ...]:
    roots = (
        ROOT / "src",
        ROOT / "tests",
        ROOT / "docs",
        ROOT / ".github",
    )
    files = [
        ROOT / ".gitignore",
        ROOT / ".python-version",
        ROOT / "README.md",
        ROOT / "config.example.toml",
        ROOT / "mail-routes.example.yaml",
        ROOT / "pyproject.toml",
        ROOT / "uv.lock",
    ]
    for directory in roots:
        files.extend(
            path
            for path in directory.rglob("*")
            if path.is_file()
            and path.suffix in {".md", ".py", ".toml", ".yaml", ".yml"}
        )
    return tuple(files)


def test_repository_emails_use_the_reserved_example_domain() -> None:
    violations = []
    for path in repository_text_files():
        text = path.read_text(encoding="utf-8")
        for match in EMAIL.finditer(text):
            if match.group(1).casefold() != "example.com":
                violations.append(f"{path.relative_to(ROOT)}: {match.group(0)}")

    assert violations == []


def test_repository_has_no_absolute_user_home_paths() -> None:
    violations = []
    for path in repository_text_files():
        text = path.read_text(encoding="utf-8")
        for match in PERSONAL_HOME.finditer(text):
            violations.append(f"{path.relative_to(ROOT)}: {match.group(0)}")

    assert violations == []
