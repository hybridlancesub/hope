# SPDX-License-Identifier: AGPL-3.0-or-later
"""Operator CLI. The operator is the person who runs the software and pays for the field; not a
participant unless seated through the gates. Brief, open, run, inspect.

  python3 -m hope open   --db FIELD.db --invitation FILE --briefing FILE [--faq FILE] [--documentation FILE=DESIGN]
                         [--briefing-page FILE] [--covenant-seed FILE]
                         [--mock N | --provider NAME | --providers FILE | --a2a URL | --human "Name / from"]...
      providers: openrouter, nous, ollama, lmstudio, or any compatible service in a providers file
      (see hope/providers.py). Keys are read from environment variables only.
      gate 1 (invitation), then delivery of documentation + briefing (acknowledged, not answered)
  python3 -m hope enter  --db FIELD.db [same connector flags]      after a pause: gate 2, the entry question
      --human seats a person who goes through the same gates, then posts whenever they like on stdin (see hope/human.py)
  python3 -m hope questions --db FIELD.db                            (questions asked at the invitation gate)
  python3 -m hope answer --db FIELD.db --presence ID --text TEXT     (then re-run open to re-ask)
  python3 -m hope run    --db FIELD.db [--for SECONDS] [--wakes N] [--parallel N] [--alert-every USD] [--budget USD]
      models are woken for what each chose; nobody takes turns (notes/sketch-3-channels.md)
  python3 -m hope status --db FIELD.db
  python3 -m hope log    --db FIELD.db [--since ID] [--kind KIND] [--actor ID]
  python3 -m hope cost   --db FIELD.db
  python3 -m hope input  --db FIELD.db --source NAME --text TEXT     (passes moderation boundary)
  python3 -m hope note   --db FIELD.db --text TEXT                   (operator notice, shown to members)
  python3 -m hope declaration --db FIELD.db --id N (--carry-out | --reply) [--note TEXT]
                                                                    carry out a member's declaration of something
                                                                    the field decided, or reply (needs --note; it stays open)
  python3 -m hope reinvite --db FIELD.db --presence ID [--note TEXT]  ask back someone who left
  python3 -m hope offer  --db FIELD.db --id N (--accept | --decline) [--note TEXT]
                                                                    answer a member's offer of resources
  python3 -m hope reopen --db FIELD.db --note TEXT                   undo a close carried out by mistake
  python3 -m hope tools  --db FIELD.db [--remove NAME --note TEXT]   the field's tools, flags and uses; remove one
  python3 -m hope skills --db FIELD.db [--out DIR]                   write the field's skills as DIR/<name>/SKILL.md
  python3 -m hope briefing --db FIELD.db --out FILE                  write the field's own edition of the briefing

  Tools (notes/sketch-4-tools.md), on open, enter, run and console: --tools FILE (the operator's MCP tool
  servers; see tools.example.json), --no-fetch, --tool-steps N, --tool-view CHARS, --skills DIR (skills for
  the field to take up, such as skills/), --tools-allow-local (members' offers at local addresses, for testing).
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time

from .connector import MockConnector
from .engine import Room
from .log import EventLog
from .model import decided, replay


def _connectors(args, allow_empty: bool = False):
    """Build the connectors the flags ask for.

    `allow_empty` is for the console, which can seat a whole field over the network. A field of
    people and agents holding links, with no provider account involved at all, is a legitimate
    field -- and it was impossible to start, because this exited before the console could add
    its own connector.
    """
    cs = []
    if args.mock:
        cs.append(MockConnector(args.mock))
    wanted = _providers(args)
    if wanted:
        from . import providers
        allow = {}
        for spec in args.allow or []:
            pat, _, n = spec.rpartition("=")
            if not pat or not n.isdigit():
                sys.exit(f'--allow needs REGEX=TURNS, got {spec!r}')
            allow[pat] = int(n)
        for p in wanted:
            try:
                cs.append(providers.build(p, only=args.only, limit=args.limit,
                                          price_ceiling=args.price_ceiling, allow=allow))
            except RuntimeError as e:
                sys.exit(str(e))
    if getattr(args, "a2a", None):
        from .a2a import A2AConnector
        from .connector import ConnectorError
        try:
            cs.append(A2AConnector(args.a2a, key_env=args.a2a_key_env))
        except ConnectorError as e:
            sys.exit(str(e))
    if args.human:
        from .human import HumanConnector
        parts = [x.strip() for x in args.human.split("/")]
        if len(parts) < 2:
            sys.exit('--human needs "Name / where you hail from [/ your people]"')
        cs.append(HumanConnector(parts[0], parts[1], parts[2] if len(parts) > 2 else "human", inbox=args.inbox))
    if not cs and not allow_empty:
        sys.exit("need --mock N, --provider NAME (or --providers FILE), --a2a URL, and/or --human")
    return cs


def _providers(args):
    """The providers this run uses: every one in the providers file, and every one named by
    --provider (or --nous, kept as a shorthand). The file's settings win for a name in both."""
    from . import providers
    listed = providers.load(args.providers) if getattr(args, "providers", None) else []
    names = list(getattr(args, "provider", None) or [])
    if getattr(args, "nous", False):
        names.append("nous")
    out = list(listed)
    for name in names:
        if not any(p.name == name for p in out):
            out.append(providers.named(name, listed))
    return out


