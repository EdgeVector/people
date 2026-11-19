#!/usr/bin/env python3
"""Open or update one issue for a red DEV sharing run, and close it after a real success.

This is the People copy of EdgeVector/lastdb scripts/ci/notify_scheduled_failure.py.
LastDB's reusable workflow cannot be called from here: it checks out the caller
and runs that script, and the script accepts only EdgeVector/lastdb.

An hourly poll that skips sharing is not a success and does not close the issue.
A manual dispatch does not open or close it.
"""

import json
import os
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, HTTPRedirectHandler

REPOSITORY = "EdgeVector/people"
ASSIGNEE = "shiba4life"
LABEL = "scheduled-failure"
MARKER = "people-scheduled-failure"
WORKFLOW = "dev-sharing.yml"
WORKFLOW_NAME = "DEV sharing"
RESULTS = {"success", "failure", "cancelled", "skipped"}
MAX_BODY = 1024 * 1024
TAG_PATTERN = re.compile(r"nightly-[0-9]{8}-[0-9a-f]{9}")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise SystemExit("notify_github_redirect")


def note(text):
    print(text, flush=True)
    path = os.environ.get("GITHUB_STEP_SUMMARY", "")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(text + "\n")


def conclusion(results, sharing):
    if sharing not in RESULTS:
        raise SystemExit("notify_sharing_result")
    if any(item == "failure" for item in results):
        return "failure"
    if any(item == "cancelled" for item in results) or "success" not in results:
        return "ignored"
    # Skipped sharing did not prove the nightly. Leave an open issue open.
    if sharing != "success":
        return "ignored"
    if all(item in ("success", "skipped") for item in results):
        return "success"
    return "ignored"


def parse_results(raw):
    items = raw.split()
    if not items or any(item not in RESULTS for item in items):
        raise SystemExit("notify_job_result")
    return items


def marker():
    return f"<!-- {MARKER}:{WORKFLOW} -->"


def nightly_tag():
    tag = os.environ.get("NIGHTLY_TAG", "")
    if TAG_PATTERN.fullmatch(tag):
        return tag
    return ""


def run_context():
    server = os.environ.get("GITHUB_SERVER_URL", "")
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    sha = os.environ.get("GITHUB_SHA", "")
    event = os.environ.get("GITHUB_EVENT_NAME", "")
    if (server != "https://github.com" or repository != REPOSITORY
            or not re.fullmatch(r"[1-9][0-9]{0,19}", run_id)
            or not re.fullmatch(r"[0-9a-f]{40}", sha)
            or event not in {"schedule", "push"}):
        raise SystemExit("notify_run_identity")
    return {"url": f"{server}/{repository}/actions/runs/{run_id}", "sha": sha, "event": event}


class GitHub:
    def __init__(self, value):
        if not 0 < len(value) <= 4096 or any(ord(char) < 33 or ord(char) > 126 for char in value):
            raise SystemExit("notify_token")
        self.calls = 0
        self.opener = build_opener(NoRedirect())
        self.headers = {
            "Authorization": "Bearer " + value,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        }

    def call(self, method, path, payload=None, *, missing=False, exists=False):
        if not path.startswith("/") or ".." in path or path.startswith("//"):
            raise SystemExit("notify_github_path")
        self.calls += 1
        if self.calls > 30:
            raise SystemExit("notify_github_call_bound")
        data = None if payload is None else json.dumps(payload).encode()
        if data is not None and len(data) > 65536:
            raise SystemExit("notify_github_payload")
        request = Request("https://api.github.com/repos/" + REPOSITORY + path,
                          data=data, headers=self.headers, method=method)
        try:
            with self.opener.open(request, timeout=20) as response:
                raw = response.read(MAX_BODY + 1)
                if response.status not in (200, 201) or len(raw) > MAX_BODY:
                    raise SystemExit("notify_github_response")
        except HTTPError as error:
            error.read(4096)
            error.close()
            if error.code == 404 and missing:
                return None
            if error.code == 422 and exists:
                return None
            raise SystemExit("notify_github_http_" + str(error.code)) from None
        except (URLError, TimeoutError, OSError):
            raise SystemExit("notify_github_transport") from None
        if not raw.strip():
            return None
        try:
            return json.loads(raw)
        except ValueError:
            raise SystemExit("notify_github_json") from None


def ensure_label(api):
    if api.call("GET", "/labels/" + LABEL, missing=True) is None:
        api.call("POST", "/labels", {
            "name": LABEL, "color": "b60205",
            "description": "Open while a scheduled workflow is failing"}, exists=True)


