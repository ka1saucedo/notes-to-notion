"""Read Apple Notes (macOS) inside a hard 30-day window and work out what's new since last run.

Privacy model:
  * Read-only. Nothing in Apple Notes is ever edited or deleted.
  * Only notes modified in the last 30 days are read. The date filter runs *inside* Notes.app,
    so older notes are never transferred out of it.
  * Local state stores SHA-256 fingerprints of notes and lines, plus redacted titles. Never raw text.
"""
import hashlib
import json
import os
import subprocess
from datetime import datetime

from .redact import redact

MAX_DAYS = 30


class NotesError(RuntimeError):
    pass


def _applescript_str(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _script(days: int, account: str, with_body: bool) -> str:
    # Batch-fetch each property per folder (one Apple Event per property). Per-note loops with string
    # concatenation are O(n^2) in AppleScript and time out on large libraries.
    body_expr = (
        "plaintext of (every note of fld whose modification date > d)"
        if with_body
        else "name of (every note of fld whose modification date > d)"
    )
    return f'''
set d to (current date) - ({int(days)} * days)
set out to ""
tell application "Notes"
  repeat with fld in folders of account "{_applescript_str(account)}"
    set fname to name of fld
    if (count of (every note of fld whose modification date > d)) > 0 then
      set ids to id of (every note of fld whose modification date > d)
      set nms to name of (every note of fld whose modification date > d)
      set mds to modification date of (every note of fld whose modification date > d)
      set cds to creation date of (every note of fld whose modification date > d)
      set txs to {body_expr}
      set isoM to {{}}
      set isoC to {{}}
      repeat with x in mds
        set end of isoM to my iso(x)
      end repeat
      repeat with x in cds
        set end of isoC to my iso(x)
      end repeat
      set AppleScript's text item delimiters to "<<<I>>>"
      set out to out & "<<<FOLDER>>>" & fname & "<<<S>>>" & (ids as text) & "<<<S>>>" & (nms as text) & "<<<S>>>" & (isoM as text) & "<<<S>>>" & (isoC as text) & "<<<S>>>" & (txs as text)
      set AppleScript's text item delimiters to ""
    end if
  end repeat
end tell
return out
on iso(d)
  set y to year of d as string
  set m to text -2 thru -1 of ("0" & ((month of d as integer) as string))
  set dd to text -2 thru -1 of ("0" & (day of d as string))
  set hh to text -2 thru -1 of ("0" & (hours of d as string))
  set mm to text -2 thru -1 of ("0" & (minutes of d as string))
  return y & "-" & m & "-" & dd & "T" & hh & ":" & mm
end iso
'''


def read_apple_notes(days: int = MAX_DAYS, account: str = "iCloud", with_body: bool = True,
                     skip_folders=("Recently Deleted",)) -> list[dict]:
    if days > MAX_DAYS:
        raise NotesError(f"Refused: {days} days exceeds the {MAX_DAYS}-day hard limit.")
    p = subprocess.run(["osascript", "-e", _script(days, account, with_body)], capture_output=True, text=True)
    if p.returncode != 0:
        raise NotesError(p.stderr.strip() or "osascript failed (is this macOS with Notes.app?)")
    return _parse(p.stdout, with_body, set(skip_folders))


def _parse(raw: str, with_body: bool, skip_folders: set) -> list[dict]:
    recs = []
    for chunk in raw.split("<<<FOLDER>>>")[1:]:
        folder, ids, names, mods, creates, texts = chunk.split("<<<S>>>", 5)
        folder = folder.strip()
        if folder in skip_folders:
            continue
        cols = [c.split("<<<I>>>") for c in (ids, names, mods, creates, texts)]
        if len({len(c) for c in cols}) != 1:
            raise NotesError(f"Field count mismatch in folder {folder!r}")
        for i, nid, title, mod, cre, body in zip(range(len(cols[0])), *cols):
            recs.append({
                "id": nid.strip(), "folder": folder, "title": title.strip(),
                "modified": mod.strip(), "created": cre.strip(),
                "body": body.rstrip("\n") if with_body else "",
            })
    return recs


def read_fixture(path: str) -> list[dict]:
    """Load notes from a JSON file instead of Apple Notes (demo, tests, other platforms)."""
    with open(os.path.expanduser(path)) as f:
        recs = json.load(f)
    out = []
    for i, r in enumerate(recs):
        out.append({
            "id": r.get("id") or f"fixture-{i}", "folder": r.get("folder", "Notes"),
            "title": r.get("title", ""), "created": r.get("created", ""),
            "modified": r.get("modified") or datetime.now().isoformat(timespec="minutes"),
            "body": r.get("body", ""),
        })
    return out


def fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class State:
    """Remembers which notes and lines have already been captured (fingerprints only)."""

    def __init__(self, directory: str):
        self.dir = os.path.expanduser(directory)
        os.makedirs(self.dir, exist_ok=True)
        self.state_path = os.path.join(self.dir, "state.json")
        self.pending_path = os.path.join(self.dir, "pending.json")

    @staticmethod
    def _load(path):
        try:
            with open(path) as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save(self, path, data):
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=0)
        os.replace(tmp, path)

    @staticmethod
    def _entry(rec):
        return {"hash": fingerprint(rec["body"]),
                "lines": sorted({fingerprint(line) for line in rec["body"].splitlines()}),
                "title": redact(rec["title"]),
                "seen": datetime.now().isoformat(timespec="minutes")}

    def baseline(self, recs) -> int:
        state = self._load(self.state_path)
        for r in recs:
            state[r["id"]] = self._entry(r)
        self._save(self.state_path, state)
        return len(recs)

    def changes(self, recs, record: bool = True) -> list[dict]:
        """New or edited notes. For edited notes, only the lines added since last capture.
        Writes pending.json (unless record=False); call commit() after the items were filed successfully."""
        state = self._load(self.state_path)
        changed, pending = [], {}
        for r in recs:
            prev = state.get(r["id"])
            if prev and prev["hash"] == fingerprint(r["body"]):
                continue
            prev_lines = set(prev["lines"]) if prev else set()
            added = [ln for ln in r["body"].splitlines() if ln.strip() and fingerprint(ln) not in prev_lines]
            changed.append({
                "id": r["id"], "folder": r["folder"], "title": redact(r["title"]),
                "created": r["created"], "modified": r["modified"], "is_new": prev is None,
                "added_lines": [redact(ln) for ln in added],
                "body": redact(r["body"]),
            })
            pending[r["id"]] = self._entry(r)
        if record:
            self._save(self.pending_path, pending)
        return changed

    def commit(self) -> int:
        state, pending = self._load(self.state_path), self._load(self.pending_path)
        state.update(pending)
        self._save(self.state_path, state)
        if os.path.exists(self.pending_path):
            os.remove(self.pending_path)
        return len(pending)

    def has_pending(self) -> bool:
        return os.path.exists(self.pending_path) and os.path.getsize(self.pending_path) > 2

    def pending_mtime(self) -> int:
        return int(os.path.getmtime(self.pending_path)) if self.has_pending() else 0