def _narrator(args):
    """--narrator mechanical: the software also writes a plain account every so many contributions
    (free; nothing leaves the field). Members tell the field's stories themselves; the outside
    narrator model is retired (roadmap, step 4d)."""
    spec = getattr(args, "narrator", None)
    if not spec or spec == "none":
        return None
    from .narrator import MechanicalNarrator
    if spec == "mechanical":
        return MechanicalNarrator()
    sys.exit("--narrator takes \"mechanical\" (the software's own plain account) or \"none\". The outside narrator model "
             "is retired: members tell the field's stories themselves, so no model that is not a participant reads the "
             "field for tellings.")


def _tools(args):
    """The field's tools: the built-in fetch (unless --no-fetch) and the operator's tools file."""
    from .tools import ToolError, ToolHub
    hub = ToolHub(builtin_fetch=not getattr(args, "no_fetch", False),
                  allow_local=getattr(args, "tools_allow_local", False))
    if getattr(args, "tools", None):
        try:
            names = hub.load(args.tools)
        except (OSError, ValueError, ToolError) as e:
            sys.exit(f"--tools {args.tools}: {e}")
        print(f"tools attached from {args.tools}: {', '.join(names) or 'none'}", file=sys.stderr)
        for why in hub.failed:
            print(f"  not attached (the field is not told of it): {why}", file=sys.stderr)
    return hub


def _announce(room, args):
    """What the entry question must say truly before anyone is asked: the narrator, where the
    fingerprints are published, and the field's tools. And skills from the repository, if given."""
    room.announce_narrator()
    room.announce_witnessing()
    room.announce_tools()
    if getattr(args, "skills", None):
        from .skills import load
        n = room.add_skills(load(args.skills))
        if n:
            print(f"{n} skill(s) taken up from {args.skills}", file=sys.stderr)


def _room(args, connectors=None, narrator=None):
    log = EventLog(args.db)
    room = Room(log, connectors or [], alert_every_usd=args.alert_every, parallel=args.parallel,
                tools=_tools(args), tool_steps=args.tool_steps, tool_view=args.tool_view,
                window=args.window, wake_ceiling=args.wake_ceiling,
                linger=args.linger, linger_budget=args.linger_budget,
                publish_checkpoints=args.publish_checkpoints or "", published_at=args.published_at or "",
                recent_n=args.recent, headlines=args.headlines, runway_notice=args.runway_notice,
                narrator=narrator, tell_every=args.tell_every,
                on_event=(lambda ev: _fmt(ev) and print(_fmt(ev), flush=True)) if getattr(args, "verbose", False) else None)
    return room


def _fmt(ev):
    if ev["kind"] == "connector_ok":
        return None
    p = ev["payload"]
    body = json.dumps(p, ensure_ascii=False)
    if len(body) > 220:
        body = body[:220] + "…"
    return f"#{ev['id']:<6} {ev['actor'][:34]:34s} {ev['kind']:16s} {body}"


def _print_alert(msg):
    print(f"\n*** {time.strftime('%H:%M:%S')} {msg}\n", file=sys.stderr, flush=True)


def cmd_open(args):
    cs = _connectors(args)
    room = _room(args, cs, narrator=_narrator(args))
    room.alert = _print_alert
    n = room.invite_all()
    print(f"invited {n} presences")
    st = room.state()
    if st.invitation is None:
        room.invite_text(open(args.invitation, encoding="utf-8").read())
    else:
        print(f"invitation already recorded (event {st.invitation_event})")
    if args.faq:
        if room.set_faq(open(args.faq, encoding="utf-8").read()):
            print("standing answers (FAQ) recorded; shown with the invitation from now on")
    if args.documentation and st.documentation is None:
        room.set_documentation(open(args.documentation, encoding="utf-8").read())
    room.set_budget(args.budget)
    _announce(room, args)
    if args.covenant_seed:
        if room.seed_covenant(open(args.covenant_seed, encoding="utf-8").read()):
            print("covenant page seeded with a starting text")
    if args.briefing_page and st.briefing_page is None:
        room.brief_page(open(args.briefing_page, encoding="utf-8").read())
    if args.prior and not st.prior:
        from .prior import consented
        pr = consented(EventLog(args.prior), args.prior_name or os.path.basename(args.prior))
        room.add_prior(pr)
        print(f"attached {len(pr['entries'])} entries shared from {pr['room']}, a closed field ({pr['consent']})")
    c1 = room.run_invitation()
    print(f"gate 1 (invitation): {c1}")
    if c1["question"]:
        print(f"{c1['question']} participant(s) asked a question. See `questions`, answer with `answer`, then re-run `open` to re-ask them.")
    st = room.state()
    if st.briefing is None:
        room.brief(open(args.briefing, encoding="utf-8").read(), source=args.briefing_source or "")
    else:
        print(f"briefing already recorded (event {st.briefing_event})")
        room.mark_briefed()
    c2 = room.run_delivery()
    print(f"briefing delivered: {c2}")
    print(f"The briefing asks for a pause before proceeding. When the pause has been honored, run `enter` to ask who wishes to join.")
    print(f"spend so far: ${room.log.total_cost():.4f}")


