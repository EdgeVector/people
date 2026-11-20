"""Print a command result as JSON, or as plain text on a terminal."""

import json


def emit(result, as_json, stream):
    if as_json or not stream.isatty():
        print(json.dumps(result, sort_keys=True), file=stream)
    else:
        text = render(result)
        if text:
            print(text, file=stream)


def render(value):
    if isinstance(value, list):
        return table(value)
    if not isinstance(value, dict):
        return "" if value is None else str(value)
    if "applied" in value or "token" in value:
        return change(value)
    return fields(value)


def scope(writes):
    """A link or a preset chooses read or submit. It does not name the schemas the node will allow."""
    if writes == "all":
        return "can submit changes"
    names = writes.get("names", []) if isinstance(writes, dict) else writes or []
    return "can submit changes" if names else "read only"


def grant(member):
    if not member or member.get("state") == "revoked" or member.get("role") == "revoked":
        return "no access"
    if member.get("role") == "write":
        return "can write directly"
    if member.get("role") == "submit":
        return "can submit changes"
    if member.get("role") == "read":
        return "read only"
    return scope(member.get("writeSchemas"))


def cell(key, value):
    if value is None:
        return "-"
    if key in ("writeSchemas", "links"):
        return scope(value) if key == "writeSchemas" else ", ".join(
            f"{database}: {scope(writes)}" for database, writes in value.items()) or "-"
    if isinstance(value, list):
        return ", ".join(cell(None, item) for item in value) or "-"
    if isinstance(value, dict):
        return value.get("name") or json.dumps(value, sort_keys=True)
    return str(value)


def table(rows):
    if not rows:
        return "None."
    if not all(isinstance(row, dict) for row in rows):
        return "\n".join(cell(None, row) for row in rows)
    columns = list(dict.fromkeys(key for row in rows for key in row))
    lines = [columns] + [[cell(key, row.get(key)) for key in columns] for row in rows]
    widths = [max(len(line[index]) for line in lines) for index in range(len(columns))]
    return "\n".join("  ".join(text.ljust(width) for text, width in zip(line, widths)).rstrip()
                     for line in lines)


def fields(value, indent=""):
    lines = []
    for key, item in value.items():
        if isinstance(item, list) and item and all(isinstance(row, dict) for row in item):
            lines.append(f"{indent}{key}:")
            lines.extend(f"{indent}  {line}" for line in table(item).splitlines())
        elif isinstance(item, dict) and key not in ("writeSchemas", "links"):
            lines.append(f"{indent}{key}:")
            lines.append(fields(item, indent + "  "))
        else:
            lines.append(f"{indent}{key}: {cell(key, item)}")
    return "\n".join(lines)


def change(value):
    """Describe a preview or an applied sharing change, one database at a time."""
    names = value.get("contactNames", {})
    lines = []
    if "subjectName" in value:
        lines.append(f"{value.get('appId', 'owner')}: {value['change']['operation']} for {value['subjectName']}")
    if "before" in value:
        lines.append(f"{value['name']}: {names.get(value['nodeKey'], value['nodeKey'])}: "
                     f"{grant(value['before'])} -> {grant(value['after'])}")
    for row in value.get("databases", []):
        title = row["name"] or row["databaseId"]
        if "status" in row:
            tip = f" at entry {row['entryHash']}" if "entryHash" in row else ""
            lines.append(f"{title}: {row['status']}" + tip)
        elif "error" not in row and not row["changes"]:
            lines.append(f"{title}: access stays the same")
        if "error" in row:
            lines.append(f"{title}: {row['error']}")
        for item in row.get("changes", []):
            lines.append(f"{title}: {names.get(item['nodeKey'], item['nodeKey'])}: "
                         f"{grant(item['before'])} -> {grant(item['after'])}")
        for item in row.get("conflicts", []):
            lines.append(f"{title}: {names.get(item['nodeKey'], item['nodeKey'])} was changed outside People "
                         f"to {grant(item['current'])}")
    if value.get("pending"):
        lines.append(f"unfinished: {', '.join(value['pending'])}. Run `people db retry`.")
    if "status" in value and "databases" not in value:
        tip = f" at entry {value['entryHash']}" if "entryHash" in value else ""
        lines.append(f"status: {value['status']}" + tip)
    if value.get("applied") is False or ("token" in value and "applied" not in value):
        lines.append("Nothing was changed. " + ("Approve it with `people request approve`."
                                                if "requestId" in value else "Rerun with --apply to make this change."))
        if "token" in value:
            lines.append(f"token: {value['token']}")
    elif "entryHash" in value and "databases" not in value and "status" not in value:
        lines.append(f"revoked at entry {value['entryHash']}")
    return "\n".join(lines)
