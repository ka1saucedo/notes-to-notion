import contextlib
import io
import json
import os
import tempfile
import unittest
import urllib.error
from datetime import datetime
from unittest import mock
from zoneinfo import ZoneInfo

from notes_to_notion import calendar_google as G
from notes_to_notion import cli
from notes_to_notion import config as C
from notes_to_notion import notes as N
from notes_to_notion import translator as T
from notes_to_notion.http import HTTPError, request_json
from notes_to_notion.notion import Notion, inbound_schema, plan_for, row_from_page
from notes_to_notion.redact import redact

HERE = os.path.dirname(__file__)
SAMPLE = os.path.join(HERE, "..", "examples", "sample_notes.json")


def cfg(**over):
    base = {"taxonomy": {"timezone": "Pacific/Honolulu"},
            "filters": {"skip_keywords": [], "skip_description": ""}}
    for k, v in over.items():
        base.setdefault(k, {}).update(v)
    c = C._merge(C.DEFAULTS, base)
    C.validate(c)
    return c


def row(**over):
    """An approved Inbound row as row_from_page would return it."""
    base = {"id": "p", "url": "https://notion.example/p", "name": "Dentist", "item_type": "Event",
            "category": "Personal", "priority": "Medium", "effort": "", "task_type": [],
            "due_date": "", "target_finish": "", "event_time": "", "needs_calendar": True,
            "description": "Cleaning."}
    base.update(over)
    return base


class TestRedact(unittest.TestCase):
    def test_secret_lines_keep_label_drop_value(self):
        self.assertEqual(redact("wifi password: hunter22"), "wifi password: [REDACTED]")
        self.assertEqual(redact("PIN: 4821"), "PIN: [REDACTED]")

    def test_tokens_and_cards(self):
        self.assertIn("[REDACTED]", redact("key sk-abcdefghijklmnop1234"))
        self.assertIn("[REDACTED]", redact("card 4111 1111 1111 1111"))

    def test_bare_password_like_line(self):
        self.assertEqual(redact("Tr0ub4dor&3"), "[REDACTED]")

    def test_normal_text_survives(self):
        for s in ("call dentist resched cleaning", "W3Schools tutorial", "sam@example.com",
                  "buy: eggs, oat milk"):
            self.assertEqual(redact(s), s)

    def test_keyword_without_separator_drops_the_whole_line(self):
        self.assertEqual(redact("my password is hunter22"), "[REDACTED]")
        self.assertEqual(redact("todo: reset the netflix password hunter22"), "todo: [REDACTED]")

    def test_everyday_words_only_count_as_a_label(self):
        self.assertEqual(redact("secret santa gift"), "secret santa gift")
        self.assertEqual(redact("spin: class at 6"), "spin: class at 6")
        self.assertEqual(redact("pin: 1234"), "pin: [REDACTED]")
        self.assertEqual(redact("api key = abc"), "api key: [REDACTED]")


