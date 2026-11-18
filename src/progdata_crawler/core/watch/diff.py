"""Line diffs between two versions of a page's markdown."""

import difflib
from dataclasses import dataclass

MAX_DIFF_CHARS = 100_000
"""Longer diffs are cut, with a note, so one rewritten page cannot flood a webhook."""


@dataclass(frozen=True)
class TextDiff:
    text: str
    """Unified diff; empty when nothing changed."""
    added_lines: int
    removed_lines: int

    @property
    def changed(self) -> bool:
        return bool(self.added_lines or self.removed_lines)


def diff_text(before: str, after: str, *, label: str = "page") -> TextDiff:
    lines = list(
        difflib.unified_diff(
            before.splitlines(),
            after.splitlines(),
            fromfile=f"{label} (before)",
            tofile=f"{label} (now)",
            lineterm="",
        )
    )
    added = sum(1 for line in lines if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in lines if line.startswith("-") and not line.startswith("---"))
    text = "\n".join(lines)
    if len(text) > MAX_DIFF_CHARS:
        text = text[:MAX_DIFF_CHARS] + "\n[The rest of the diff was cut.]"
    return TextDiff(text, added, removed)
