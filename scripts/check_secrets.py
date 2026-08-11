#!/usr/bin/env python3
"""Fail closed when staged or outgoing Git bytes contain likely secrets/PII."""

import argparse
from collections import Counter
from hashlib import sha256
from math import log2
import os
import re
import subprocess
import sys


DIRECT_PATTERNS = (
    (
        "private key",
        re.compile("-" * 5 + r"BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY" + "-" * 5),
    ),
    ("GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")),
    ("AWS access key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("Google API key", re.compile(r"\bAIza[A-Za-z0-9_-]{30,}\b")),
    ("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b")),
    ("Stripe live key", re.compile(r"\b(?:sk|rk)_live_[A-Za-z0-9]{16,}\b")),
    ("OpenAI/Anthropic key", re.compile(r"\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{20,}\b")),
    ("credential in URL", re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s/:]+:[^\s/@]+@", re.I)),
    ("email address", re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)),
    ("Taiwan mobile number", re.compile(r"(?<!\d)(?:\+886[- ]?|0)9\d{8}(?!\d)")),
    ("Taiwan national ID", re.compile(r"(?<![A-Za-z0-9])[A-Z][12]\d{8}(?![A-Za-z0-9])")),
)

LABEL = (
    r"(?:api[_-]?key|secret|token|password|passwd|credential|private[_-]?key|"
    r"client[_-]?secret|access[_-]?key|personal[_-]?(?:email|phone|address)|"
    r"phone[_-]?number|national[_-]?id|passport[_-]?number|ssn)"
)
LABELED_VALUE = re.compile(
    r"(?im)[\"']?(?P<label>(?:[A-Za-z0-9]+[._-])*" + LABEL + r")[\"']?"
    r"[ \t]*[:=][ \t]*(?P<value>[\"'][^\"'\r\n]+[\"']|[^\s,#}\r\n]+)",
)
ENTROPY_CANDIDATE = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z0-9_+/=-]{32,}(?![A-Za-z0-9_])")
PLACEHOLDER_WORDS = {
    "changeme",
    "dummy",
    "example",
    "fake",
    "not-a-secret",
    "placeholder",
    "redacted",
    "test",
    "your-key-here",
    "your_key_here",
}
SENSITIVE_SUFFIXES = (
    ".pem", ".key", ".p12", ".pfx", ".jks", ".keystore", ".mobileprovision",
    ".kdbx", ".ovpn", ".der", ".p8", ".pkcs12",
)
SENSITIVE_NAMES = {
    "auth.json", "token.json", "tokens.json", ".netrc", "application_default_credentials.json",
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
}
BINARY_SUFFIXES = (
    ".7z", ".a", ".avi", ".bin", ".class", ".db", ".dll", ".dmg", ".doc", ".docx",
    ".dylib", ".exe", ".gif", ".gz", ".heic", ".jar", ".jpeg", ".jpg", ".m4a",
    ".mov", ".mp3", ".mp4", ".numbers", ".o", ".pages", ".pdf", ".pkg", ".png",
    ".ppt", ".pptx", ".rar", ".so", ".sqlite", ".sqlite3", ".tar", ".wav", ".wasm",
    ".webp", ".xls", ".xlsx", ".xz", ".zip",
)


def _line(text, offset):
    return text.count("\n", 0, offset) + 1


def _placeholder(value):
    value = value.strip().strip("\"'")
    lower = value.lower()
    if not value:
        return True
    if re.fullmatch(r"\$?\{[A-Za-z_][A-Za-z0-9_.]{0,47}\}", value):
        return True
    if re.fullmatch(r"<(?:YOUR[_ -]?)?[A-Z][A-Z0-9_ -]*(?:KEY|TOKEN|SECRET|PASSWORD|VALUE)>", value, re.I):
        return True
    if re.fullmatch(r"(?:os\.)?environ\[[\"'][A-Za-z_][A-Za-z0-9_]*[\"']\]", value):
        return True
    if re.fullmatch(r"(?:os\.)?getenv\([\"'][A-Za-z_][A-Za-z0-9_]*[\"']\)", value):
        return True
    if lower in PLACEHOLDER_WORDS:
        return True
    if re.fullmatch(r"(?:dummy|example|fake|your)[_-](?:api[_-]?key|token|secret|password|value)", lower):
        return True
    return len(value) >= 6 and set(lower) <= {"x", "*", "-", "_"}


def _high_entropy(value):
    value = value.rstrip("=")
    if re.fullmatch(r"[0-9a-fA-F]+", value):
        return False
    classes = sum((any(c.islower() for c in value), any(c.isupper() for c in value), any(c.isdigit() for c in value)))
    if classes < 3:
        return False
    counts = Counter(value)
    entropy = -sum((count / len(value)) * log2(count / len(value)) for count in counts.values())
    return entropy >= 4.0


