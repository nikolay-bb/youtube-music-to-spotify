#!/usr/bin/env python3
"""Check that nothing secret is about to reach GitHub.

    npm run security-check

Run it before every push. It never prints a secret value - only the file and
line where one was found, and which setting it came from.

What it checks
--------------
1. The files that hold secrets are ignored by git and are not tracked.
2. No tracked, staged or untracked-but-not-ignored file contains the actual
   values from your .env or your saved login tokens.
3. No such file contains anything *shaped* like a Google, Spotify or OAuth
   secret, whatever .env says. This catches a key pasted into a comment.
4. Nothing in the git history contains those values either. Deleting a
   commit does not remove it from history, so this matters even after a fix.
5. .env.example still has empty values.

Standard library only, so it runs before `npm run setup` has happened.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
THIS_FILE = Path(__file__).resolve()

# Paths that must never be committed. Directories end with a slash.
MUST_BE_IGNORED = [
    ".env",
    "backend/data/",
    "backend/data/oauth.json",
    "backend/data/spotify_token.json",
    "HANDOVER.md",
    ".playwright-mcp/",
]

# .env keys whose values are secrets, or identify your developer apps.
SECRET_KEY_WORDS = ("SECRET", "CLIENT_ID", "TOKEN", "PASSWORD", "API_KEY")

# Things that look like a credential no matter where they appear.
SHAPES = {
    "Google client secret": re.compile(r"GOCSPX-[A-Za-z0-9_\-]{20,}"),
    "Google client ID": re.compile(r"\b[0-9]{6,}-[a-z0-9]{20,}\.apps\.googleusercontent\.com\b"),
    "Google refresh token": re.compile(r"\b1//0[A-Za-z0-9_\-]{30,}"),
    "Google access token": re.compile(r"\bya29\.[A-Za-z0-9_\-]{30,}"),
    "Google API key": re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    "Spotify token": re.compile(r"\b[AB]Q[A-Za-z0-9_\-]{120,}"),
    "Private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----"),
    "Credential assignment": re.compile(
        r"(?i)\b(client_secret|refresh_token|access_token|api[_-]?key|password)\b"
        r"\s*[=:]\s*[\"']?[A-Za-z0-9_\-./+]{24,}"
    ),
}

# Files the shape scan skips: this script (it contains the patterns), lock
# files (long hashes that look like tokens), and binaries.
SKIP_SHAPE_SCAN = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml"}
MAX_SCAN_BYTES = 5 * 1024 * 1024

OK, BAD, WARN = "  OK  ", " FAIL ", " WARN "


def line(state: str, label: str, detail: str = "") -> None:
    print(f"[{state}] {label}" + (f"\n         {detail}" if detail else ""))


def git(*args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", *args], cwd=REPO, capture_output=True, text=True, check=False
    )
    if check and result.returncode not in (0, 1):
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def is_ignored(path: str) -> bool:
    return subprocess.run(
        ["git", "check-ignore", "-q", path], cwd=REPO, capture_output=True
    ).returncode == 0


def files_git_would_take() -> list[Path]:
    """Tracked or staged files, plus untracked ones git is not ignoring.

    The untracked ones matter because `git add .` would sweep them in.
    """
    tracked = git("ls-files", "-z").split("\0")
    untracked = git("ls-files", "-z", "--others", "--exclude-standard").split("\0")
    seen: list[Path] = []
    for name in tracked + untracked:
        if name and (REPO / name).is_file():
            seen.append(REPO / name)
    return seen


def read_text(path: Path) -> str | None:
    """The file's text, or None if it is binary or too big to bother with."""
    try:
        if path.stat().st_size > MAX_SCAN_BYTES:
            return None
        raw = path.read_bytes()
    except OSError:
        return None
    if b"\0" in raw[:8000]:
        return None
    return raw.decode("utf-8", errors="replace")


def secret_values() -> dict[str, str]:
    """Real secret values from .env and the token files, keyed by a label.

    The labels are what gets printed. The values never are.
    """
    found: dict[str, str] = {}

    env = REPO / ".env"
    if env.exists():
        for raw in env.read_text(errors="replace").splitlines():
            stripped = raw.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, _, value = stripped.partition("=")
            key, value = key.strip(), value.strip().strip("'\"")
            if any(word in key.upper() for word in SECRET_KEY_WORDS) and len(value) >= 8:
                found[f".env value of {key}"] = value

    for token_file in ("backend/data/oauth.json", "backend/data/spotify_token.json"):
        path = REPO / token_file
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(payload, dict):
            for field in ("access_token", "refresh_token", "id_token"):
                value = payload.get(field)
                if isinstance(value, str) and len(value) >= 8:
                    found[f"{token_file} {field}"] = value

    return found


