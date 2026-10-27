"""Notion: create the Inbound review database, file items as Proposed, promote Approved rows."""
from typing import Self

from . import config as C
from .http import JsonObject, request_json
from .models import Item, Row

API = "https://api.notion.com/v1"
NOTION_VERSION = "2025-09-03"

# Inbound column names. The setup command creates exactly these.
P_TITLE, P_STATUS, P_CATEGORY = "Task name", "Status", "Category"
P_PRIORITY, P_EFFORT, P_TASK_TYPE = "Priority", "Effort level", "Task type"
P_ITEM_TYPE, P_CONFIDENCE = "Item Type", "Confidence"
P_DUE, P_TARGET, P_EVENT = "Due date", "Target finish", "Event Time"
P_NEEDS_CAL, P_DESC, P_SOURCE = "Needs Calendar Event", "Description", "Source"
P_BATCH, P_CAL_ID = "Batch ID", "Calendar Event ID"

STATUS_COLORS = {"Proposed": "yellow", "Approved": "green", "Filed": "gray", "Rejected": "red"}


class NotionError(RuntimeError):
    """Notion answered, but not with what we asked for."""


def _sel(names: list[str]) -> JsonObject:
    return {"select": {"options": [{"name": n} for n in names]}}


def _rt(s: str | None, limit: int = 1900) -> JsonObject:
    return {"rich_text": [{"text": {"content": (s or "")[:limit]}}]}


def inbound_schema(cfg: C.Config) -> JsonObject:
    tax = cfg["taxonomy"]
    return {
        P_TITLE: {"title": {}},
        P_STATUS: {"select": {"options": [{"name": n, "color": c}
                                          for n, c in STATUS_COLORS.items()]}},
        P_CATEGORY: _sel(tax["categories"]),
        P_PRIORITY: _sel(C.PRIORITIES),
        P_EFFORT: _sel(C.EFFORTS),
        P_TASK_TYPE: {"multi_select": {"options": [{"name": n} for n in tax["task_types"]]}},
        P_ITEM_TYPE: _sel(C.ITEM_TYPES),
        P_CONFIDENCE: _sel(C.CONFIDENCE),
        P_DUE: {"date": {}}, P_TARGET: {"date": {}}, P_EVENT: {"date": {}},
        P_NEEDS_CAL: {"checkbox": {}},
        P_DESC: {"rich_text": {}}, P_SOURCE: {"rich_text": {}},
        P_BATCH: {"rich_text": {}}, P_CAL_ID: {"rich_text": {}},
    }