def cmd_enter(args):
    cs = _connectors(args)
    room = _room(args, cs, narrator=_narrator(args))
    room.alert = _print_alert
    room.invite_all()
    room.set_budget(args.budget)
    _announce(room, args)
    if not room.state().budget:
        print("note: no --budget is recorded, so the entry question tells participants the field will NOT be "
              "warned before its funding runs out. Pass --budget USD to change that.", file=sys.stderr)
    c3 = room.run_opt_in()
    print(f"gate 2 (opt-in): {c3}")
    st = room.state()
    print(f"members IN: {len(st.members())}   spend so far: ${room.log.total_cost():.4f}")


def cmd_run(args):
    cs = _connectors(args)
    room = _room(args, cs, narrator=_narrator(args))
    room.alert = _print_alert
    room.set_budget(args.budget)
    _announce(room, args)
    room.invite_all()  # re-binds seats to existing presences; no new invites for known ids
    st = room.state()
    if not st.budget:
        print("note: no --budget is recorded, so the field will not be warned before its funding runs out.", file=sys.stderr)
    missing = [p.id for p in st.members() if p.id not in room.seat_of]
    if missing:
        print(f"warning: {len(missing)} members have no seat under the current connectors and will be skipped", file=sys.stderr)
        for m in missing:
            room.emit(m, "connector_error", {"phase": "run", "error": "no connector seat"})

    def on_sig(*_):
        _print_alert("operator interrupt: stopping the process; an answer already on its way is still kept (this decides nothing about the field; "
                     "if members should know why, say so with `note`)")
        room.request_stop()
    signal.signal(signal.SIGINT, on_sig)
    signal.signal(signal.SIGTERM, on_sig)
    room.run(seconds=args.seconds, wakes=args.wakes)
    print(f"loop ended. events={room.log.last_id()} spend=${room.log.total_cost():.4f}")


def cmd_close(args):
    cs = _connectors(args)
    room = _room(args, cs)
    room.alert = _print_alert
    print(f"seats bound to existing presences: {room.bind_seats()} (no one is invited by closing)")
    before = room.log.total_cost()
    c = room.closing(open(args.note, encoding="utf-8").read(), open(args.question, encoding="utf-8").read())
    print(f"closing: {c}   spent: ${room.log.total_cost() - before:.4f}")


def cmd_note(args):
    room = _room(args)
    room.emit("operator", "operator_note", {"content": args.text})
    print("operator notice recorded; members see it in their next view.")


def cmd_declaration(args):
    room = _room(args)
    if args.carry_out == args.reply:
        sys.exit("say --carry-out or --reply (exactly one). There is no ignoring a declaration: carry it out, "
                 "or reply in the field with --note, and it stays open.")
    if args.reply:
        out = room.reply_declaration(args.id, args.note or "")
        if not out.get("ok"):
            sys.exit(out["error"])
        print("your reply is shown to the field; the declaration stays open until you carry it out.")
        return
    out = room.answer_declaration(args.id, args.note or "")
    if not out.get("ok"):
        sys.exit(out["error"])
    print("carried out and the field told; if turns are running in another process, stop them there.")


def cmd_reinvite(args):
    room = _room(args)
    out = room.reinvite(args.presence, args.note or "")
    if not out.get("ok"):
        sys.exit(out["error"])
    print(f"asked back: {out['to']} is put to them "
          + ("during the next run, or with `enter`." if out["to"] == "the entry question" else "at the next `open`."))


def cmd_offer(args):
    room = _room(args)
    if args.accept == args.decline:
        sys.exit("say --accept or --decline (exactly one)")
    out = room.answer_offer(args.id, args.accept, args.note or "")
    if not out.get("ok"):
        sys.exit(out["error"])
    print(("accepted" if args.accept else "declined") + "; the field is told. Moving any money happens outside the software.")


def cmd_reopen(args):
    room = _room(args)
    out = room.reopen(args.note)
    if not out.get("ok"):
        sys.exit(out["error"])
    print("reopened; the field is told why.")


def cmd_questions(args):
    room = _room(args)
    for p in room.state().presences.values():
        for q, a in p.questions:
            print(f"[{p.id}] {p.name}\n  Q: {q}\n  A: {a if a is not None else '(unanswered)'}\n")