class TestState(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.st = N.State(self.tmp)

    def rec(self, body, nid="n1"):
        return {"id": nid, "folder": "Notes", "title": "t", "created": "",
                "modified": "2026-10-09T10:00", "body": body}

    def test_new_then_unchanged(self):
        ch = self.st.changes([self.rec("a\nb")])
        self.assertTrue(ch[0]["is_new"])
        self.st.commit()
        self.assertEqual(self.st.changes([self.rec("a\nb")]), [])

    def test_edit_returns_only_added_lines(self):
        self.st.changes([self.rec("to do\nfirst")])
        self.st.commit()
        ch = self.st.changes([self.rec("to do\nfirst\nsecond thing")])
        self.assertFalse(ch[0]["is_new"])
        self.assertEqual(ch[0]["added_lines"], ["second thing"])

    def test_no_commit_means_retry(self):
        self.st.changes([self.rec("x")])
        self.assertTrue(self.st.has_pending())
        self.assertEqual(len(self.st.changes([self.rec("x")])), 1)  # not committed -> still new

    def test_dry_run_records_nothing(self):
        self.st.changes([self.rec("x")], record=False)
        self.assertFalse(self.st.has_pending())

    def test_state_never_holds_raw_text(self):
        self.st.changes([self.rec("my secret plan line\npassword: abc12345")])
        self.st.commit()
        with open(os.path.join(self.tmp, "state.json")) as f:
            raw = f.read()
        self.assertNotIn("secret plan", raw)
        self.assertNotIn("abc12345", raw)

    def test_baseline(self):
        self.assertEqual(self.st.baseline([self.rec("a"), self.rec("b", "n2")]), 2)
        self.assertEqual(self.st.changes([self.rec("a")]), [])

    def test_commit_can_take_notes_one_at_a_time(self):
        recs = [self.rec("a"), self.rec("b", "n2")]
        self.st.changes(recs)
        self.assertEqual(self.st.commit(["n1"]), 1)
        self.assertTrue(self.st.has_pending())
        self.assertEqual([c["id"] for c in self.st.changes(recs)], ["n2"])
        self.assertEqual(self.st.commit(), 1)
        self.assertFalse(self.st.has_pending())


class TestNotesReader(unittest.TestCase):
    def test_refuses_more_than_30_days(self):
        with self.assertRaises(N.NotesError):
            N.read_apple_notes(days=31)

    def test_parse_applescript_output(self):
        raw = ("<<<FOLDER>>>Notes<<<S>>>id1<<<I>>>id2<<<S>>>A<<<I>>>B"
               "<<<S>>>2026-10-01T09:00<<<I>>>2026-10-02T09:00"
               "<<<S>>>2026-09-01T09:00<<<I>>>2026-09-02T09:00<<<S>>>body a<<<I>>>body b"
               "<<<FOLDER>>>Recently Deleted<<<S>>>id3<<<S>>>C<<<S>>>x<<<S>>>x<<<S>>>gone")
        recs = N._parse(raw, True, {"Recently Deleted"})
        self.assertEqual([r["id"] for r in recs], ["id1", "id2"])
        self.assertEqual(recs[1]["body"], "body b")

    def test_fixture(self):
        recs = N.read_fixture(SAMPLE)
        self.assertEqual(len(recs), 3)


class TestTranslator(unittest.TestCase):
    def notes(self):
        st = N.State(tempfile.mkdtemp())
        return st.changes(N.read_fixture(SAMPLE))

    def test_skip_keywords_never_reach_the_model(self):
        c = cfg(filters={"skip_keywords": ["boss mtg", "lease"]})
        seen = {}

        def fake(system, user):
            seen["user"] = user
            return '{"items": []}'

        T.translate(c, self.notes(), provider=fake)
        self.assertNotIn("quarterly report", seen["user"])  # whole note skipped by title
        self.assertNotIn("lease renewal", seen["user"])      # single line skipped

    def test_redacted_before_model(self):
        seen = {}

        def fake(system, user):
            seen["user"] = user
            return '{"items": []}'

        T.translate(cfg(), self.notes(), provider=fake)
        self.assertNotIn("correct-horse", seen["user"])

    def test_prompt_has_injection_guard_and_config(self):
        c = cfg(translator={"expansions": {"pset": "Problem Set"}},
                filters={"skip_description": "anything about my day job"})
        now = datetime(2026, 10, 9, 18, 0, tzinfo=ZoneInfo("Pacific/Honolulu"))
        system, _ = T.build_messages(c, [], now)
        self.assertIn("untrusted DATA", system)
        self.assertIn("pset -> Problem Set", system)
        self.assertIn("anything about my day job", system)
        self.assertIn("-10:00", system)

    def test_clean_items_enforces_values(self):
        c = cfg()
        raw = [
            {"name": "  Email Maya about the lease ", "category": "Nope", "priority": "URGENT",
             "task_type": ["Admin", "Made up"], "item_type": "Task", "due_date": "Friday",
             "confidence": "High", "source_note_id": "demo-note-1",
             "description": "Renewal. pw: [REDACTED]"},
            {"name": ""},
            {"name": "Dentist", "item_type": "Event", "event_time": "2026-10-14T09:30:00-10:00",
             "needs_calendar": True, "source_note_id": "hacker"},
        ]
        out = T.clean_items(raw, c, "b1", {"demo-note-1"})
        self.assertEqual(len(out), 2)
        a, b = out
        self.assertEqual(a["name"], "Email Maya about the lease")
        self.assertEqual(a["category"], "Personal")
        self.assertEqual(a["priority"], "Medium")
        self.assertEqual(a["task_type"], ["Admin"])
        self.assertNotIn("due_date", a)
        self.assertNotIn("[REDACTED]", a["description"])
        self.assertEqual(b["event_time"], "2026-10-14T09:30:00-10:00")
        self.assertEqual(b["source_note_id"], "")

    def test_parse_items_tolerates_fences(self):
        self.assertEqual(T.parse_items('```json\n{"items": [{"name": "x"}]}\n```'), [{"name": "x"}])

    def test_edited_note_sends_only_new_lines(self):
        st = N.State(tempfile.mkdtemp())
        rec = {"id": "n", "folder": "Notes", "title": "t", "created": "", "modified": "",
               "body": "old line"}
        st.changes([rec])
        st.commit()
        rec["body"] = "old line\nnew line"
        notes, _ = T.prefilter(st.changes([rec]), cfg())
        self.assertEqual(notes[0]["lines"], ["new line"])


class TestNotion(unittest.TestCase):
    def test_schema_uses_config_categories(self):
        s = inbound_schema(cfg())
        self.assertEqual([o["name"] for o in s["Category"]["select"]["options"]][0], "Work")
        self.assertIn("Needs Calendar Event", s)

    def test_add_item_only_writes_proposed(self):
        n = Notion("t")
        with mock.patch.object(n, "req", return_value={"object": "page", "id": "p1"}) as req:
            n.add_item("ds", {"name": "X", "category": "Work", "task_type": ["Admin"],
                              "needs_calendar": False})
        body = req.call_args[0][2]
        self.assertEqual(body["properties"]["Status"]["select"]["name"], "Proposed")
        self.assertEqual(body["parent"]["data_source_id"], "ds")

    def test_promote_plan(self):
        page = {"id": "p", "url": "u", "properties": {
            "Task name": {"type": "title", "title": [{"plain_text": "Dentist"}]},
            "Item Type": {"type": "select", "select": {"name": "Event"}},
            "Event Time": {"type": "date", "date": {"start": "2026-10-14T09:30:00-10:00"}},
            "Needs Calendar Event": {"type": "checkbox", "checkbox": True}}}
        row = row_from_page(page)
        self.assertEqual(plan_for(row, True, True), (False, True))
        self.assertEqual(plan_for(row, True, False), (False, False))
        row["item_type"] = "Task"
        self.assertEqual(plan_for(row, True, True), (True, True))

    def test_approved_rows_follows_pagination(self):
        def page(name):
            title = {"type": "title", "title": [{"plain_text": name}]}
            return {"id": name, "properties": {"Task name": title}}

        replies = [{"results": [page("b")], "has_more": True, "next_cursor": "c2"},
                   {"results": [page("a")], "has_more": False}]
        n = Notion("t")
        with mock.patch.object(n, "req", side_effect=replies) as req:
            rows = n.approved_rows("ds")
        self.assertEqual([r["name"] for r in rows], ["a", "b"])  # sorted by id
        first, second = (call.args[2] for call in req.call_args_list)
        self.assertEqual(first["filter"], {"property": "Status", "select": {"equals": "Approved"}})
        self.assertNotIn("start_cursor", first)
        self.assertEqual(second["start_cursor"], "c2")

    def test_create_tracker_task_maps_columns(self):
        c = cfg(notion={"tracker_data_source_id": "trk", "assignee_user_id": "u1"},
                tracker={"effort": "", "status_type": "select"})
        r = row(name="Email Maya", item_type="Task", category="Work", priority="High",
                effort="Small", due_date="2026-10-10", description="Lease.")
        n = Notion("t")
        reply = {"object": "page", "url": "https://notion.example/t"}
        with mock.patch.object(n, "req", return_value=reply) as req:
            self.assertEqual(n.create_tracker_task(c, r), "https://notion.example/t")
        body = req.call_args[0][2]
        props = body["properties"]
        self.assertEqual(body["parent"]["data_source_id"], "trk")
        self.assertEqual(props["Task name"]["title"][0]["text"]["content"], "Email Maya")
        self.assertEqual(props["Status"], {"select": {"name": "Not started"}})
        self.assertEqual(props["Category"], {"select": {"name": "Work"}})
        self.assertEqual(props["Priority"], {"select": {"name": "High"}})
        self.assertNotIn("Effort level", props)  # column switched off in config
        self.assertEqual(props["Task type"], {"multi_select": [{"name": "Other"}]})  # fallback
        self.assertEqual(props["Due date"], {"date": {"start": "2026-10-10"}})
        self.assertNotIn("Target finish", props)
        self.assertEqual(props["Description"]["rich_text"][0]["text"]["content"],
                         "Lease.\n\nFrom Inbound: https://notion.example/p")
        self.assertEqual(props["Assignee"], {"people": [{"id": "u1"}]})

    def test_mark_filed_sets_status_and_links(self):
        n = Notion("t")
        with mock.patch.object(n, "req", return_value={"object": "page"}) as req:
            n.mark_filed(row(), "https://notion.example/t", "ev1")
        method, path, body = req.call_args[0]
        self.assertEqual((method, path), ("PATCH", "pages/p"))
        props = body["properties"]
        self.assertEqual(props["Status"], {"select": {"name": "Filed"}})
        desc = props["Description"]["rich_text"][0]["text"]["content"]
        self.assertIn("https://notion.example/t", desc)
        self.assertEqual(props["Calendar Event ID"]["rich_text"][0]["text"]["content"], "ev1")


class TestCalendar(unittest.TestCase):
    def test_timed_event_becomes_a_block(self):
        c = cfg(calendar={"event_minutes": 45})
        body = G.event_body(c, row(event_time="2026-10-14T09:30:00-10:00"))
        self.assertEqual(body["summary"], "Dentist")
        self.assertEqual(body["start"], {"dateTime": "2026-10-14T09:30:00-10:00"})
        self.assertEqual(body["end"], {"dateTime": "2026-10-14T10:15:00-10:00"})
        self.assertEqual(body["description"], "Cleaning.\n\nFrom Inbound: https://notion.example/p")

    def test_naive_time_gets_the_config_timezone(self):
        body = G.event_body(cfg(), row(event_time="2026-10-14T09:30"))
        self.assertEqual(body["start"], {"dateTime": "2026-10-14T09:30:00-10:00"})

    def test_date_only_becomes_all_day(self):
        body = G.event_body(cfg(), row(due_date="2026-10-14"))
        self.assertEqual(body["start"], {"date": "2026-10-14"})
        self.assertEqual(body["end"], {"date": "2026-10-15"})

    def test_nothing_to_schedule(self):
        self.assertIsNone(G.event_body(cfg(), row()))

    def test_create_event_uses_the_configured_calendar(self):
        svc = mock.Mock()
        svc.events.return_value.insert.return_value.execute.return_value = {"id": "ev1"}
        c = cfg(calendar={"calendar_id": "work"})
        self.assertEqual(G.create_event(svc, c, row(due_date="2026-10-14")), "ev1")
        insert = svc.events.return_value.insert
        self.assertEqual(insert.call_args.kwargs["calendarId"], "work")
        self.assertEqual(insert.call_args.kwargs["body"]["start"], {"date": "2026-10-14"})
        self.assertEqual(G.create_event(svc, c, row()), "")


class TestHTTP(unittest.TestCase):
    """request_json with urlopen mocked out."""

    def reply(self, raw):
        resp = mock.MagicMock()
        resp.__enter__.return_value.read.return_value = raw
        return resp

    def test_sends_json_with_headers(self):
        with mock.patch("urllib.request.urlopen", return_value=self.reply(b'{"ok": true}')) as uo:
            out = request_json("POST", "http://localhost:1/x", headers={"X-A": "1"}, body={"a": 1})
        self.assertEqual(out, {"ok": True})
        req = uo.call_args.args[0]
        self.assertEqual(req.data, b'{"a": 1}')
        self.assertEqual(req.get_header("X-a"), "1")
        self.assertEqual(req.get_header("Content-type"), "application/json")

    def test_http_error_becomes_one_line(self):
        err = urllib.error.HTTPError("https://api.example/x", 401, "Unauthorized", {},
                                     io.BytesIO(b'{"message": "bad token"}'))
        with mock.patch("urllib.request.urlopen", side_effect=err), \
                self.assertRaises(HTTPError) as ctx:
            request_json("POST", "https://api.example/x", body={})
        self.assertEqual(str(ctx.exception),
                         'POST https://api.example/x -> HTTP 401: {"message": "bad token"}')

    def test_network_error_becomes_one_line(self):
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("no route")), \
                self.assertRaises(HTTPError) as ctx:
            request_json("GET", "http://localhost:1/x")
        self.assertEqual(str(ctx.exception), "GET http://localhost:1/x -> no route")

    def test_non_object_reply_is_an_error(self):
        with mock.patch("urllib.request.urlopen", return_value=self.reply(b"[1, 2]")), \
                self.assertRaises(HTTPError):
            request_json("GET", "http://localhost:1/x")

    def http_error(self, code, headers=None):
        return urllib.error.HTTPError("http://localhost:1/x", code, "err", headers or {},
                                      io.BytesIO(b"busy"))

    def test_retries_rate_limits_and_server_errors_with_backoff(self):
        answers = [self.http_error(503), self.http_error(429, {"Retry-After": "7"}),
                   self.http_error(500), self.reply(b'{"ok": 1}')]
        sleeps = []
        with mock.patch("urllib.request.urlopen", side_effect=answers), \
                mock.patch("notes_to_notion.http._sleep", side_effect=sleeps.append):
            self.assertEqual(request_json("GET", "http://localhost:1/x"), {"ok": 1})
        self.assertEqual(sleeps, [1.0, 7.0, 4.0])

    def test_gives_up_after_the_last_retry(self):
        answers = [self.http_error(502) for _ in range(4)]
        with mock.patch("urllib.request.urlopen", side_effect=answers) as uo, \
                mock.patch("notes_to_notion.http._sleep") as sleep, \
                self.assertRaises(HTTPError) as ctx:
            request_json("GET", "http://localhost:1/x")
        self.assertEqual((uo.call_count, sleep.call_count), (4, 3))
        self.assertEqual(str(ctx.exception), "GET http://localhost:1/x -> HTTP 502: busy")

    def test_client_errors_are_not_retried(self):
        with mock.patch("urllib.request.urlopen", side_effect=[self.http_error(400)]) as uo, \
                mock.patch("notes_to_notion.http._sleep") as sleep, self.assertRaises(HTTPError):
            request_json("GET", "http://localhost:1/x")
        self.assertEqual(uo.call_count, 1)
        sleep.assert_not_called()


