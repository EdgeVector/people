#!/usr/bin/env python3
"""Wait until every earlier DEV sharing run on the DEV publisher account is done.

The sharing legs of all main runs use one DEV publisher account, so only one
run may be in its sharing legs at a time. A workflow concurrency group cannot
hold that line: GitHub keeps one pending run per group and cancels the pending
one when another arrives, which failed the LastDB gate that dispatched it.

This job is the line instead. Runs are ordered by the time the run (or its
re-run) started, then by run id. A run waits while any run ahead of it in that
order has not completed. The order is total, so two runs never wait on each
other, and a run that is in its sharing legs keeps every later run waiting.

A list request that fails is never read as "nobody is ahead": the job keeps
waiting and fails at its limit.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

WORKFLOW = "dev-sharing.yml"
# The newest runs of this workflow. A run still active is always among them:
# no run lasts long enough for a hundred newer ones to start.
PAGE = 100
POLL_SECONDS = 30
DEFAULT_WAIT_SECONDS = 90 * 60


def gh_json(path: str) -> dict | None:
    result = subprocess.run(
        ["gh", "api", path], capture_output=True, text=True, timeout=120
    )
    if result.returncode != 0:
        print(
            f"wait-for-turn: request failed: {(result.stderr or '').strip()[:200]}",
            flush=True,
        )
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        print("wait-for-turn: reply was not JSON", flush=True)
        return None


def position(run: dict) -> tuple[str, int]:
    """Start time of this attempt, then run id. Times are UTC ISO strings."""
    return (run.get("run_started_at") or run.get("created_at") or "", int(run["id"]))


def runs_ahead(repo: str, mine: tuple[str, int]) -> list[dict] | None:
    listing = gh_json(
        f"repos/{repo}/actions/workflows/{WORKFLOW}/runs?per_page={PAGE}"
    )
    if listing is None:
        return None
    ahead = []
    for run in listing.get("workflow_runs", []):
        # A pull request run never reaches the publisher account.
        if run.get("event") == "pull_request":
            continue
        if run.get("status") == "completed":
            continue
        if position(run) < mine:
            ahead.append(run)
    return ahead


def describe(runs: list[dict]) -> str:
    return ", ".join(
        f"{run['id']} ({run.get('status')}, {run.get('display_title') or run.get('name')})"
        for run in sorted(runs, key=position)
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--wait-seconds",
        type=int,
        default=DEFAULT_WAIT_SECONDS,
        help="Fail when earlier runs are still active after this long (default 5400)",
    )
    args = parser.parse_args()
    repo = os.environ["GITHUB_REPOSITORY"]
    run_id = int(os.environ["GITHUB_RUN_ID"])
    deadline = time.monotonic() + args.wait_seconds

    own = None
    while own is None:
        own = gh_json(f"repos/{repo}/actions/runs/{run_id}")
        if own is None:
            if time.monotonic() >= deadline:
                print("wait-for-turn: FAIL could not read this run", file=sys.stderr)
                return 1
            time.sleep(POLL_SECONDS)
    mine = position(own)
    print(f"wait-for-turn: this run is {mine[1]}, started {mine[0]}", flush=True)

    ahead: list[dict] | None = None
    while time.monotonic() < deadline:
        ahead = runs_ahead(repo, mine)
        if ahead is not None and not ahead:
            print("wait-for-turn: no earlier run is active", flush=True)
            return 0
        if ahead:
            print(f"wait-for-turn: waiting for {describe(ahead)}", flush=True)
        time.sleep(POLL_SECONDS)
    still = describe(ahead) if ahead else "unknown: the last list request failed"
    print(
        f"wait-for-turn: FAIL earlier runs still active after {args.wait_seconds}s: {still}",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