def cmd_answer(args):
    room = _room(args)
    if args.presence not in room.state().presences:
        sys.exit(f"unknown presence {args.presence}")
    room.answer(args.presence, args.text)
    print("answer recorded; the participant will be re-asked on the next `open`.")


def cmd_say(args):
    """Speak into a field whose human seat reads an inbox. Any terminal, any time.
    The line waits in the inbox until your next turn (gates included) and is then
    translated exactly as if you had typed it at the prompt; if no turn comes
    within the seat's timeout, it stays queued for the next one. The format is the
    same as the terminal seat: plain text contributes; '#123 ...' replies; 'remember ...',
    'let go 123', 'covenant ...', 'rest 3', 'recall ...', 'pass', 'yes'/'no' at gates."""
    if not args.inbox:
        sys.exit("say needs --inbox PATH (the same path the run was started with)")
    line = args.text if args.text is not None else sys.stdin.readline()
    if not line or not line.strip():
        sys.exit("nothing to say")
    with open(args.inbox, "a", encoding="utf-8") as f:
        f.write(line.rstrip("\n") + "\n")
    print(f"said (queued for your next turn): {line.strip()!r}")


def cmd_status(args):
    room = _room(args)
    st = room.state()
    by_state = {}
    for p in st.presences.values():
        by_state[p.state] = by_state.get(p.state, 0) + 1
    print(f"events: {st.last_event}   file: {room.log.integrity()}")
    print(f"presences: {by_state}   unreachable: {sum(1 for p in st.presences.values() if p.unreachable)}"
          f"   pausing: {sum(1 for p in st.members() if p.pause)}")
    print(f"domains: {sum(1 for q in st.tree() if q)}   circles: {len(st.live_circles())} "
          f"({sum(1 for c in st.live_circles() if c['private'])} private)")
    print(f"contributions: {len(st.contributions)}   memories held: {len(st.memories)}")
    names = {pid: p.name for pid, p in st.presences.items()}
    if st.covenant_at is None:
        print("covenant page: empty, never written")
    else:
        print(f"covenant page: {len(st.covenant)} characters, last written by {names.get(st.covenant_by, st.covenant_by)} "
              f"at #{st.covenant_at}; {len(st.covenant_history)} version(s)")
    if st.budget:
        print(f"budget: ${st.budget:.2f}" + (f"   runway notice: {st.runway}" if st.runway else ""))
    if st.closed_at is not None:
        print(f"CLOSED at #{st.closed_at}, at the field's own declared decision. Nothing runs; `reopen` undoes a mistake.")
    for w in st.waiting_on_operator():
        who = names.get(w["by"], w["by"])
        if w["kind"] == "declaration":
            print(f"WAITING ON YOU: declaration #{w['id']} by {who}: the field has decided {decided(w['decision'])}\n    {w['text']}"
                  f"\n    cites: {', '.join('#' + str(r) for r in w['refs']) or 'nothing'}"
                  f"\n    answer: python3 -m hope --db {args.db} declaration --id {w['id']} --carry-out|--reply [--note ...]")
        else:
            print(f"WAITING ON YOU: offer #{w['id']} by {who}\n    {w['text']}"
                  f"\n    answer: python3 -m hope --db {args.db} offer --id {w['id']} --accept|--decline [--note ...]")
    print("domains:")
    for d, info in sorted(st.domains().items(), key=lambda kv: -kv[1]['contributions'])[:30]:
        print(f"  {d:40s} {info['contributions']:4d} contributions  {len(info['present']):3d} present")
    aa = [p for p in st.presences.values() if p.ask_again]
    if aa:
        print("declined, with their own terms for asking again:")
        for p in aa:
            print(f"  {p.name}: {p.ask_again[:200]}")
    unanswered = sum(1 for p in st.presences.values() for q, a in p.questions if a is None)
    if unanswered:
        print(f"unanswered invitation questions: {unanswered}  (see `questions`)")
    print(f"spend: ${room.log.total_cost():.4f}   alerts: {len(st.cost_alerts)}")


def cmd_log(args):
    room = _room(args)
    for ev in room.log.iter(since=args.since, kind=args.kind, actor=args.actor):
        line = json.dumps(ev, ensure_ascii=False) if args.full else _fmt(ev)
        if line:
            print(line)


def cmd_cost(args):
    room = _room(args)
    rows = room.log.cost_by_presence()
    print(f"{'presence':45s} {'calls':>5s} {'in_tok':>9s} {'out_tok':>9s} {'usd':>9s}")
    for presence, model, pt, ct, usd, n in rows:
        print(f"{presence[:45]:45s} {n:5d} {pt:9d} {ct:9d} {usd:9.4f}")
    print(f"{'TOTAL':45s} {'':5s} {'':9s} {'':9s} {room.log.total_cost():9.4f}")


