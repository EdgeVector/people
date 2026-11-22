"""What a people-circles failure saw.

The detail matches people-direct. A wait names each record read. attach()
adds each node's files, ops, and sync line. The shared helpers redact
before any clip.
"""

from direct_report import attach, label, refusal_note, row_report, watch
from support import OperationError


def fail(code, detail, **fields):
    raise OperationError(code, command_output=(detail, ""), **fields)


def check(condition, code, detail):
    if not condition:
        fail(code, detail)


def mark(value):
    """A short public mark. A long hex value keeps its first 12 characters."""
    if type(value) is int and 0 <= value <= 999999:
        return str(value)
    if isinstance(value, str) and len(value) >= 12 and all(char in "0123456789abcdefABCDEF" for char in value):
        return value[:12].lower()
    return label(value)


def saw(reason, **marks):
    body = " ".join("%s=%s" % (key, mark(value)) for key, value in marks.items())
    return "%s %s" % (reason, body) if body else reason


def note(row, field):
    if not isinstance(row, dict):
        return None
    fields = row.get("fields") if isinstance(row.get("fields"), dict) else {}
    text = fields.get(field)
    if not isinstance(text, str):
        text = fields.get("text") if isinstance(fields.get("text"), str) else None
    return {"fields": {"text": text}, "created_by": row.get("created_by"), "updated_by": row.get("updated_by")}


def cell(name, row, field):
    return row_report((name,), [note(row, field)], "saw").removeprefix("saw ")


def comment_line(name, page):
    if not isinstance(page, dict):
        return "%s unread" % name
    return "%s %s %s" % (
        name, cell("pub", page.get("publisher"), "social_comment_text"),
        cell("own", page.get("receiver"), "social_comment_text"))


def delete_line(name, item):
    if not isinstance(item, tuple):
        return comment_line(name, None)
    page, album = item
    album_row = album[0] if isinstance(album, list) and album else None
    return "%s %s" % (comment_line(name, page), cell("album", album_row, "title"))


def wait_field(name, stage, read, field, text):
    """One node, one key. The detail is that record, or absent."""
    def done(rows):
        return isinstance(rows, list) and len(rows) == 1 and _field_is(rows[0], field, text)

    def describe(rows, reason):
        row = rows[0] if isinstance(rows, list) and rows else None
        return "%s %s" % (reason, cell(name, row, field))

    return watch(stage, read, done, describe)


def require_absent(name, rows, code, field):
    row = rows[0] if isinstance(rows, list) and rows else None
    check(not rows, code, "present %s" % cell(name, row, field))


def wait_pages(named, stage, read, done, line):
    """Read once per named node. An error on one node stays in the detail."""
    state = {"notes": []}

    def load():
        pages, notes = [], []
        for name, node in named:
            try:
                pages.append(read(node))
            except OperationError as error:
                pages.append(None)
                notes.append("%s=%s:%s" % (name, mark(str(error)), refusal_note(error)))
        state["notes"] = notes
        return pages

    def describe(pages, reason):
        shown = pages if isinstance(pages, list) else []
        body = " ".join(line(name, shown[index] if index < len(shown) else None)
                        for index, (name, _node) in enumerate(named))
        text = "%s %s" % (reason, body)
        if state["notes"]:
            text = "%s unread=%s" % (text, ",".join(state["notes"]))
        return text

    def ready(pages):
        return not state["notes"] and done(pages)

    return watch(stage, load, ready, describe)


def _field_is(row, field, text):
    fields = row.get("fields") if isinstance(row, dict) else None
    return isinstance(fields, dict) and fields.get(field) == text


wait = watch


def status_text(found):
    """Known proposal states, joined. A longer list keeps only its length."""
    known = {"published", "unknown", "submitted"}
    parts = [item if item in known else "?" for item in found]
    text = ".".join(parts)
    return text if len(text) <= 39 else "n%s" % len(parts)


def inbox_line(page):
    if not isinstance(page, dict):
        return saw("inbox")
    entries = page.get("entries")
    count = len(entries) if isinstance(entries, list) else None
    return saw("inbox", entries=count, next=_cursor(page.get("next")))


def views_line(views):
    if not isinstance(views, list):
        return "views=-"
    parts = []
    for view in views:
        entries = view.get("entries") if isinstance(view, dict) else None
        parts.append(str(len(entries)) if isinstance(entries, list) else "-")
    text = ".".join(parts)
    return "views=%s" % (text if len(text) <= 24 else "n%s" % len(parts))


def _cursor(value):
    if type(value) is int and 0 <= value <= 999999:
        return value
    if isinstance(value, str) and value.isdigit() and len(value) <= 6:
        return int(value)
    return None if value is None else "set"
