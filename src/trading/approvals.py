"""Inbound approval command handling.

The parsing/handling is built and tested here; the actual chat transport (a Discord
bot that reads messages and calls this) is the M13 blocked dependency — it needs a
bot token and a hosted listener. When that's available, the bot's on-message handler
is a two-line call to `handle_approval_command`. Until then, approval is via
`trading approve <id>` on the CLI, with the outbound ping telling you what's pending.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_APPROVE = re.compile(r"^\s*approve\s+#?(\d+)\s*$", re.IGNORECASE)
_DENY = re.compile(r"^\s*deny\s+#?(\d+)\s*$", re.IGNORECASE)


@dataclass
class ApprovalCommand:
    action: str          # 'approve' | 'deny'
    proposal_id: int


def parse_approval_command(text: str) -> ApprovalCommand | None:
    if text is None:
        return None
    m = _APPROVE.match(text)
    if m:
        return ApprovalCommand("approve", int(m.group(1)))
    m = _DENY.match(text)
    if m:
        return ApprovalCommand("deny", int(m.group(1)))
    return None


def handle_approval_command(pipeline, journal, text: str) -> str:
    """Parse and act on an inbound approval message. Returns a reply string. Safe to
    wire directly to a bot's on-message handler once a transport is available."""
    cmd = parse_approval_command(text)
    if cmd is None:
        return ""  # not an approval command — ignore
    if cmd.action == "approve":
        try:
            result = pipeline.approve(cmd.proposal_id)
            return f"approved #{cmd.proposal_id}: {result.status}"
        except ValueError as e:
            return f"cannot approve #{cmd.proposal_id}: {e}"
    # deny
    row = journal.get_proposal(cmd.proposal_id)
    if row is None or row["status"] != "pending_approval":
        return f"cannot deny #{cmd.proposal_id}: not pending approval"
    journal.record_verdict(cmd.proposal_id, source="human", verdict="veto",
                           reason="denied via approval channel")
    journal.set_proposal_status(cmd.proposal_id, "rejected")
    return f"denied #{cmd.proposal_id}"