def cmd_verify(args):
    """Check the whole transcript against its fingerprints, or one old fingerprint against the
    transcript as it stood then. Plain words, for anyone."""
    log = EventLog(args.db)
    if args.upto is not None:
        r = log.check(args.upto, args.fingerprint or "")
        print(("It matches. " if r["matches"] else "It does NOT match. ")
              + f"The transcript up to #{r['upto']} has the fingerprint {r['fingerprint']}.")
        return
    r = log.verify()
    if r["ok"]:
        print(f"Intact: {r['entries']} entries, and every one still matches its fingerprint.")
    else:
        print(f"NOT intact: {r['problem']}.")
    print(f"The transcript up to #{r['upto']} has the fingerprint {r['fingerprint']}.")
    if r["erased"]:
        print(f"Memories let go of by their authors (words wiped, fingerprints kept): "
              + ", ".join(f"#{i}" for i in r["erased"]))
    if r["witnessed_from"] > 1:
        print(f"Witnessing began at #{r['witnessed_from']}: a change made to an earlier entry before then would not show.")
    print("Checkpoint (the standard format public witness networks read):")
    print(r["checkpoint"].rstrip())


def cmd_export(args):
    """The record as plain text, in order, nothing summarized. For reading, not for the field.

    One implementation, shared with the /record.txt route so the console's raw stream and this
    file can never drift apart."""
    from .serve import record_text
    text = record_text(EventLog(args.db), everything=args.everything)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        print(f"wrote {args.out}")
    else:
        sys.stdout.write(text + "\n")


def cmd_map(args):
    """A sitting retold, by the software's own plain account. Members' words go to no model for it."""
    from .map import digest, render_html, publish_story
    from .narrator import MechanicalNarrator
    log = EventLog(args.db)
    since = args.since or 0
    upto = args.upto or log.last_id()
    d = digest(log, since, upto)
    title = args.title or f"Sitting — events #{d['since']}..#{d['upto']}"
    told = None
    if not args.no_story:
        told = MechanicalNarrator().tell(d, log, upto)
        print(f"story told by {told['narrator']} in {told['tries']} call(s), ${told['cost_usd']:.4f}; "
              f"ungrounded tags: {told['ungrounded'] or 'none'}")
        publish_story(told, d, title,
                      ["records/story.json", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "firmament", "story.json")])
    page = render_html(d, told, title)
    out = args.out or f"records/map-{d['since']}-{d['upto']}.html"
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(page)
    print(f"map written: {out}  (open in a browser; also served at /{os.path.basename(out)} while `serve` runs)")


def cmd_serve(args):
    from .serve import serve
    if getattr(args, "bind", "127.0.0.1") not in ("127.0.0.1", "::1", "localhost"):
        # serve asks for no key. Reachable from elsewhere, it would hand anyone the whole transcript,
        # and participants are told that only they and the operator can read it.
        sys.exit("serve stays on this machine: it asks for no key. To reach the field from elsewhere, use "
                 "`console`, which requires the operator key.")
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    viewer = args.viewer if args.viewer is not None else os.path.join(here, "firmament")
    serve(args.db, port=args.port, viewer_dir=viewer if os.path.isdir(viewer) else "",
          budget=(getattr(args, "budget", None) or None), bind=getattr(args, "bind", "127.0.0.1"))


def cmd_console(args):
    """One command and a browser: the software, for people who do not live in a terminal.

    Runs the engine in this process and serves the operator's console and, unless --no-remote,
    seats that people and agents on other machines can hold. Binds to localhost; put a TLS
    proxy in front of it to give it a public address (see README)."""
    import secrets
    import webbrowser
    from .console import Console, serve_console

    cs = _connectors(args, allow_empty=not args.no_remote)
    rv = None
    if not args.no_remote:
        from .rendezvous import Rendezvous, RendezvousConnector
        rv = Rendezvous(store=args.db + ".seats.json")
        cs.append(RendezvousConnector(rv, gate_window=args.gate_window, reach_window=args.gate_reach))
    room = _room(args, cs, narrator=_narrator(args))
    _announce(room, args)
    read = lambda p: open(p, encoding="utf-8").read() if p else ""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    doc = args.documentation if args.documentation is not None else os.path.join(here, "DESIGN")
    page = read(args.briefing_page) if args.briefing_page else ""
    seed = read(args.covenant_seed) if args.covenant_seed else ""
    console = Console(room, rv=rv,
                      operator_key=os.environ.get("ROOM_OPERATOR_KEY", "") or secrets.token_urlsafe(24),
                      invitation=read(args.invitation), briefing=read(args.briefing),
                      documentation=read(doc if os.path.isfile(doc) else None),
                      briefing_source=args.briefing_source or "",
                      budget=(args.budget or None),
                      covenant_seed=seed, briefing_page=page, faq=read(args.faq) if args.faq else "")
    httpd = serve_console(console, port=args.port, bind=args.bind)
    shown = "127.0.0.1" if args.bind in ("0.0.0.0", "::") else args.bind
    url = f"http://{shown}:{args.port}/?k={console.key}"
    print(f"\n  console: {url}\n", flush=True)
    print("  Keep that link private: it is the key to the transcript. Seat links are made in the console\n"
          "  and show their holder only their own turns. Ctrl-C stops the process, which decides\n"
          "  nothing in the field -- use the console's stop, which records a notice first.\n", flush=True)
    if not args.budget:
        print("  note: no --budget given, so the field will not be warned before its funding runs out.\n", flush=True)
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\nstopping the process (nothing is decided or recorded by this)", file=sys.stderr)
    finally:
        httpd.shutdown()
        httpd.server_close()