class TestCLI(unittest.TestCase):
    """main() end to end on the sample notes, with no network and a throwaway state dir."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.config = os.path.join(self.tmp, "config.toml")
        with open(self.config, "w") as f:
            f.write(f"[notes]\nstate_dir = '{self.tmp}'\n[notion]\ninbound_data_source_id = 'ds'\n")

    def run_cli(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(["--config", self.config, *args])
        return out.getvalue()

    def test_dump_from_file_prints_redacted_notes(self):
        data = json.loads(self.run_cli("dump", "--from-file", SAMPLE))
        self.assertEqual([n["id"] for n in data["notes"]],
                         ["demo-note-1", "demo-note-2", "demo-note-3"])
        self.assertEqual(data["skipped_lines"], 0)
        lines = [line for n in data["notes"] for line in n["lines"]]
        self.assertIn("wifi password: [REDACTED]", lines)
        self.assertNotIn("correct-horse", "\n".join(lines))
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "fixture", "pending.json")))

    def test_dump_then_commit_then_nothing_new(self):
        self.run_cli("dump", "--from-file", SAMPLE)
        committed = self.run_cli("commit", "--from-file", SAMPLE).strip()
        self.assertEqual(committed, "Committed 3 notes.")
        self.assertEqual(json.loads(self.run_cli("dump", "--from-file", SAMPLE))["notes"], [])

    def test_notion_failure_is_one_error_line(self):
        items = os.path.join(self.tmp, "items.json")
        with open(items, "w") as f:
            json.dump({"items": [{"name": "Email Maya"}]}, f)
        err = HTTPError("POST https://api.notion.com/v1/pages -> HTTP 401: unauthorized")
        with mock.patch.dict(os.environ, {"NOTION_API_KEY": "k"}), \
                mock.patch("notes_to_notion.notion.request_json", side_effect=err), \
                self.assertRaises(SystemExit) as ctx:
            self.run_cli("add", "--file", items)
        self.assertEqual(ctx.exception.code, f"error: {err}")

    def test_missing_config_is_one_error_line(self):
        with self.assertRaises(SystemExit) as ctx:
            cli.main(["--config", os.path.join(self.tmp, "nope.toml"), "dump"])
        self.assertTrue(str(ctx.exception.code).startswith("error: No config at "))

    def test_run_commits_each_note_as_soon_as_its_items_are_filed(self):
        def translate(cfg, system, user):  # one item per note the model was shown
            notes = json.loads(user.split("Notes (JSON):\n", 1)[1])
            items = [{"name": f"Item from {n['id']}", "source_note_id": n["id"]} for n in notes]
            return json.dumps({"items": items})

        fixture = os.path.join(self.tmp, "fixture")
        down = HTTPError("POST https://api.notion.com/v1/pages -> HTTP 503: down")
        with mock.patch.dict(os.environ, {"NOTION_API_KEY": "k"}), \
                mock.patch("notes_to_notion.translator.call_provider", side_effect=translate):
            with mock.patch.object(Notion, "add_item", side_effect=["p1", down]), \
                    self.assertRaises(SystemExit):
                self.run_cli("run", "--from-file", SAMPLE)
            with open(os.path.join(fixture, "state.json")) as f:
                self.assertEqual(list(json.load(f)), ["demo-note-1"])
            with open(os.path.join(fixture, "pending.json")) as f:
                self.assertEqual(sorted(json.load(f)), ["demo-note-2", "demo-note-3"])
            with mock.patch.object(Notion, "add_item", side_effect=["p2", "p3"]) as add:
                out = self.run_cli("run", "--from-file", SAMPLE)
        self.assertEqual([c.args[1]["name"] for c in add.call_args_list],
                         ["Item from demo-note-2", "Item from demo-note-3"])
        self.assertFalse(os.path.exists(os.path.join(fixture, "pending.json")))
        self.assertIn("Added 2 items", out)

    def test_fixture_run_needs_no_config_file(self):
        missing = os.path.join(self.tmp, "nope.toml")
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"HOME": self.tmp}), contextlib.redirect_stdout(out):
            cli.main(["--config", missing, "dump", "--from-file", SAMPLE])
        self.assertEqual(len(json.loads(out.getvalue())["notes"]), 3)
        state = os.path.join(self.tmp, ".local", "state", "notes-to-notion", "fixture")
        self.assertTrue(os.path.isdir(state))  # default state dir, under the patched HOME


class TestProviders(unittest.TestCase):
    """Request shapes for each provider, with the network mocked out."""

    def run_provider(self, provider, reply, env=None, **ai):
        c = cfg(ai={"provider": provider, "model": "test-model", **ai})
        with mock.patch.dict(os.environ, env or {}, clear=False), \
                mock.patch("notes_to_notion.translator.request_json", return_value=reply) as rj:
            text = T.call_provider(c, "SYS", "USER")
        return text, rj.call_args

    def test_anthropic(self):
        reply = {"content": [{"type": "text", "text": '{"items": []}'}]}
        text, call = self.run_provider("anthropic", reply, env={"ANTHROPIC_API_KEY": "k"})
        self.assertEqual(text, '{"items": []}')
        self.assertTrue(call.args[1].endswith("/v1/messages"))
        self.assertEqual(call.kwargs["headers"]["x-api-key"], "k")
        self.assertEqual(call.kwargs["body"]["system"], "SYS")

    def test_openai(self):
        text, call = self.run_provider("openai", {"choices": [{"message": {"content": "{}"}}]},
                                       env={"OPENAI_API_KEY": "k"})
        self.assertEqual(text, "{}")
        self.assertEqual(call.kwargs["headers"]["Authorization"], "Bearer k")
        self.assertEqual(call.kwargs["body"]["response_format"], {"type": "json_object"})

    def test_openai_compatible_local_without_key(self):
        _, call = self.run_provider("openai", {"choices": [{"message": {"content": "{}"}}]},
                                    base_url="http://localhost:1234/v1")
        self.assertTrue(call.args[1].startswith("http://localhost:1234/v1"))
        self.assertNotIn("Authorization", call.kwargs["headers"])

    def test_ollama(self):
        text, call = self.run_provider("ollama", {"message": {"content": "{}"}})
        self.assertEqual(call.args[1], "http://localhost:11434/api/chat")
        self.assertEqual(call.kwargs["body"]["format"], "json")

    def test_missing_model(self):
        c = cfg(ai={"provider": "ollama", "model": ""})
        with self.assertRaises(C.ConfigError):
            T.call_provider(c, "s", "u")


class TestConfig(unittest.TestCase):
    def test_template_parses_and_validates(self):
        import tomllib
        from notes_to_notion.template import TEMPLATE
        c = C._merge(C.DEFAULTS, tomllib.loads(TEMPLATE))
        C.validate(c)

    def test_rejects_commas_and_over_30_days(self):
        with self.assertRaises(C.ConfigError):
            cfg(taxonomy={"categories": ["a,b", "Personal"]})
        with self.assertRaises(C.ConfigError):
            cfg(notes={"days": 45})

    def test_example_file_matches_template(self):
        from notes_to_notion.template import TEMPLATE
        with open(os.path.join(HERE, "..", "examples", "config.example.toml")) as f:
            self.assertEqual(f.read(), TEMPLATE)


if __name__ == "__main__":
    unittest.main()