def listed_issues(api):
    found = []
    for page in range(1, 4):
        batch = api.call("GET", f"/issues?state=all&labels={LABEL}&per_page=100&page={page}", missing=True)
        if batch is None:
            return []
        if not isinstance(batch, list) or len(batch) > 100:
            raise SystemExit("notify_issue_page")
        found.extend(item for item in batch if isinstance(item, dict) and "pull_request" not in item)
        if len(batch) < 100:
            return found
    raise SystemExit("notify_issue_page_bound")


def tracked(issues):
    token = marker()
    matches = [issue for issue in issues if token in (issue.get("body") or "")]
    matches.sort(key=lambda issue: issue["number"])
    for issue in matches:
        if type(issue.get("number")) is not int or issue["number"] <= 0:
            raise SystemExit("notify_issue_number")
    return matches


def assigned(issue):
    people = issue.get("assignees") or []
    return any(isinstance(person, dict) and person.get("login") == ASSIGNEE for person in people)


def comment_has(api, number, url):
    comments = api.call("GET", f"/issues/{number}/comments?per_page=100")
    if not isinstance(comments, list):
        raise SystemExit("notify_issue_comments")
    return any(isinstance(item, dict) and url in (item.get("body") or "") for item in comments)


def detail(context):
    tag = nightly_tag()
    nightly = f", nightly `{tag}`" if tag else ""
    return f"{context['url']} (`{context['sha']}`, {context['event']}{nightly})"


def record_failure(api, issue, context):
    number = issue["number"]
    if issue.get("state") != "open" or not assigned(issue):
        api.call("PATCH", f"/issues/{number}", {"state": "open", "assignees": [ASSIGNEE]})
    text = detail(context)
    if context["url"] not in (issue.get("body") or "") and not comment_has(api, number, context["url"]):
        api.call("POST", f"/issues/{number}/comments", {"body": f"Failed again: {text}."})
    note(f"Updated issue #{number}")


def record_success(api, issue, context):
    if issue.get("state") != "open":
        return
    number = issue["number"]
    api.call("POST", f"/issues/{number}/comments", {"body": f"Recovered: {detail(context)}."})
    api.call("PATCH", f"/issues/{number}", {"state": "closed", "state_reason": "completed"})
    note(f"Closed issue #{number}")


def close_duplicate(api, issue, canonical):
    if issue.get("state") != "open":
        return
    number = issue["number"]
    api.call("POST", f"/issues/{number}/comments", {"body": f"Closing duplicate of #{canonical}."})
    api.call("PATCH", f"/issues/{number}", {"state": "closed", "state_reason": "not_planned"})


def create_issue(api, context):
    tag = nightly_tag()
    lines = [
        marker(), "",
        f"Scheduled workflow **{WORKFLOW_NAME}** failed.", "",
        f"- Workflow: `.github/workflows/{WORKFLOW}`",
        f"- Run: {context['url']}",
        f"- Commit: `{context['sha']}`",
        f"- Event: {context['event']}",
    ]
    if tag:
        lines.append(f"- Nightly: `{tag}`")
    lines.extend(["", "This issue stays open until a later schedule or push run starts sharing and succeeds.", ""])
    created = api.call("POST", "/issues", {
        "title": f"Scheduled failure: {WORKFLOW_NAME}",
        "body": "\n".join(lines),
        "labels": [LABEL],
        "assignees": [ASSIGNEE]})
    if not isinstance(created, dict) or type(created.get("number")) is not int:
        raise SystemExit("notify_issue_create")
    note(f"Opened issue #{created['number']}")


def main():
    event = os.environ.get("GITHUB_EVENT_NAME", "")
    if event not in {"schedule", "push"}:
        note("Notification skipped for this event")
        return 0
    if os.environ.get("GITHUB_REF") != "refs/heads/main":
        note("Notification skipped off main")
        return 0
    if os.environ.get("WORKFLOW_FILE", "") != WORKFLOW:
        raise SystemExit("notify_workflow")
    outcome = conclusion(parse_results(os.environ.get("JOB_RESULTS", "")), os.environ.get("SHARING_RESULT", ""))
    note(WORKFLOW + ": " + outcome)
    if outcome == "ignored":
        return 0
    context = run_context()
    api = GitHub(os.environ.get("GH_TOKEN", ""))
    if outcome == "failure":
        ensure_label(api)
    matches = tracked(listed_issues(api))
    if outcome == "failure" and not matches:
        create_issue(api, context)
        return 0
    if outcome == "failure":
        record_failure(api, matches[0], context)
        for extra in matches[1:]:
            close_duplicate(api, extra, matches[0]["number"])
        return 0
    for issue in matches:
        record_success(api, issue, context)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
