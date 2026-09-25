"""Behavioural tests for the commit-author credit path in add-contributor.yml.

The workflow credits three sets of people from one GraphQL sweep over every
merged pull request: the PR's own author, the linked authors and co-authors of
its commits, and the reporters of the issues it closed. The commit-author path
is what closes the superseded-PR attribution gap -- when a maintainer lands
someone's work as a replacement PR (commits cherry-picked or preserved via a
``Co-authored-by:`` trailer), the replacement PR's own author is the maintainer,
and only the commit-author path names the original contributor.

That logic lives in ``jq`` programs embedded in the "Collect merged-PR authors"
``run:`` block, which no other test touches. These tests extract those jq
programs from the workflow YAML and execute them for real against a synthetic
``merged.jsonl``, so the filters (linked user only, ``__typename == "User"`` bot
exclusion, three-way union dedup) are verified rather than assumed.

Skipped where the POSIX toolchain the scripts need (bash, jq) is unavailable,
which is the case on the Windows leg of the matrix.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "add-contributor.yml"

pytestmark = pytest.mark.skipif(
    not WORKFLOW.exists()
    or os.name == "nt"
    or shutil.which("bash") is None
    or shutil.which("jq") is None,
    reason="requires the workflow file plus a POSIX bash and jq",
)


def _collect_step_script() -> str:
    """The ``run:`` script of the 'Collect merged-PR authors' step."""
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    for step in doc["jobs"]["add"]["steps"]:
        name = step.get("name", "")
        if name.startswith("Collect merged-PR authors"):
            return step["run"]
    raise AssertionError("Collect merged-PR authors step not found in add-contributor.yml")


SCRIPT = _collect_step_script()


def _jq_program(dest_file: str) -> str:
    """Extract the jq program that writes ``dest_file`` from the collect step.

    The step runs ``jq -r '<program>' /tmp/merged.jsonl | sort -u >/tmp/<dest>``;
    pull the single-quoted program back out so the test runs the exact source,
    not a copy that could drift. Anchor on ``jq -r '`` and stop the program at
    the first ``' /tmp/merged.jsonl`` so the three same-shaped calls in the step
    do not bleed into one another (the jq programs contain no single quote).
    """
    m = re.search(
        r"jq -r '(?P<prog>[^']*)' /tmp/merged\.jsonl \| sort -u >/tmp/" + re.escape(dest_file),
        SCRIPT,
        re.DOTALL,
    )
    assert m, f"no jq program writing /tmp/{dest_file} found in the collect step"
    return m.group("prog")


def _run_jq(program: str, lines: list[dict]) -> list[str]:
    stdin = "\n".join(json.dumps(node) for node in lines) + "\n"
    proc = subprocess.run(
        ["jq", "-r", program],
        input=stdin,
        capture_output=True,
        # jq emits UTF-8; pin it so the decode does not fall back to the locale
        # encoding (the Windows ANSI code page under CI's subprocess-encoding gate).
        encoding="utf-8",
        check=True,
    )
    return sorted({lg for lg in proc.stdout.splitlines() if lg})


# One merged-PR node shaped like the real GraphQL response: a maintainer-authored
# replacement PR whose commits carry the original contributor (a real committer)
# plus a preserved co-author, alongside an unlinked commit email and a bot.
def _pr_node() -> dict:
    def user(login: str, typename: str = "User") -> dict:
        return {"user": {"login": login, "__typename": typename}}

    return {
        "author": {"login": "maintainer", "__typename": "User"},
        "commits": {
            "nodes": [
                {
                    "commit": {
                        "authors": {
                            "nodes": [
                                user("original-contributor"),
                                user("preserved-coauthor"),
                            ]
                        }
                    }
                },
                {
                    "commit": {
                        "authors": {
                            "nodes": [
                                {"user": None},  # unlinked email -> skipped
                                user("dependabot", "Bot"),  # bot -> skipped
                            ]
                        }
                    }
                },
            ]
        },
        "closingIssuesReferences": {
            "nodes": [{"author": {"login": "reporter", "__typename": "User"}}]
        },
    }


def test_query_fetches_commit_authors():
    """The GraphQL query must request each commit's linked authors."""
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    script = None
    for step in doc["jobs"]["add"]["steps"]:
        if step.get("name", "").startswith("Collect merged-PR authors"):
            script = step["run"]
    assert script is not None
    # Normalize whitespace so line wrapping in the YAML does not matter.
    flat = " ".join(script.split())
    assert "commits(first:" in flat.replace("commits(first: ", "commits(first:")
    assert "authors(first:" in flat.replace("authors(first: ", "authors(first:")
    assert "user { login __typename }" in flat or "user{ login __typename }" in flat


def test_commit_authors_jq_extracts_linked_users_only():
    program = _jq_program("commit_logins.txt")
    logins = _run_jq(program, [_pr_node()])
    # Both linked commit authors/co-authors are credited; the unlinked email and
    # the bot are dropped; the maintainer PR author is NOT in this file (it is a
    # separate path).
    assert logins == ["original-contributor", "preserved-coauthor"]


def test_pr_author_jq_still_isolated_from_commit_authors():
    program = _jq_program("pr_logins.txt")
    logins = _run_jq(program, [_pr_node()])
    assert logins == ["maintainer"]


def test_issue_reporter_jq_unchanged():
    program = _jq_program("issue_logins.txt")
    logins = _run_jq(program, [_pr_node()])
    assert logins == ["reporter"]


def test_union_credits_all_three_sets_deduped():
    """The three files are unioned with `sort -u`; the collect step ends with a
    `sort -u pr commit issue >logins`. Reproduce that union and assert the
    superseded-PR original author is credited exactly once."""
    node = _pr_node()
    pr = _run_jq(_jq_program("pr_logins.txt"), [node])
    commit = _run_jq(_jq_program("commit_logins.txt"), [node])
    issue = _run_jq(_jq_program("issue_logins.txt"), [node])
    union = sorted(set(pr) | set(commit) | set(issue))
    assert union == [
        "maintainer",
        "original-contributor",
        "preserved-coauthor",
        "reporter",
    ]
    # And the workflow really does union all three files (not just two).
    assert re.search(
        r"sort -u /tmp/pr_logins\.txt /tmp/commit_logins\.txt /tmp/issue_logins\.txt >/tmp/logins\.txt",
        SCRIPT,
    )


def test_commit_author_appearing_only_in_commits_is_still_credited():
    """A contributor who is neither the PR author nor an issue reporter -- only a
    commit co-author -- is the whole point: they must reach the union."""
    node = _pr_node()
    commit = _run_jq(_jq_program("commit_logins.txt"), [node])
    assert "original-contributor" in commit
    assert "preserved-coauthor" in commit