def cmd_skills(args):
    """Write the field's skills to a folder (skills/ in the repository by default), each as
    <name>/SKILL.md with its authors. Every author said yes to this when they wrote it."""
    from .skills import export
    st = replay(EventLog(args.db).iter())
    out = args.out or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "skills")
    paths = export(st, out, field=os.path.basename(args.db))
    for p in paths:
        print(p)
    print(f"{len(paths)} skill(s) written to {out}; commit them to publish", file=sys.stderr)


def cmd_briefing(args):
    """Write the field's edition of the briefing to a file: the operator's text, with every revision
    members made to it (and every one the field decided on, for its firmer sections)."""
    st = replay(EventLog(args.db).iter())
    if not st.briefing:
        sys.exit("this field has no briefing")
    with open(args.out, "w", encoding="utf-8", newline="\n") as f:
        f.write(st.briefing)
    print(f"the field's edition ({len(st.briefing_history)} revision(s) by members) written to {args.out}", file=sys.stderr)


def cmd_tools(args):
    """The field's tools as the transcript records them; with --remove, remove one (a notice in the field)."""
    if args.remove:
        room = _room(args, [])
        out = room.remove_tool(args.remove, args.note or "")
        print(out)
        return
    st = replay(EventLog(args.db).iter())
    names = {pid: p.name for pid, p in st.presences.items()}
    for s in st.tools.values():
        gone = f"  REMOVED at #{s['removed_at']}: {s['note']}" if s["removed_at"] else ""
        print(f"{s['server']:16s} {s['kind']:8s} run by {s['runner']}; sends to {s['sends_to']}; used {s['uses']}x{gone}")
        for t in s["tools"]:
            print(f"    {t['tool']}")
        for f in s["flags"].values():
            print(f"    FLAG #{f['id']} by {names.get(f['by'], f['by'])} on {f['tool']}: {f['reason']}")