class Notion:
    def __init__(self, token: str) -> None:
        self.token = token

    @classmethod
    def from_config(cls, cfg: C.Config) -> Self:
        return cls(C.secret(cfg["notion"]["api_key_env"]))

    def req(self, method: str, path: str, body: JsonObject | None = None) -> JsonObject:
        headers = {"Authorization": f"Bearer {self.token}", "Notion-Version": NOTION_VERSION}
        return request_json(method, f"{API}/{path}", body=body, headers=headers)

    # ---- setup ----
    def create_inbound(self, parent_page_id: str, cfg: C.Config,
                       title: str = "Inbound") -> tuple[str, str]:
        r = self.req("POST", "databases", {
            "parent": {"type": "page_id", "page_id": parent_page_id},
            "title": [{"type": "text", "text": {"content": title}}],
            "initial_data_source": {"properties": inbound_schema(cfg)},
        })
        sources = r.get("data_sources") or []
        if not sources:
            raise NotionError(f"Database created but no data source returned: {r}")
        return r["id"], sources[0]["id"]

    # ---- ingest ----
    def add_item(self, data_source_id: str, it: Item, source: str = "Apple Notes") -> str:
        # Ingest only ever writes Proposed.
        props: JsonObject = {P_TITLE: {"title": [{"text": {"content": it["name"][:200]}}]},
                             P_STATUS: {"select": {"name": "Proposed"}}}
        if it.get("category"):
            props[P_CATEGORY] = {"select": {"name": it["category"]}}
        selects = ((it.get("priority"), P_PRIORITY), (it.get("effort"), P_EFFORT),
                   (it.get("item_type"), P_ITEM_TYPE), (it.get("confidence"), P_CONFIDENCE))
        for value, prop in selects:
            if value:
                props[prop] = {"select": {"name": value}}
        if it.get("task_type"):
            props[P_TASK_TYPE] = {"multi_select": [{"name": t} for t in it["task_type"]]}
        dates = ((it.get("due_date"), P_DUE), (it.get("target_finish"), P_TARGET),
                 (it.get("event_time"), P_EVENT))
        for value, prop in dates:
            if value:
                props[prop] = {"date": {"start": value}}
        props[P_NEEDS_CAL] = {"checkbox": bool(it.get("needs_calendar"))}
        if it.get("description"):
            props[P_DESC] = _rt(it["description"])
        props[P_SOURCE] = _rt(source)
        if it.get("batch_id"):
            props[P_BATCH] = _rt(it["batch_id"])
        parent = {"type": "data_source_id", "data_source_id": data_source_id}
        r = self.req("POST", "pages", {"parent": parent, "properties": props})
        if r.get("object") != "page":
            raise NotionError(f"Notion create failed for {it['name']!r}: {r.get('message', r)}")
        return str(r["id"])

    # ---- promote ----
    def approved_rows(self, data_source_id: str) -> list[Row]:
        out: list[Row] = []
        cursor: str | None = None
        while True:
            body: JsonObject = {"filter": {"property": P_STATUS, "select": {"equals": "Approved"}},
                                "page_size": 100}
            if cursor:
                body["start_cursor"] = cursor
            r = self.req("POST", f"data_sources/{data_source_id}/query", body)
            for pg in r.get("results", []):
                out.append(row_from_page(pg))
            if not r.get("has_more"):
                break
            cursor = r.get("next_cursor")
        return sorted(out, key=lambda x: x["id"])

    def create_tracker_task(self, cfg: C.Config, row: Row) -> str:
        t, n = cfg["tracker"], cfg["notion"]
        props: JsonObject = {t["title"]: {"title": [{"text": {"content": row["name"][:200]}}]}}
        if t["status"]:
            props[t["status"]] = {t["status_type"]: {"name": t["status_value"]}}
        if t["category"] and row["category"]:
            props[t["category"]] = {"select": {"name": row["category"]}}
        if t["priority"] and row["priority"]:
            props[t["priority"]] = {"select": {"name": row["priority"]}}
        if t["effort"] and row["effort"]:
            props[t["effort"]] = {"select": {"name": row["effort"]}}
        if t["task_type"]:
            fallback = ["Other"] if "Other" in cfg["taxonomy"]["task_types"] else []
            types = row["task_type"] or fallback
            if types:
                props[t["task_type"]] = {"multi_select": [{"name": x} for x in types]}
        if t["due_date"] and row["due_date"]:
            props[t["due_date"]] = {"date": {"start": row["due_date"]}}
        if t["target_finish"] and row["target_finish"]:
            props[t["target_finish"]] = {"date": {"start": row["target_finish"]}}
        if t["description"]:
            desc = f'{row["description"]}\n\nFrom Inbound: {row["url"]}'.strip()
            props[t["description"]] = _rt(desc)
        if t["assignee"] and n["assignee_user_id"]:
            props[t["assignee"]] = {"people": [{"id": n["assignee_user_id"]}]}
        r = self.req("POST", "pages", {"parent": {"type": "data_source_id",
                                                  "data_source_id": n["tracker_data_source_id"]},
                                       "properties": props})
        if r.get("object") != "page":
            raise NotionError(f"Tracker create failed: {r.get('message', r)}")
        return str(r.get("url", ""))

    def mark_filed(self, row: Row, task_url: str = "", event_id: str = "") -> None:
        desc = row["description"] + (f"\n→ Tracker: {task_url}" if task_url else "")
        props: JsonObject = {P_STATUS: {"select": {"name": "Filed"}}, P_DESC: _rt(desc)}
        if event_id:
            props[P_CAL_ID] = _rt(event_id)
        r = self.req("PATCH", f"pages/{row['id']}", {"properties": props})
        if r.get("object") != "page":
            raise NotionError(f"Status update failed: {r.get('message', r)}")


def _as_str(value: object) -> str:
    return value if isinstance(value, str) else ""


def _prop_text(prop: JsonObject | None) -> str:
    """Plain text of a title, rich_text, select, status or date property ("" when empty)."""
    if not prop:
        return ""
    kind = prop["type"]
    value = prop.get(kind)
    if kind in ("title", "rich_text"):
        return "".join(x.get("plain_text", "") for x in value or [])
    if kind in ("select", "status"):
        return _as_str(value["name"]) if value else ""
    if kind == "date":
        return _as_str(value["start"]) if value else ""
    return ""


def _prop_names(prop: JsonObject | None) -> list[str]:
    """Option names of a multi_select property."""
    if not prop or prop["type"] != "multi_select":
        return []
    return [_as_str(o["name"]) for o in prop["multi_select"]]


def _prop_flag(prop: JsonObject | None) -> bool:
    return bool(prop and prop["type"] == "checkbox" and prop["checkbox"])


def row_from_page(pg: JsonObject) -> Row:
    props = pg["properties"]

    def text(key: str) -> str:
        return _prop_text(props.get(key))

    return {
        "id": pg["id"], "url": pg.get("url", ""), "name": text(P_TITLE),
        "item_type": text(P_ITEM_TYPE) or "Task", "category": text(P_CATEGORY),
        "priority": text(P_PRIORITY) or "Medium", "effort": text(P_EFFORT),
        "task_type": _prop_names(props.get(P_TASK_TYPE)), "due_date": text(P_DUE),
        "target_finish": text(P_TARGET), "event_time": text(P_EVENT),
        "needs_calendar": _prop_flag(props.get(P_NEEDS_CAL)), "description": text(P_DESC),
    }


def plan_for(row: Row, has_tracker: bool, has_calendar: bool) -> tuple[bool, bool]:
    """(make_task, make_event) for an approved row."""
    is_event = row["item_type"] == "Event"
    make_task = has_tracker and not is_event
    has_when = bool(row["event_time"] or row["due_date"])
    make_event = has_calendar and (is_event or row["needs_calendar"]) and has_when
    return make_task, make_event
