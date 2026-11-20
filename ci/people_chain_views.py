"""Prove signed-chain state through the node's local API (no ExeMem mutation-log)."""

from share_node import chain_grants, owner_databases, until
from support import require


def expect_member(node, database, node_key, role="read"):
    """Wait until the folded chain lists this member at the expected role."""

    def done(row):
        members = row.get("members") or []
        return any(item.get("nodeKey") == node_key and item.get("role") == role
                   for item in members if isinstance(item, dict))

    return until("chain_member_role", lambda: chain_grants(node, database), done)


def expect_no_active(node, database, node_key):
    """Wait until the folded chain has no active role for this member."""

    def done(row):
        members = row.get("members") or []
        for item in members:
            if not isinstance(item, dict) or item.get("nodeKey") != node_key:
                continue
            if item.get("role") not in (None, "revoked"):
                return False
        return True

    return until("chain_member_revoked", lambda: chain_grants(node, database), done)


def self_access(node, database):
    """`db show` selfAccess for a received database on this node."""
    reply = owner_databases(node, "list")
    rows = reply.get("databases") if isinstance(reply, dict) else None
    require(isinstance(rows, list), "chain_list_databases")
    for row in rows:
        if isinstance(row, dict) and row.get("id") == database:
            return row.get("selfAccess")
    return None


def expect_self_role(node, database, role):
    """Wait until this node folds selfAccess.role from the signed grant chain."""

    def done(access):
        return isinstance(access, dict) and access.get("role") == role

    return until("chain_self_role", lambda: self_access(node, database), done)
