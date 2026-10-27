"""The shapes that flow through the pipeline, as TypedDicts because every one of them is JSON.

Note (reader) -> Change (State.changes) -> NoteLines (translator.prefilter)
-> Item (translator.clean_items) -> Notion page -> Row (notion.row_from_page) -> tracker, calendar
"""
from typing import NotRequired, TypedDict


class Note(TypedDict):
    """One note as read from Apple Notes or a fixture file."""

    id: str
    folder: str
    title: str
    created: str
    modified: str
    body: str


class Change(Note):
    """A new or edited note after redaction, with the lines added since the last capture."""

    is_new: bool
    added_lines: list[str]


class NoteLines(TypedDict):
    """What the translator sees: a note's context and the lines to turn into items."""

    id: str
    folder: str
    title: str
    modified: str
    lines: list[str]


class Item(TypedDict):
    """A validated item ready to file as Proposed. Keys the translator left out are absent."""

    name: str
    batch_id: str
    category: str
    task_type: list[str]
    priority: str
    item_type: str
    confidence: str
    needs_calendar: bool
    source_note_id: str
    effort: NotRequired[str]
    due_date: NotRequired[str]
    target_finish: NotRequired[str]
    event_time: NotRequired[str]
    description: NotRequired[str]


class Row(TypedDict):
    """An Inbound page read back for promotion."""

    id: str
    url: str
    name: str
    item_type: str
    category: str
    priority: str
    effort: str
    task_type: list[str]
    due_date: str
    target_finish: str
    event_time: str
    needs_calendar: bool
    description: str
