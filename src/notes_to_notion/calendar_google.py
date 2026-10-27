"""Optional Google Calendar output for approved events.

Install the extra (`pip install "notes-to-notion[calendar]"`) and point
`calendar.google_token_file` at an authorized-user OAuth token JSON with the calendar.events scope.
"""
import os
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .config import Config
from .models import Row


def service(cfg: Config) -> Any:
    """An authorized Calendar API client. The Google client library is untyped, hence Any."""
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
    except ImportError:
        msg = 'Calendar output needs: pip install "notes-to-notion[calendar]"'
        raise RuntimeError(msg) from None
    path = os.path.expanduser(cfg["calendar"]["google_token_file"])
    creds = Credentials.from_authorized_user_file(path)
    if not creds.valid and creds.refresh_token:
        creds.refresh(Request())
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def event_body(cfg: Config, row: Row) -> dict[str, Any] | None:
    when = row["event_time"] or row["due_date"]
    if not when:
        return None
    desc = f'{row["description"]}\n\nFrom Inbound: {row["url"]}'.strip()[:1900]
    tz = ZoneInfo(cfg["taxonomy"]["timezone"])
    if "T" in when:  # timed -> block of event_minutes
        start = datetime.fromisoformat(when.replace("Z", "+00:00"))
        if start.tzinfo is None:
            start = start.replace(tzinfo=tz)
        end = start + timedelta(minutes=int(cfg["calendar"]["event_minutes"]))
        return {"summary": row["name"], "description": desc,
                "start": {"dateTime": start.isoformat()}, "end": {"dateTime": end.isoformat()}}
    day = datetime.strptime(when[:10], "%Y-%m-%d")  # date only -> all-day
    return {"summary": row["name"], "description": desc,
            "start": {"date": day.strftime("%Y-%m-%d")},
            "end": {"date": (day + timedelta(days=1)).strftime("%Y-%m-%d")}}


def create_event(svc: Any, cfg: Config, row: Row) -> str:
    body = event_body(cfg, row)
    if not body:
        return ""
    ev = svc.events().insert(calendarId=cfg["calendar"]["calendar_id"], body=body).execute()
    return str(ev.get("id", ""))