def findings_for_text(path, text):
    """Return (path, line, kind) findings without returning secret values."""
    findings = []
    seen = set()
    for kind, pattern in DIRECT_PATTERNS:
        for match in pattern.finditer(text):
            if kind == "email address" and match.group().lower().endswith(
                ("@example.com", "@example.invalid", "@users.noreply.github.com")
            ):
                continue
            item = (path, _line(text, match.start()), kind)
            key = item[:2]
            if key not in seen:
                seen.add(key)
                findings.append(item)
    url_spans = [
        match.span()
        for match in re.finditer(r"\b[a-z][a-z0-9+.-]*://[^\s\"']+", text, re.I)
    ]
    for match in ENTROPY_CANDIDATE.finditer(text):
        if any(match.start() < end and match.end() > start for start, end in url_spans):
            continue
        if not _high_entropy(match.group()):
            continue
        item = (path, _line(text, match.start()), "high-entropy credential-like value")
        key = item[:2]
        if key not in seen:
            seen.add(key)
            findings.append(item)
    for match in LABELED_VALUE.finditer(text):
        value = match.group("value").strip("\"'")
        if len(value) < 6 or _placeholder(value):
            continue
        item = (path, _line(text, match.start()), "labeled secret or personal data")
        key = item[:2]
        if key not in seen:
            seen.add(key)
            findings.append(item)
    return findings


def findings_for_path(path):
    """Block force-added credential containers even when their bytes are binary."""
    logical = re.sub(r"^[0-9a-f]{12}:", "", path).replace("\\", "/")
    parts = [part.lower() for part in logical.split("/")]
    name = parts[-1]
    if name == ".env.example" or name.endswith(".env.example"):
        return []
    if name.endswith(BINARY_SUFFIXES):
        return [(path, 0, "binary file requires explicit security review")]
    sensitive = (
        any(part in {"secrets", ".secrets", "credentials"} for part in parts[:-1])
        or name in SENSITIVE_NAMES
        or name == ".env"
        or name.startswith(".env.")
        or name.endswith(".env")
        or ".env." in name
        or ("credentials" in name and name.endswith(".json"))
        or (name.startswith("service-account") and name.endswith(".json"))
        or name.endswith(SENSITIVE_SUFFIXES)
    )
    return [(path, 0, "sensitive credential path")] if sensitive else []


def _git(*args):
    return subprocess.check_output(("git",) + args)


def _paths(output):
    return [os.fsdecode(path) for path in output.split(b"\0") if path]


def _text(blob):
    if b"\0" in blob:
        return None
    if blob.startswith((b"%PDF-", b"PK\x03\x04", b"GIF87a", b"GIF89a", b"RIFF")):
        return None
    if any(byte < 32 and byte not in {9, 10, 13} for byte in blob):
        return None
    try:
        return blob.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _display_path(path):
    if findings_for_path(path) or findings_for_text("<path>", path):
        digest = sha256(os.fsencode(path)).hexdigest()[:12]
        return "[REDACTED_PATH:{}]".format(digest)
    return path.encode("unicode_escape").decode("ascii")


def _staged_entries():
    names = _paths(_git("diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"))
    for path in names:
        yield path, _git("show", ":" + path)


def _tracked_entries():
    for path in _paths(_git("ls-files", "-z")):
        yield path, _git("show", ":" + path)


def _pre_push_entries(lines):
    zero = "0" * 40
    seen = set()
    for raw in lines:
        fields = raw.split()
        if len(fields) != 4:
            raise RuntimeError("unexpected pre-push input")
        local_sha, remote_sha = fields[1], fields[3]
        if local_sha == zero:
            continue
        revision = local_sha if remote_sha == zero else remote_sha + ".." + local_sha
        commits = _git("rev-list", revision).decode().splitlines()
        for commit in commits:
            # ponytail: O(commits × files) full-tree scan; dedupe blobs only if history grows.
            names = _paths(_git("ls-tree", "-r", "--name-only", "-z", commit))
            for path in names:
                identity = (commit, path)
                if identity in seen:
                    continue
                seen.add(identity)
                yield commit[:12] + ":" + path, _git("show", commit + ":" + path)


def _scan(entries):
    findings = []
    for path, blob in entries:
        path_findings = findings_for_path(path)
        findings.extend(path_findings)
        text = _text(blob)
        if text is None:
            if not path_findings:
                findings.append((path, 0, "binary file requires explicit security review"))
        else:
            findings.extend(findings_for_text(path, text))
    if findings:
        print("Secret/PII scan blocked this Git operation:", file=sys.stderr)
        for path, line, kind in findings:
            display_path = _display_path(path)
            location = "{}:{}".format(display_path, line) if line else display_path
            print("  {}: {}".format(location, kind), file=sys.stderr)
        print("Move secrets outside Git; never bypass with --no-verify.", file=sys.stderr)
        return 1
    print("Secret/PII scan: PASS")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--staged", action="store_true")
    mode.add_argument("--tracked", action="store_true")
    mode.add_argument("--pre-push", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.staged:
            entries = _staged_entries()
        elif args.tracked:
            entries = _tracked_entries()
        else:
            entries = _pre_push_entries(sys.stdin)
        return _scan(entries)
    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
        print("Secret/PII scan failed closed: {}".format(type(exc).__name__), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
