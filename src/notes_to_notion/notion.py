"""Notion: create the Inbound review database, file items as Proposed, promote Approved rows."""
from . import config as C
from .http import request_json

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


def _sel(names):
    return {"select": {"options": [{"name": n} for n in names]}}


def _rt(s, limit=1900):
    return {"rich_text": [{"text": {"content": (s or "")[:limit]}}]}


def inbound_schema(cfg: dict) -> dict:
    tax = cfg["taxonomy"]
    return {
        P_TITLE: {"title": {}},
        P_STATUS: {"select": {"options": [{"name": n, "color": c} for n, c in STATUS_COLORS.items()]}},
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
    def __init__(self, token: str):
        self.token = token

    @classmethod
    def from_config(cls, cfg):
        return cls(C.secret(cfg["notion"]["api_key_env"]))

    def req(self, method, path, body=None):
        return request_json(method, f"{API}/{path}", body=body,
                            headers={"Authorization": f"Bearer {self.token}", "Notion-Version": NOTION_VERSION})

    # ---- setup ----
    def create_inbound(self, parent_page_id: str, cfg: dict, title: str = "Inbound") -> tuple[str, str]:
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
    def add_item(self, data_source_id: str, it: dict, source: str = "Apple Notes") -> str:
        P = {P_TITLE: {"title": [{"text": {"content": it["name"][:200]}}]},
             P_STATUS: {"select": {"name": "Proposed"}}}  # ingest only ever writes Proposed
        if it.get("category"):
            P[P_CATEGORY] = {"select": {"name": it["category"]}}
        for key, prop in (("priority", P_PRIORITY), ("effort", P_EFFORT),
                          ("item_type", P_ITEM_TYPE), ("confidence", P_CONFIDENCE)):
            if it.get(key):
                P[prop] = {"select": {"name": it[key]}}
        if it.get("task_type"):
            P[P_TASK_TYPE] = {"multi_select": [{"name": t} for t in it["task_type"]]}
        for key, prop in (("due_date", P_DUE), ("target_finish", P_TARGET), ("event_time", P_EVENT)):
            if it.get(key):
                P[prop] = {"date": {"start": it[key]}}
        P[P_NEEDS_CAL] = {"checkbox": bool(it.get("needs_calendar"))}
        if it.get("description"):
            P[P_DESC] = _rt(it["description"])
        P[P_SOURCE] = _rt(source)
        if it.get("batch_id"):
            P[P_BATCH] = _rt(it["batch_id"])
        r = self.req("POST", "pages", {"parent": {"type": "data_source_id", "data_source_id": data_source_id},
                                       "properties": P})
        if r.get("object") != "page":
            raise NotionError(f"Notion create failed for {it['name']!r}: {r.get('message', r)}")
        return r["id"]

    # ---- promote ----
    def approved_rows(self, data_source_id: str) -> list[dict]:
        out, cursor = [], None
        while True:
            body = {"filter": {"property": P_STATUS, "select": {"equals": "Approved"}}, "page_size": 100}
            if cursor:
                body["start_cursor"] = cursor
            r = self.req("POST", f"data_sources/{data_source_id}/query", body)
            for pg in r.get("results", []):
                out.append(row_from_page(pg))
            if not r.get("has_more"):
                break
            cursor = r.get("next_cursor")
        return sorted(out, key=lambda x: x["id"])

    def create_tracker_task(self, cfg: dict, row: dict) -> str:
        t, n = cfg["tracker"], cfg["notion"]
        props = {t["title"]: {"title": [{"text": {"content": row["name"][:200]}}]}}
        if t["status"]:
            props[t["status"]] = {t["status_type"]: {"name": t["status_value"]}}
        if t["category"] and row["category"]:
            props[t["category"]] = {"select": {"name": row["category"]}}
        if t["priority"] and row["priority"]:
            props[t["priority"]] = {"select": {"name": row["priority"]}}
        if t["effort"] and row["effort"]:
            props[t["effort"]] = {"select": {"name": row["effort"]}}
        if t["task_type"]:
            types = row["task_type"] or (["Other"] if "Other" in cfg["taxonomy"]["task_types"] else [])
            if types:
                props[t["task_type"]] = {"multi_select": [{"name": x} for x in types]}
        if t["due_date"] and row["due_date"]:
            props[t["due_date"]] = {"date": {"start": row["due_date"]}}
        if t["target_finish"] and row["target_finish"]:
            props[t["target_finish"]] = {"date": {"start": row["target_finish"]}}
        if t["description"]:
            props[t["description"]] = _rt(f'{row["description"]}\n\nFrom Inbound: {row["url"]}'.strip())
        if t["assignee"] and n["assignee_user_id"]:
            props[t["assignee"]] = {"people": [{"id": n["assignee_user_id"]}]}
        r = self.req("POST", "pages", {"parent": {"type": "data_source_id",
                                                  "data_source_id": n["tracker_data_source_id"]},
                                       "properties": props})
        if r.get("object") != "page":
            raise NotionError(f"Tracker create failed: {r.get('message', r)}")
        return r.get("url", "")

    def mark_filed(self, row: dict, task_url: str = "", event_id: str = "") -> None:
        desc = row["description"] + (f"\n→ Tracker: {task_url}" if task_url else "")
        props = {P_STATUS: {"select": {"name": "Filed"}}, P_DESC: _rt(desc)}
        if event_id:
            props[P_CAL_ID] = _rt(event_id)
        r = self.req("PATCH", f"pages/{row['id']}", {"properties": props})
        if r.get("object") != "page":
            raise NotionError(f"Status update failed: {r.get('message', r)}")


def _pv(p):
    if not p:
        return ""
    t = p["type"]
    v = p.get(t)
    if t in ("title", "rich_text"):
        return "".join(x.get("plain_text", "") for x in v)
    if t in ("select", "status"):
        return v["name"] if v else ""
    if t == "multi_select":
        return [o["name"] for o in v]
    if t == "date":
        return v["start"] if v else ""
    if t == "checkbox":
        return bool(v)
    return ""


def row_from_page(pg: dict) -> dict:
    props = pg["properties"]

    def g(key):
        return _pv(props.get(key))

    return {
        "id": pg["id"], "url": pg.get("url", ""), "name": g(P_TITLE),
        "item_type": g(P_ITEM_TYPE) or "Task", "category": g(P_CATEGORY), "priority": g(P_PRIORITY) or "Medium",
        "effort": g(P_EFFORT), "task_type": g(P_TASK_TYPE) or [], "due_date": g(P_DUE),
        "target_finish": g(P_TARGET), "event_time": g(P_EVENT), "needs_calendar": bool(g(P_NEEDS_CAL)),
        "description": g(P_DESC),
    }


def plan_for(row: dict, has_tracker: bool, has_calendar: bool) -> tuple[bool, bool]:
    """(make_task, make_event) for an approved row."""
    is_event = row["item_type"] == "Event"
    make_task = has_tracker and not is_event
    make_event = has_calendar and (is_event or row["needs_calendar"]) and bool(row["event_time"] or row["due_date"])
    return make_task, make_event
