#!/usr/bin/env python3
"""Fail closed unless a Kanban plan reference resolves to approved Git bytes."""

import argparse
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys


FULL_SHA = re.compile(r"[0-9a-fA-F]{40}")
IDENTIFIER = re.compile(r"[A-Z0-9][A-Z0-9-]*")
SAFE_PATH = re.compile(r"[A-Za-z0-9._/-]+")


class PlanRefError(ValueError):
    """The referenced plan cannot be proven from the requested Git commit."""


def _git(repo, *args, text=True):
    return subprocess.run(
        ("git", *args),
        cwd=repo,
        check=False,
        capture_output=True,
        text=text,
    )


def _safe_path(raw):
    path = PurePosixPath(raw)
    if (
        not SAFE_PATH.fullmatch(raw)
        or path.is_absolute()
        or not path.parts
        or path.as_posix() != raw
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise PlanRefError("path must be a normalized repository-relative POSIX path")
    return path.as_posix()


def _identifier(raw, label):
    if not IDENTIFIER.fullmatch(raw):
        raise PlanRefError(f"{label} must match {IDENTIFIER.pattern}")
    return raw


def _structural_marker_count(content, marker):
    """Count standalone markers outside Markdown fenced code blocks."""
    count = 0
    fence = None
    for line in content.splitlines():
        stripped = line.lstrip()
        match = re.match(r"(`{3,}|~{3,})", stripped)
        if match:
            fence_run = match.group(1)
            if fence is None:
                fence = (fence_run[0], len(fence_run))
            elif fence_run[0] == fence[0] and len(fence_run) >= fence[1]:
                fence = None
            continue
        if fence is None and line == marker:
            count += 1
    return count


def verify_plan_ref(repo, commit, path, plan_id, sections):
    """Return verified reference metadata or raise PlanRefError."""
    repo = Path(repo).resolve()
    if not FULL_SHA.fullmatch(commit):
        raise PlanRefError("commit must be a full 40-character SHA")
    commit = commit.lower()
    path = _safe_path(path)
    plan_id = _identifier(plan_id, "plan_id")
    if not sections:
        raise PlanRefError("at least one section is required")
    sections = [_identifier(section, "section") for section in sections]
    if len(sections) != len(set(sections)):
        raise PlanRefError("duplicate requested section")

    root = _git(repo, "rev-parse", "--show-toplevel")
    if root.returncode != 0 or Path(root.stdout.strip()).resolve() != repo:
        raise PlanRefError("repo must be the Git worktree root")

    resolved = _git(repo, "rev-parse", "--verify", f"{commit}^{{commit}}")
    if resolved.returncode != 0 or resolved.stdout.strip().lower() != commit:
        raise PlanRefError("commit is not available as the exact requested commit")

    head = _git(repo, "rev-parse", "--verify", "HEAD^{commit}")
    head_sha = head.stdout.strip().lower()
    if head.returncode != 0 or not FULL_SHA.fullmatch(head_sha):
        raise PlanRefError("cannot resolve HEAD")

    ancestor = _git(repo, "merge-base", "--is-ancestor", commit, head_sha)
    if ancestor.returncode != 0:
        raise PlanRefError("approved plan commit is not an ancestor of HEAD")

    shown = _git(repo, "show", f"{commit}:{path}", text=False)
    if shown.returncode != 0:
        raise PlanRefError("path does not exist in the approved plan commit")
    try:
        content = shown.stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PlanRefError("plan bytes are not UTF-8 text") from exc

    plan_marker = f"<!-- PLAN_ID:{plan_id} -->"
    if content.count(plan_marker) != 1 or _structural_marker_count(content, plan_marker) != 1:
        raise PlanRefError("plan_id marker is missing or not unique")
    for section in sections:
        marker = f"<!-- PLAN_SECTION:{section} -->"
        if content.count(marker) != 1 or _structural_marker_count(content, marker) != 1:
            raise PlanRefError(f"section marker is missing or not unique: {section}")

    return {
        "commit": commit,
        "head": head_sha,
        "path": path,
        "plan_id": plan_id,
        "sections": sections,
        "status": "PASS",
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=".")
    parser.add_argument("--commit", required=True)
    parser.add_argument("--path", required=True)
    parser.add_argument("--plan-id", required=True)
    parser.add_argument("--section", action="append", dest="sections", required=True)
    args = parser.parse_args(argv)
    try:
        result = verify_plan_ref(
            args.repo,
            args.commit,
            args.path,
            args.plan_id,
            args.sections,
        )
    except (OSError, subprocess.SubprocessError, PlanRefError) as exc:
        print(
            json.dumps(
                {"error": str(exc), "status": "FAIL"},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
