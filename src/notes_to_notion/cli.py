"""n2n: turn your notes app into the interface between messy thoughts and an organized Notion."""
import argparse
import json
import os
import platform
import re
import sys

from . import __version__
from . import config as C
from . import notes as N
from . import translator as T
from .notion import Notion, plan_for
from .template import TEMPLATE


def _read(cfg, from_file=None, with_body=True):
    if from_file:
        return N.read_fixture(from_file)
    return N.read_apple_notes(days=min(int(cfg["notes"]["days"]), N.MAX_DAYS), account=cfg["notes"]["account"],
                              with_body=with_body, skip_folders=cfg["notes"]["skip_folders"])


def _state(cfg, from_file=None):
    # Fixture runs keep their own state so demos never disturb the real one.
    d = C.state_dir(cfg)
    return N.State(os.path.join(d, "fixture") if from_file else d)


def cmd_init(a):
    path = os.path.expanduser(a.config or os.environ.get("N2N_CONFIG") or C.DEFAULT_CONFIG_PATH)
    if os.path.exists(path) and not a.force:
        sys.exit(f"{path} already exists (use --force to overwrite).")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(TEMPLATE)
    print(f"Wrote {path}. Edit it, set your env vars, then run `n2n setup-notion --parent-page <page id>`.")


def cmd_setup_notion(a):
    cfg = C.load(a.config)
    db_id, ds_id = Notion.from_config(cfg).create_inbound(a.parent_page, cfg, title=a.title)
    path = cfg["_path"]
    text = open(path).read()
    text, n = re.subn(r'^inbound_data_source_id\s*=.*$', f'inbound_data_source_id = "{ds_id}"', text, flags=re.M)
    if n:
        open(path, "w").write(text)
    print(f"Created '{a.title}' database {db_id} (data source {ds_id}).")
    print("Saved to config." if n else f"Add this to [notion] in your config: inbound_data_source_id = \"{ds_id}\"")


def cmd_baseline(a):
    cfg = C.load(a.config)
    n = _state(cfg, a.from_file).baseline(_read(cfg, a.from_file))
    print(f"Baseline: {n} notes marked as seen. Only what you write from now on gets captured.")


def cmd_run(a):
    cfg = C.load(a.config)
    state = _state(cfg, a.from_file)
    changed = state.changes(_read(cfg, a.from_file), record=not a.dry_run)
    if not changed:
        print("Nothing new in your notes.")
        return
    result = T.translate(cfg, changed)
    items = result["items"]
    if a.dry_run:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return
    ds = cfg["notion"]["inbound_data_source_id"]
    if not ds:
        sys.exit("Set notion.inbound_data_source_id (run `n2n setup-notion`).")
    notion = Notion.from_config(cfg)
    for it in items:
        notion.add_item(ds, it)
    state.commit()  # only after every item was filed
    if not items:
        print("Nothing worth filing in the new notes.")
    elif len(items) == 1:
        print(f"Added **{items[0]['name']}** from your notes to Notion. Please review.")
    else:
        print(f"Added {len(items)} items from your notes to Notion. Please review:")
        for it in items:
            print(f" • {it['name']}")


def cmd_dump(a):
    """Agent mode: print new notes (redacted, skip-filtered) for an external agent to translate."""
    cfg = C.load(a.config)
    changed = _state(cfg, a.from_file).changes(_read(cfg, a.from_file))
    notes, skipped = T.prefilter(changed, cfg)
    json.dump({"notes": notes, "skipped_lines": skipped}, sys.stdout, indent=1, ensure_ascii=False)
    print()


def cmd_add(a):
    """Agent mode: validate items JSON from an agent and file them to Inbound as Proposed."""
    cfg = C.load(a.config)
    raw = json.load(open(a.file)) if a.file else json.load(sys.stdin)
    raw = raw.get("items", raw) if isinstance(raw, dict) else raw
    from datetime import datetime
    from zoneinfo import ZoneInfo
    batch = datetime.now(ZoneInfo(cfg["taxonomy"]["timezone"])).strftime("%Y-%m-%dT%H:%M")
    ids = {str(i.get("source_note_id")) for i in raw if isinstance(i, dict)}
    items = T.clean_items(raw, cfg, batch, ids)
    notion = Notion.from_config(cfg)
    for it in items:
        print(f'{notion.add_item(cfg["notion"]["inbound_data_source_id"], it)} | {it["name"]}')


def cmd_commit(a):
    cfg = C.load(a.config)
    print(f"Committed {_state(cfg, a.from_file).commit()} notes.")


