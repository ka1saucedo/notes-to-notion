import os
import tempfile
import unittest
from datetime import datetime
from unittest import mock
from zoneinfo import ZoneInfo

from notes_to_notion import config as C
from notes_to_notion import notes as N
from notes_to_notion import translator as T
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
