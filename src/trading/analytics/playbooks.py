"""Playbook evolution — apply weekend research edits to playbooks/*.md.

Paper mode applies edits immediately (git is the audit trail). Live mode queues
edits to memory/pending_playbook_edits.md for human approval. Never bypasses
guardrails; only mutates markdown the strategy agent reads via read_playbook.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

EDIT_FENCE = re.compile(
    r"```playbook-edits\s*\n(.*?)\n```",
    re.DOTALL | re.IGNORECASE,
)


@dataclass
class PlaybookEdit:
    tag: str
    action: str  # append_bullets | replace_section | create
    section: str | None = None
    bullets: list[str] = field(default_factory=list)
    body: str | None = None


@dataclass
class ApplyReport:
    applied: list[str] = field(default_factory=list)
    queued: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = []
        if self.applied:
            parts.append(f"applied {len(self.applied)}")
        if self.queued:
            parts.append(f"queued {len(self.queued)}")
        if self.skipped:
            parts.append(f"skipped {len(self.skipped)}")
        if self.errors:
            parts.append(f"errors {len(self.errors)}")
        return ", ".join(parts) or "no edits"


def parse_playbook_edits(text: str) -> list[PlaybookEdit]:
    """Extract edits from a ```playbook-edits JSON fence in research markdown."""
    m = EDIT_FENCE.search(text or "")
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return []
    raw = data.get("edits") if isinstance(data, dict) else data
    if not isinstance(raw, list):
        return []
    edits: list[PlaybookEdit] = []
    for item in raw:
        if not isinstance(item, dict) or not item.get("tag") or not item.get("action"):
            continue
        bullets = item.get("bullets") or []
        if isinstance(bullets, str):
            bullets = [bullets]
        edits.append(PlaybookEdit(
            tag=str(item["tag"]).strip().lower().replace(" ", "-"),
            action=str(item["action"]).strip().lower(),
            section=(str(item["section"]).strip() if item.get("section") else None),
            bullets=[str(b).strip() for b in bullets if str(b).strip()],
            body=item.get("body"),
        ))
    return edits


def _playbook_path(playbooks_dir: Path, tag: str) -> Path:
    safe = re.sub(r"[^a-z0-9\-]+", "-", tag.lower()).strip("-") or "untagged"
    return playbooks_dir / f"{safe}.md"


def _normalize_bullet(line: str) -> str:
    s = line.strip()
    if not s.startswith("-"):
        s = "- " + s
    return s


def _append_bullets(text: str, section: str, bullets: list[str]) -> str:
    """Append bullet lines under a ## Section header; create the section if missing."""
    header = f"## {section}"
    lines = text.splitlines()
    bullets = [_normalize_bullet(b) for b in bullets]
    # Find section
    start = None
    for i, ln in enumerate(lines):
        if ln.strip().lower() == header.lower():
            start = i
            break
    if start is None:
        # Append new section at end
        body = text.rstrip() + "\n\n" + header + "\n" + "\n".join(bullets) + "\n"
        return body
    # Find end of section (next ## or EOF)
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if lines[j].startswith("## "):
            end = j
            break
    existing = {ln.strip().lower() for ln in lines[start + 1:end]}
    to_add = [b for b in bullets if b.strip().lower() not in existing]
    if not to_add:
        return text
    new_lines = lines[:end] + to_add + lines[end:]
    return "\n".join(new_lines).rstrip() + "\n"


def _replace_section(text: str, section: str, bullets: list[str]) -> str:
    header = f"## {section}"
    lines = text.splitlines()
    start = None
    for i, ln in enumerate(lines):
        if ln.strip().lower() == header.lower():
            start = i
            break
    bullets = [_normalize_bullet(b) for b in bullets]
    block = [header] + bullets
    if start is None:
        return text.rstrip() + "\n\n" + "\n".join(block) + "\n"
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if lines[j].startswith("## "):
            end = j
            break
    new_lines = lines[:start] + block + lines[end:]
    return "\n".join(new_lines).rstrip() + "\n"


def apply_edit(playbooks_dir: Path, edit: PlaybookEdit) -> str:
    """Apply one edit; returns a short status string or raises ValueError."""
    playbooks_dir.mkdir(parents=True, exist_ok=True)
    path = _playbook_path(playbooks_dir, edit.tag)
    if edit.action == "create":
        if path.exists():
            raise ValueError(f"{path.name} already exists — use append_bullets/replace_section")
        body = (edit.body or "").strip()
        if not body:
            body = (f"# Playbook: {edit.tag}\n\n"
                    f"Candidate strategy proposed by weekend research.\n\n"
                    f"## Entry filters\n" +
                    "\n".join(_normalize_bullet(b) for b in edit.bullets) + "\n")
        if not body.startswith("#"):
            body = f"# Playbook: {edit.tag}\n\n{body}"
        path.write_text(body.rstrip() + "\n", encoding="utf-8")
        return f"created {path.name}"

    if not path.exists():
        # Seed a minimal playbook then apply
        path.write_text(
            f"# Playbook: {edit.tag}\n\n"
            f"## Thesis\n- (seeded by research edit)\n\n"
            f"## Entry filters\n\n## Exit rules\n\n## Known failure modes\n",
            encoding="utf-8",
        )

    text = path.read_text(encoding="utf-8")
    if edit.action == "append_bullets":
        if not edit.section or not edit.bullets:
            raise ValueError("append_bullets needs section + bullets")
        path.write_text(_append_bullets(text, edit.section, edit.bullets), encoding="utf-8")
        return f"appended {len(edit.bullets)} bullet(s) to {path.name}::{edit.section}"
    if edit.action == "replace_section":
        if not edit.section or not edit.bullets:
            raise ValueError("replace_section needs section + bullets")
        path.write_text(_replace_section(text, edit.section, edit.bullets), encoding="utf-8")
        return f"replaced {path.name}::{edit.section}"
    raise ValueError(f"unknown action '{edit.action}'")


def apply_playbook_edits(
    research_text: str,
    *,
    playbooks_dir: str | Path,
    memory_dir: str | Path,
    paper_mode: bool,
) -> ApplyReport:
    """Parse research markdown and apply (paper) or queue (live) edits."""
    report = ApplyReport()
    edits = parse_playbook_edits(research_text)
    if not edits:
        report.skipped.append("no ```playbook-edits``` block found")
        return report

    playbooks_dir = Path(playbooks_dir)
    memory_dir = Path(memory_dir)
    memory_dir.mkdir(parents=True, exist_ok=True)

    if not paper_mode:
        pending = memory_dir / "pending_playbook_edits.md"
        stamp = __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ).isoformat(timespec="minutes")
        block = EDIT_FENCE.search(research_text)
        payload = block.group(0) if block else research_text
        prev = pending.read_text(encoding="utf-8") if pending.exists() else "# Pending playbook edits\n"
        pending.write_text(
            prev.rstrip() + f"\n\n## Queued {stamp} (live — needs human approval)\n\n{payload}\n",
            encoding="utf-8",
        )
        for e in edits:
            report.queued.append(f"{e.tag}:{e.action}")
        return report

    for edit in edits:
        try:
            msg = apply_edit(playbooks_dir, edit)
            report.applied.append(msg)
        except Exception as e:
            report.errors.append(f"{edit.tag}:{edit.action}: {e}")
    return report