def cmd_promote(a):
    cfg = C.load(a.config)
    notion = Notion.from_config(cfg)
    has_tracker = bool(cfg["notion"]["tracker_data_source_id"])
    has_cal = cfg["calendar"]["provider"] == "google"
    rows = notion.approved_rows(cfg["notion"]["inbound_data_source_id"])
    if not rows:
        return
    svc, filed, errors = None, [], []
    for row in rows:
        make_task, make_event = plan_for(row, has_tracker, has_cal)
        if a.dry_run:
            print(f"WOULD FILE: {row['name']!r}" + (" -> tracker" if make_task else "")
                  + (" -> calendar" if make_event else "") + ("" if (make_task or make_event) else " -> mark Filed only"))
            continue
        try:
            url = notion.create_tracker_task(cfg, row) if make_task else ""
            ev = ""
            if make_event:
                from . import calendar_google as G
                svc = svc or G.service(cfg)
                ev = G.create_event(svc, cfg, row)
            notion.mark_filed(row, url, ev)
            filed.append(row["name"])
        except Exception as ex:  # keep going; report at the end
            errors.append(f"{row['name']!r}: {ex}")
    if filed:
        print(f"Filed {len(filed)} approved item(s):")
        for name in filed:
            print(f" • {name}")
    if errors:
        print(f"Couldn't file {len(errors)} (still Approved, will retry):")
        for e in errors:
            print(f" • {e}")
        sys.exit(1)


def cmd_monitor(a):
    """Cheap change detector for schedulers: output changes only when there's something to do."""
    cfg = C.load(a.config)
    for r in sorted(_read(cfg, with_body=False), key=lambda r: r["id"]):
        print(f'{r["id"]}|{r["modified"]}')
    st = _state(cfg)
    if st.has_pending():
        print(f"PENDING|{st.pending_mtime()}")


def cmd_doctor(a):
    ok = True

    def check(label, cond, hint=""):
        nonlocal ok
        ok &= bool(cond)
        print(("✓ " if cond else "✗ ") + label + ("" if cond or not hint else f"  ({hint})"))

    check("macOS (Apple Notes access)", platform.system() == "Darwin", "use --from-file on other platforms")
    try:
        cfg = C.load(a.config)
        check(f"config {cfg['_path']}", True)
    except C.ConfigError as e:
        check("config", False, str(e))
        sys.exit(1)
    ai = cfg["ai"]
    check(f"ai.model set ({ai['provider']})", bool(ai["model"]))
    key_env = ai["api_key_env"] or {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}.get(ai["provider"])
    if key_env and not (ai["provider"] == "openai" and ai["base_url"] and not ai["api_key_env"]):
        check(f"{key_env} is set", bool(os.environ.get(key_env)))
    check(f"{cfg['notion']['api_key_env']} is set", bool(os.environ.get(cfg["notion"]["api_key_env"])))
    check("notion.inbound_data_source_id set", bool(cfg["notion"]["inbound_data_source_id"]), "run n2n setup-notion")
    sys.exit(0 if ok else 1)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="n2n", description=__doc__)
    ap.add_argument("--version", action="version", version=f"notes-to-notion {__version__}")
    ap.add_argument("--config", help=f"config path (default {C.DEFAULT_CONFIG_PATH} or $N2N_CONFIG)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_, fixture=False):
        p = sub.add_parser(name, help=help_)
        p.set_defaults(fn=fn)
        if fixture:
            p.add_argument("--from-file", help="read notes from a JSON file instead of Apple Notes")
        return p

    p = add("init", cmd_init, "write a starter config")
    p.add_argument("--force", action="store_true")
    p = add("setup-notion", cmd_setup_notion, "create the Inbound review database in Notion")
    p.add_argument("--parent-page", required=True, help="id of a Notion page shared with your integration")
    p.add_argument("--title", default="Inbound")
    add("baseline", cmd_baseline, "mark existing notes as seen (run once before the first `run`)", fixture=True)
    p = add("run", cmd_run, "read new notes, translate them, file them to Inbound as Proposed", fixture=True)
    p.add_argument("--dry-run", action="store_true", help="translate and print; write nothing")
    add("dump", cmd_dump, "agent mode: print new notes for an external agent", fixture=True)
    p = add("add", cmd_add, "agent mode: file translated items to Inbound")
    p.add_argument("--file")
    add("commit", cmd_commit, "agent mode: mark dumped notes as captured", fixture=True)
    p = add("promote", cmd_promote, "file Approved rows to your tracker / calendar")
    p.add_argument("--dry-run", action="store_true")
    add("monitor", cmd_monitor, "print a change fingerprint (for schedulers)")
    add("doctor", cmd_doctor, "check your setup")

    a = ap.parse_args(argv)
    if not hasattr(a, "from_file"):
        a.from_file = None
    try:
        a.fn(a)
    except (C.ConfigError, N.NotesError) as e:
        sys.exit(f"error: {e}")


if __name__ == "__main__":
    main()