def line_number(text: str, position: int) -> int:
    return text.count("\n", 0, position) + 1


def main() -> int:
    failures = 0
    print("\nSecurity check before pushing to GitHub\n" + "-" * 60)

    if not (REPO / ".git").exists():
        line(BAD, "Not a git repository", "Run this from inside the project after git init.")
        return 1

    # 1. The secret files are ignored and untracked. -------------------------
    tracked = set(git("ls-files", "-z").split("\0"))
    staged = set(git("diff", "--cached", "--name-only", "-z").split("\0"))
    for path in MUST_BE_IGNORED:
        bare = path.rstrip("/")
        if bare in tracked or bare in staged or any(
            name.startswith(bare + "/") for name in tracked | staged
        ):
            line(BAD, f"{path} is tracked or staged", f"Run: git rm -r --cached {bare}")
            failures += 1
        elif not is_ignored(path):
            line(BAD, f"{path} is not ignored", "Add it to .gitignore.")
            failures += 1
        else:
            line(OK, f"{path} is ignored")

    # 2 and 3. Scan every file git would take. -------------------------------
    candidates = files_git_would_take()
    secrets = secret_values()
    if not secrets:
        line(WARN, "No .env or token files found, so real values could not be checked",
             "The shape scan below still runs.")

    value_hits = 0
    shape_hits = 0
    for path in candidates:
        if path == THIS_FILE:
            continue
        text = read_text(path)
        if text is None:
            continue
        rel = path.relative_to(REPO)

        for label, value in secrets.items():
            position = text.find(value)
            if position != -1:
                line(BAD, f"{rel}:{line_number(text, position)} contains the {label}",
                     "Remove it, then rotate that credential on the developer dashboard.")
                value_hits += 1

        if path.name in SKIP_SHAPE_SCAN:
            continue
        for label, pattern in SHAPES.items():
            match = pattern.search(text)
            if match:
                line(BAD, f"{rel}:{line_number(text, match.start())} has something shaped like a {label}",
                     "If it is real, remove it and rotate it. If it is a placeholder, make it obviously fake.")
                shape_hits += 1

    failures += value_hits + shape_hits
    if not value_hits and secrets:
        line(OK, f"None of your {len(secrets)} real secret values appear in {len(candidates)} files git would take")
    if not shape_hits:
        line(OK, "Nothing shaped like a credential in those files")

    # 4. History. --------------------------------------------------------------
    commits = [c for c in git("rev-list", "--all").split() if c]
    if not commits:
        line(OK, "No commits yet, so the history is clean by definition")
    else:
        history_hits = 0
        for label, value in secrets.items():
            # -S finds commits that added or removed the string. The value goes
            # in as an argument, never through a shell, and is never printed.
            hits = git("log", "--all", "--format=%h", f"-S{value}").split()
            if hits:
                line(BAD, f"The {label} appears in {len(hits)} past commit(s): {', '.join(hits[:5])}",
                     "Rotate the credential now. Then rewrite history (git filter-repo) before pushing.")
                history_hits += 1
        # The working tree was covered above; now every commit, for shapes.
        scanned = commits[:500]
        for commit in scanned:
            listing = subprocess.run(
                ["git", "grep", "-I", "-l", "-E"]
                + [arg for p in SHAPES.values() for arg in ("-e", p.pattern)]
                + [commit, "--", ".", f":(exclude){THIS_FILE.relative_to(REPO)}"],
                cwd=REPO, capture_output=True, text=True,
            ).stdout.strip()
            if listing:
                files = ", ".join(entry.split(":", 1)[1] for entry in listing.splitlines()[:5])
                line(BAD, f"Commit {commit[:7]} contains something shaped like a credential in {files}",
                     "Rotate it, then rewrite history before pushing.")
                history_hits += 1
        if not history_hits:
            line(OK, f"Nothing found in {len(scanned)} commit(s) of history")
        failures += history_hits

    # 5. The example file is still an example. ---------------------------------
    example = REPO / ".env.example"
    if example.exists():
        filled = []
        for raw in example.read_text(errors="replace").splitlines():
            stripped = raw.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, _, value = stripped.partition("=")
            if any(word in key.upper() for word in SECRET_KEY_WORDS) and value.strip():
                filled.append(key.strip())
        if filled:
            line(BAD, ".env.example has filled-in values", f"Empty these: {', '.join(filled)}")
            failures += 1
        else:
            line(OK, ".env.example has empty values")

    print("-" * 60)
    if failures:
        print(f"\n{failures} problem(s). Do not push until they are fixed.\n")
        return 1
    print("\nNothing secret is going to GitHub. Safe to push.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
