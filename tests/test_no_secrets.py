"""Secret scan over the working tree (not git history), so it works with or without git."""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SKIP_DIRS = {".git", ".venv", "venv", "env", "node_modules", "__pycache__", ".pytest_cache",
             "var", "data_volume", "chroma", ".mypy_cache", ".ruff_cache"}
SKIP_FILES = {".env"}          # local secrets file; gitignored and dockerignored (checked below)
MAX_BYTES = 1_000_000

KEY_PATTERNS = {
    "groq": re.compile(r"gsk_[A-Za-z0-9]{20,}"),
    "google": re.compile(r"AIza[0-9A-Za-z_\-]{35}"),
    "openai/anthropic": re.compile(r"sk-(?:ant-)?[A-Za-z0-9_\-]{20,}"),
    "aws": re.compile(r"AKIA[0-9A-Z]{16}"),
    "private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "assigned api key": re.compile(r"(?:GROQ|GEMINI|OPENAI)_API_KEY\s*=\s*['\"]?[A-Za-z0-9_\-]{16,}"),
}


def _files():
    for path in ROOT.rglob("*"):
        rel = path.relative_to(ROOT)
        if any(part in SKIP_DIRS for part in rel.parts) or not path.is_file():
            continue
        if path.name in SKIP_FILES or path.stat().st_size > MAX_BYTES:
            continue
        yield path


def find_secrets() -> list[str]:
    hits = []
    for path in _files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # binary or unreadable
        for name, pattern in KEY_PATTERNS.items():
            if pattern.search(text):
                hits.append(f"{path.relative_to(ROOT)}: looks like a {name}")
    return hits


def test_no_key_like_strings_in_tree():
    assert find_secrets() == []


def test_scanner_detects_a_planted_key(tmp_path, monkeypatch):
    fake = "gsk_" + "A1b2C3d4" * 4  # built at runtime so this file itself stays clean
    (tmp_path / "leak.py").write_text(f'KEY = "{fake}"\n')
    monkeypatch.setattr(sys.modules[__name__], "ROOT", tmp_path)
    assert any("groq" in hit for hit in find_secrets())


def test_env_file_is_ignored_by_git_and_docker():
    for ignore_file in (".gitignore", ".dockerignore"):
        lines = (ROOT / ignore_file).read_text().splitlines()
        assert ".env" in [line.strip() for line in lines], f".env missing from {ignore_file}"
