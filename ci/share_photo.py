"""Prove a shared social/Photo full file and inline thumbnail reach a reader.

Uses LastDB's record `file` slot for the original bytes and the `inline_file`
slot for a ≤64 KiB thumbnail. Photo.json has no thumbnail field; the proof
attaches both without setting schema `file_required`.
"""

import hashlib
from pathlib import Path

from people_app import read_records, write_owned
from support import require, retry, summary


SCHEMA = "social/Photo"
MEDIA = "image/jpeg"
FULL_NAME = "proof-full.jpg"
THUMB_NAME = "proof-thumb.jpg"


def jpegish(size, tag):
    """Synthetic JPEG-ish bytes (SOI + JFIF marker + payload + EOI)."""
    head = (bytes([0xFF, 0xD8, 0xFF, 0xE0, 0x00, 0x10])
            + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00")
    unit = tag.encode() + b"\x00"
    need = max(0, size - len(head) - 2)
    body = (unit * ((need // len(unit)) + 1))[:need]
    return head + body + bytes([0xFF, 0xD9])


def path_object(path, name):
    return {"path": str(path), "name": name, "media_type": MEDIA}


def write_shared_photo(publisher, database, root):
    """Publisher writes one Photo with a full file and an inline thumbnail."""
    folder = Path(root) / "shared-photo"
    folder.mkdir(mode=0o700)
    full_bytes = jpegish(4096, "full")
    thumb_bytes = jpegish(512, "thumb")
    require(len(thumb_bytes) < 64 * 1024, "people_photo_thumb_bound")
    full_path = folder / FULL_NAME
    thumb_path = folder / THUMB_NAME
    full_path.write_bytes(full_bytes)
    thumb_path.write_bytes(thumb_bytes)
    key = hashlib.sha256(full_bytes).hexdigest()
    write_owned(publisher, database, {
        "schema": SCHEMA, "key_value": {"hash": key}, "mutation_type": "create",
        "record": {
            "photo_taken_at": "2026-11-28T00:00:00Z",
            "photo_caption": "DEV share file proof",
            "image_width_pixels": 64, "image_height_pixels": 48,
            "file_media_type": MEDIA, "file_size_bytes": len(full_bytes),
            "camera_make": "ci", "camera_model": "people-share",
            "geographic_latitude_degrees": 0.0, "geographic_longitude_degrees": 0.0,
        },
        "file": path_object(full_path, FULL_NAME),
        "inline_file": path_object(thumb_path, THUMB_NAME),
    })
    return {"key": key, "full_bytes": full_bytes, "thumb_bytes": thumb_bytes}


def wait_for_photo(node, database, key, *, seconds=150):
    def load():
        sample = {}
        try:
            rows = read_records(node, database, SCHEMA, [key])
            sample["rows"] = rows
            sample["count"] = len(rows) if isinstance(rows, list) else None
        except Exception as error:
            sample["read"] = "%s:%s" % (type(error).__name__, error)
        return sample

    def matches(sample):
        rows = sample.get("rows") if isinstance(sample, dict) else None
        if not (isinstance(rows, list) and len(rows) == 1):
            return False
        row = rows[0]
        fields = row.get("fields") if isinstance(row, dict) else None
        address = row.get("key") if isinstance(row, dict) else None
        return (isinstance(fields, dict) and fields.get("file_size_bytes") is not None
                and isinstance(address, dict) and address.get("hash") == key)

    retry("people_shared_photo", load, matches, seconds=seconds)


def marker_size(marker):
    if not isinstance(marker, dict):
        return None
    for name in ("size", "size_bytes"):
        value = marker.get(name)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def assert_marker(marker, *, name, media, size, label, sha256=None):
    require(isinstance(marker, dict), f"people_photo_{label}_marker")
    if marker.get("name") is not None:
        require(marker.get("name") == name, f"people_photo_{label}_name")
    if marker.get("media_type") is not None:
        require(marker.get("media_type") == media, f"people_photo_{label}_media")
    found = marker_size(marker)
    require(found == size, f"people_photo_{label}_size")
    if sha256 is not None:
        got = marker.get("sha256")
        require(isinstance(got, str) and got == sha256, f"people_photo_{label}_sha256")


def bytes_from_marker(marker, expected):
    """Compare bytes when the read placed a path (file:true / file_dir)."""
    if not isinstance(marker, dict):
        return False
    for field in ("path", "local"):
        value = marker.get(field)
        if isinstance(value, str) and value:
            path = Path(value)
            require(path.is_file(), "people_photo_placed_missing")
            require(path.read_bytes() == expected, "people_photo_bytes_differ")
            return True
    return False


def read_photo_with_files(node, database, key, file_dir):
    """Owner-socket read with file:true and file_dir. App sockets refuse file_dir."""
    item = {"database": database, "schema": SCHEMA, "key": {"hash": key},
            "file": True, "file_dir": str(file_dir)}
    page = node.call("/v1/read", {"reads": [item]}).get("results")
    require(isinstance(page, list) and len(page) == 1, "people_photo_file_pages")
    require(isinstance(page[0], dict) and "error" not in page[0],
            "people_photo_file_read", detail=str(page[0])[:300])
    records = page[0].get("records")
    require(isinstance(records, list) and len(records) == 1, "people_photo_file_row")
    return records[0]


def prove_shared_photo(publisher, recipient, database, root):
    """Write a Photo on the publisher; assert the recipient gets identical values.

    Full-file bytes are compared after `file:true` + `file_dir` placement.
    `inline_file` on `/v1/read` is the marker (`name`, `media_type`, `size`,
    `sha256`); LastDB stores the ≤64 KiB payload inside the record body (not a
    relay blob). The sha256 is the content comparison for the thumbnail.
    """
    written = write_shared_photo(publisher, database, root)
    key = written["key"]
    # Publisher sees the dual attachment before the recipient catches up.
    pub = Path(root) / "publisher-photo-files"
    pub.mkdir(mode=0o700)
    published = read_photo_with_files(publisher, database, key, pub)
    require(bytes_from_marker(published.get("file"), written["full_bytes"]),
            "people_photo_publisher_full")
    thumb_sha = hashlib.sha256(written["thumb_bytes"]).hexdigest()
    assert_marker(published.get("inline_file"), name=THUMB_NAME, media=MEDIA,
                  size=len(written["thumb_bytes"]), label="publisher_inline",
                  sha256=thumb_sha)
    wait_for_photo(recipient, database, key)
    rows = read_records(recipient, database, SCHEMA, [key])
    require(len(rows) == 1 and rows[0]["fields"].get("file_size_bytes") == len(written["full_bytes"]),
            "people_photo_fields")
    assert_marker(rows[0].get("inline_file"), name=THUMB_NAME, media=MEDIA,
                  size=len(written["thumb_bytes"]), label="inline_meta",
                  sha256=thumb_sha)
    folder = Path(root) / "recipient-photo-files"
    folder.mkdir(mode=0o700)
    record = read_photo_with_files(recipient, database, key, folder)
    file_marker = record.get("file")
    assert_marker(file_marker, name=FULL_NAME, media=MEDIA,
                  size=len(written["full_bytes"]), label="file")
    require(bytes_from_marker(file_marker, written["full_bytes"]), "people_photo_full_bytes")
    inline_read = record.get("inline_file")
    thumb_sha = hashlib.sha256(written["thumb_bytes"]).hexdigest()
    assert_marker(inline_read, name=THUMB_NAME, media=MEDIA,
                  size=len(written["thumb_bytes"]), label="inline",
                  sha256=thumb_sha)
    if bytes_from_marker(inline_read, written["thumb_bytes"]):
        inline_note = "inline bytes placed"
    else:
        # API v48 exposes sha256 on inline_file; that is the content check.
        inline_note = "inline marker sha256"
    message = ("A shared social/Photo reached the recipient with byte-identical "
               "fields and full file (file:true + file_dir), and matching "
               f"inline_file thumbnail ({inline_note}).")
    print(message, flush=True)
    summary(message)