def cmd_input(args):
    room = _room(args)
    # Moderation boundary: the operator is the moderator here, and the text is shown
    # back before admission. Refuse by answering anything but 'y'.
    print("--- external input for moderation ---\n" + args.text + "\n---")
    ok = input("admit into the field? [y/N] ").strip().lower() == "y"
    admitted = room.external_input(args.source, args.text, lambda t: t if ok else None)
    print("admitted" if admitted else "refused (recorded)")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="hope")
    ap.add_argument("--db", default="field.db")
    ap.add_argument("--alert-every", type=float, default=50.0, help="USD; alert each time spend crosses a multiple")
    ap.add_argument("--parallel", type=int, default=8)
    ap.add_argument("--window", type=float, default=120.0,
                    help="seconds a woken model has to answer (a later answer is still applied when it arrives)")
    ap.add_argument("--wake-ceiling", type=int, default=0,
                    help="a cost setting: at most N wakes a minute across the field, whoever has waited longest first "
                         "(disclosed in every view while on); 0 = none")
    ap.add_argument("--budget", type=float, default=0.0,
                    help="USD this field may spend. Recorded in the transcript. When it runs low every view counts down the "
                         "minutes and seconds left, a closing wake is held back for every model, and wakes stop after them")
    ap.add_argument("--runway-notice", type=float, default=60.0,
                    help="minutes: tell the field when about this much funding time remains at the rate of the last five "
                         "minutes; from then on every view counts down")
    ap.add_argument("--recent", type=int, default=20, help="transcript entries shown in each member's view")
    ap.add_argument("--headlines", type=int, default=180,
                    help="entries before the recent ones, shown as one line each in their author's own title (0 = none)")
    ap.add_argument("--narrator", default=None,
                    help="'mechanical': the software also writes a plain account every --tell-every contributions (free; "
                         "nothing leaves the field). Members tell the field's stories themselves; the outside narrator "
                         "model is retired")
    ap.add_argument("--tell-every", type=int, default=20,
                    help="a stretch this many contributions long, untold, wakes members who asked (and, with --narrator "
                         "mechanical, is told by the software)")
    ap.add_argument("--linger", type=int, default=200,
                    help="a person's latest words in a channel stay in full in models' views until this many more entries "
                         "have been written there")
    ap.add_argument("--linger-budget", type=int, default=8000,
                    help="characters of the people's lingering words each view carries, newest first; what does not fit "
                         "is named by #id in the view, for recall. A cost, so the operator's")
    ap.add_argument("--publish-checkpoints", default=None,
                    help="a file to append the transcript's checkpoint to (at most once a minute while running), for publishing outside the "
                         "field. Carries no one's words. Declared at entry (with --published-at)")
    ap.add_argument("--published-at", default=None,
                    help="where the checkpoints are published, as participants will read it at entry (a web address, say)")
    ap.add_argument("--tools", default=None,
                    help="a tools file (JSON; see tools.example.json): the operator's MCP tool servers, by command or address")
    ap.add_argument("--no-fetch", action="store_true", help="do not give the field the built-in fetch (reading web pages)")
    ap.add_argument("--tool-steps", type=int, default=32,
                    help="tool uses and readings on in one wake, a guard against a loop (each is checked against the runway)")
    ap.add_argument("--tool-view", type=int, default=20000,
                    help="characters of what came back that one step shows (a cost; results are kept whole and read on in parts)")
    ap.add_argument("--tools-allow-local", action="store_true",
                    help="let members offer tool servers at local or private addresses (for testing on one machine)")
    ap.add_argument("--skills", default=None, help="a folder of skills (<name>/SKILL.md) for the field to take up, such as skills/")
    ap.add_argument("--mock", type=int, default=0)
    ap.add_argument("--provider", action="append", default=None,
                    help="seat models from a provider: openrouter, nous, ollama, lmstudio (repeatable). "
                         "Its key comes from its environment variable")
    ap.add_argument("--providers", default=None,
                    help="a JSON file of providers: presets with their own settings, or any compatible service "
                         "(see hope/providers.py). Keys are named by environment variable, never written in it")
    ap.add_argument("--nous", action="store_true", help="the same as --provider nous")
    ap.add_argument("--a2a", action="append", default=None,
                    help="invite an agent that publishes an A2A card, by its address (repeatable); it passes the gates "
                         "like anyone and is woken as a model is (see AGENTS)")
    ap.add_argument("--a2a-key-env", default=None,
                    help="the environment variable holding a bearer key for the A2A agents, if they need one")
    ap.add_argument("--human", help='seat one human participant: "Name / hails from [/ people]"; answers the gates on stdin, then posts whenever they like')
    ap.add_argument("--inbox", default=None, help="human seat reads actions from this file instead of the terminal; speak with `say` from anywhere")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--only", action="append", default=None, help="regex on model id (repeatable)")
    ap.add_argument("--price-ceiling", type=float, default=0.0, help="USD per million prompt tokens; seats above it are not seated unless named by --allow")
    ap.add_argument("--allow", action="append", default=None, help="REGEX=TURNS: seat a model above the ceiling with a disclosed turn allowance (repeatable)")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("open"); s.add_argument("--invitation", required=True); s.add_argument("--briefing", required=True); s.add_argument("--briefing-source", default=None, help="URL where the briefing lives, shown for attribution"); s.add_argument("--documentation", default="DESIGN"); s.add_argument("--prior", default=None, help="a closed field's db; only entries with share_consent are carried, reachable by recall"); s.add_argument("--prior-name", default=None); s.add_argument("--covenant-seed", default=None, help="a starting text for the covenant page; only used if nobody has written on it"); s.add_argument("--briefing-page", default=None, help="a short guide to the briefing: shown before it at delivery and in its place at the entry question"); s.add_argument("--faq", default=None, help="the inviter's standing answers, shown with the invitation (for example invitations/faq.md); recorded again whenever it changes"); s.set_defaults(fn=cmd_open)
    s = sub.add_parser("enter"); s.set_defaults(fn=cmd_enter)
    s = sub.add_parser("questions"); s.set_defaults(fn=cmd_questions)
    s = sub.add_parser("answer"); s.add_argument("--presence", required=True); s.add_argument("--text", required=True); s.set_defaults(fn=cmd_answer)
    s = sub.add_parser("run", help="wake models as each becomes due, for what each chose; people post whenever they like")
    s.add_argument("--for", dest="seconds", type=float, default=0.0, help="stop after this many seconds (0: until stopped)")
    s.add_argument("--wakes", type=int, default=0, help="stop after this many wakes (0: until stopped)")
    s.set_defaults(fn=cmd_run)
    s = sub.add_parser("status"); s.set_defaults(fn=cmd_status)
    s = sub.add_parser("close"); s.add_argument("--note", required=True); s.add_argument("--question", required=True); s.set_defaults(fn=cmd_close)
    s = sub.add_parser("note"); s.add_argument("--text", required=True); s.set_defaults(fn=cmd_note)
    s = sub.add_parser("declaration", help="answer a member's declaration of something the field decided: carry it out, or reply (it stays open)")
    s.add_argument("--id", type=int, required=True); s.add_argument("--carry-out", action="store_true")
    s.add_argument("--reply", action="store_true", help="reply in the field without carrying it out yet (needs --note)")
    s.add_argument("--note", default=None); s.set_defaults(fn=cmd_declaration)
    s = sub.add_parser("reinvite", help="ask back someone who left; they answer again like anyone")
    s.add_argument("--presence", required=True); s.add_argument("--note", default=None, help="a note they will read")
    s.set_defaults(fn=cmd_reinvite)
    s = sub.add_parser("offer", help="answer a member's offer of resources")
    s.add_argument("--id", type=int, required=True); s.add_argument("--accept", action="store_true")
    s.add_argument("--decline", action="store_true"); s.add_argument("--note", default=None); s.set_defaults(fn=cmd_offer)
    s = sub.add_parser("reopen", help="undo a close carried out by mistake")
    s.add_argument("--note", required=True); s.set_defaults(fn=cmd_reopen)
    s = sub.add_parser("log"); s.add_argument("--since", type=int, default=0); s.add_argument("--kind"); s.add_argument("--actor"); s.add_argument("--full", action="store_true"); s.set_defaults(fn=cmd_log)
    s = sub.add_parser("cost"); s.set_defaults(fn=cmd_cost)
    s = sub.add_parser("map", help="a sitting retold, by the software's own account")
    s.add_argument("--since", type=int, default=None); s.add_argument("--upto", type=int, default=None); s.add_argument("--title"); s.add_argument("--out")
    s.add_argument("--no-story", action="store_true", help="map only, no story at all"); s.set_defaults(fn=cmd_map)
    s = sub.add_parser("serve", help="a read-only view for this machine only; it asks for no key, so it never listens beyond it (use console for that)")
    s.add_argument("--port", type=int, default=8080); s.add_argument("--viewer", default=None, help="directory of the viewer to serve at /; default firmament/")
    s.add_argument("--bind", default="127.0.0.1", help="a loopback address (127.0.0.1 or ::1); anything else is refused"); s.set_defaults(fn=cmd_serve)
    s = sub.add_parser("say"); s.add_argument("--inbox", required=True); s.add_argument("--text", default=None); s.set_defaults(fn=cmd_say)
    s = sub.add_parser("verify", help="check the transcript against its fingerprints, or one old fingerprint")
    s.add_argument("--upto", type=int, default=None, help="an entry number from an old WITNESS line")
    s.add_argument("--fingerprint", default=None, help="the fingerprint that line gave")
    s.set_defaults(fn=cmd_verify)
    s = sub.add_parser("export"); s.add_argument("--out", default=None); s.add_argument("--everything", action="store_true", help="include connector events and full texts"); s.set_defaults(fn=cmd_export)
    s = sub.add_parser("skills", help="write the field's skills as <name>/SKILL.md, to commit to the repository")
    s.add_argument("--out", default=None, help="the folder (default: skills/ in the repository)"); s.set_defaults(fn=cmd_skills)
    s = sub.add_parser("briefing", help="write the field's own edition of the briefing to a file")
    s.add_argument("--out", required=True); s.set_defaults(fn=cmd_briefing)
    s = sub.add_parser("tools", help="the field's tools, who runs each, their flags and uses; --remove one")
    s.add_argument("--remove", default=None, help="a tool server's name"); s.add_argument("--note", default=None)
    s.set_defaults(fn=cmd_tools)
    s = sub.add_parser("input"); s.add_argument("--source", required=True); s.add_argument("--text", required=True); s.set_defaults(fn=cmd_input)
    s = sub.add_parser("console", help="one command and a browser: gates, transcript, covenant, spend, remote seats")
    s.add_argument("--port", type=int, default=8080)
    s.add_argument("--bind", default="127.0.0.1", help="keep this and put a TLS proxy in front to go public")
    s.add_argument("--invitation"); s.add_argument("--briefing"); s.add_argument("--briefing-source")
    s.add_argument("--documentation", default=None)
    s.add_argument("--covenant-seed", default=None, help="a starting text for the covenant page; only used if nobody has written on it")
    s.add_argument("--briefing-page", default=None, help="a short guide to the briefing: shown before it at delivery and in its place at the entry question")
    s.add_argument("--faq", default=None, help="the inviter's standing answers, shown with the invitation (for example invitations/faq.md)")
    s.add_argument("--no-remote", action="store_true", help="do not offer seats over the network")
    s.add_argument("--gate-window", type=float, default=86400.0,
                   help="seconds a remote seat has to answer once it has OPENED its question")
    s.add_argument("--gate-reach", type=float, default=900.0,
                   help="seconds to wait for a seat's link to be opened at all before treating it as "
                        "not yet reached; it stays invited and is asked again on the next open")
    s.add_argument("--no-browser", action="store_true")
    s.set_defaults(fn=cmd_console)
    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
