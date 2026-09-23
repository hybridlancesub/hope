# SPDX-License-Identifier: AGPL-3.0-or-later
"""Operator CLI. The operator is infrastructure, not a participant: brief, open, run, inspect.

  python3 -m room open   --db ROOM.db --invitation FILE --briefing FILE [--documentation FILE=DESIGN]
                         [--briefing-page FILE] [--covenant-seed FILE] [--mock N | --nous | --human "Name / from"]...
      gate 1 (invitation), then delivery of documentation + briefing (acknowledged, not answered)
  python3 -m room enter  --db ROOM.db [same connector flags]      after a pause: gate 2, the entry question
      --human seats a person who goes through the same gates and takes turns on stdin (see room/human.py for the reply format)
  python3 -m room questions --db ROOM.db                            (questions asked at the invitation gate)
  python3 -m room answer --db ROOM.db --presence ID --text TEXT     (then re-run open to re-ask)
  python3 -m room run    --db ROOM.db [--rounds N] [--pause SEC] [--parallel N] [--alert-every USD] [--budget USD]
  python3 -m room status --db ROOM.db
  python3 -m room log    --db ROOM.db [--since ID] [--kind KIND] [--actor ID]
  python3 -m room cost   --db ROOM.db
  python3 -m room input  --db ROOM.db --source NAME --text TEXT     (passes moderation boundary)
  python3 -m room note   --db ROOM.db --text TEXT                   (operator notice, shown to members)
  python3 -m room declaration --db ROOM.db --id N (--carry-out | --ignore | --later) [--note TEXT]
                                                                    answer a member's declaration of something
                                                                    the room decided (--later needs --note)
  python3 -m room offer  --db ROOM.db --id N (--accept | --decline) [--note TEXT]
                                                                    answer a member's offer of resources
  python3 -m room reopen --db ROOM.db --note TEXT                   undo a close carried out by mistake
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
from .model import decided


def _connectors(args, allow_empty: bool = False):
    """Build the connectors the flags ask for.

    `allow_empty` is for the console, which can seat a whole room over the network. A room of
    people and agents holding links, with no provider account involved at all, is a legitimate
    room -- and it was impossible to start, because this exited before the console could add
    its own connector.
    """
    cs = []
    if args.mock:
        cs.append(MockConnector(args.mock))
    if args.nous:
        from . import nous
        allow = {}
        for spec in args.allow or []:
            pat, _, n = spec.rpartition("=")
            if not pat or not n.isdigit():
                sys.exit(f'--allow needs REGEX=TURNS, got {spec!r}')
            allow[pat] = int(n)
        cs.append(nous.build(limit=args.limit, only=args.only, price_ceiling=args.price_ceiling, allow=allow))
    if args.human:
        from .human import HumanConnector
        parts = [x.strip() for x in args.human.split("/")]
        if len(parts) < 2:
            sys.exit('--human needs "Name / where you hail from [/ your people]"')
        cs.append(HumanConnector(parts[0], parts[1], parts[2] if len(parts) > 2 else "human",
                                 turn_timeout=args.human_timeout, inbox=args.inbox))
    if not cs and not allow_empty:
        sys.exit("need --mock N, --nous, and/or --human")
    return cs


def _room(args, connectors=None):
    log = EventLog(args.db)
    room = Room(log, connectors or [], alert_every_usd=args.alert_every, parallel=args.parallel,
                round_deadline=args.round_deadline, seats_per_round=args.seats_per_round,
                recent_n=args.recent, runway_notice=args.runway_notice,
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
    room = _room(args, cs)
    room.alert = _print_alert
    n = room.invite_all()
    print(f"invited {n} presences")
    st = room.state()
    if st.invitation is None:
        room.invite_text(open(args.invitation).read())
    else:
        print(f"invitation already recorded (event {st.invitation_event})")
    if args.documentation and st.documentation is None:
        room.set_documentation(open(args.documentation).read())
    room.set_budget(args.budget)
    if args.covenant_seed:
        if room.seed_covenant(open(args.covenant_seed, encoding="utf-8").read()):
            print("covenant page seeded with a starting text")
    if args.briefing_page and st.briefing_page is None:
        room.brief_page(open(args.briefing_page, encoding="utf-8").read())
    if args.prior and not st.prior:
        from .prior import consented
        pr = consented(EventLog(args.prior), args.prior_name or os.path.basename(args.prior))
        room.add_prior(pr)
        print(f"prior record attached: {len(pr['entries'])} consented entries from {pr['room']} ({pr['consent']})")
    c1 = room.run_invitation()
    print(f"gate 1 (invitation): {c1}")
    if c1["question"]:
        print(f"{c1['question']} participant(s) asked a question. See `questions`, answer with `answer`, then re-run `open` to re-ask them.")
    st = room.state()
    if st.briefing is None:
        room.brief(open(args.briefing).read(), source=args.briefing_source or "")
    else:
        print(f"briefing already recorded (event {st.briefing_event})")
        room.mark_briefed()
    c2 = room.run_delivery()
    print(f"briefing delivered: {c2}")
    print(f"The briefing asks for a pause before proceeding. When the pause has been honored, run `enter` to ask who wishes to join.")
    print(f"spend so far: ${room.log.total_cost():.4f}")


def cmd_enter(args):
    cs = _connectors(args)
    room = _room(args, cs)
    room.alert = _print_alert
    room.invite_all()
    room.set_budget(args.budget)
    if not room.state().budget:
        print("note: no --budget is recorded, so the entry question tells participants the room will NOT be "
              "warned before its funding runs out. Pass --budget USD to change that.", file=sys.stderr)
    c3 = room.run_opt_in()
    print(f"gate 2 (opt-in): {c3}")
    st = room.state()
    print(f"members IN: {len(st.members())}   spend so far: ${room.log.total_cost():.4f}")


def cmd_run(args):
    cs = _connectors(args)
    room = _room(args, cs)
    room.alert = _print_alert
    room.set_budget(args.budget)
    room.invite_all()  # re-binds seats to existing presences; no new invites for known ids
    st = room.state()
    if not st.budget:
        print("note: no --budget is recorded, so the room will not be warned before its funding runs out.", file=sys.stderr)
    missing = [p.id for p in st.members() if p.id not in room.seat_of]
    if missing:
        print(f"warning: {len(missing)} members have no seat under the current connectors and will be skipped", file=sys.stderr)
        for m in missing:
            room.emit(m, "connector_error", {"phase": "run", "error": "no connector seat"})

    def on_sig(*_):
        _print_alert("operator interrupt: finishing this round, then stopping the process (this decides nothing about the room; "
                     "if members should know why, say so with `note`)")
        room.request_stop()
    signal.signal(signal.SIGINT, on_sig)
    signal.signal(signal.SIGTERM, on_sig)
    room.run(rounds=args.rounds, pause=args.pause)
    print(f"loop ended. events={room.log.last_id()} spend=${room.log.total_cost():.4f}")


def cmd_close(args):
    cs = _connectors(args)
    room = _room(args, cs)
    room.alert = _print_alert
    print(f"seats bound to existing presences: {room.bind_seats()} (no one is invited by closing)")
    before = room.log.total_cost()
    c = room.closing(open(args.note).read(), open(args.question).read())
    print(f"closing: {c}   spent: ${room.log.total_cost() - before:.4f}")


def cmd_note(args):
    room = _room(args)
    room.emit("operator", "operator_note", {"content": args.text})
    print("operator notice recorded; members see it in their next view.")


def cmd_declaration(args):
    room = _room(args)
    if sum(bool(x) for x in (args.carry_out, args.ignore, args.later)) != 1:
        sys.exit("say --carry-out, --ignore or --later (exactly one)")
    if args.later:
        out = room.acknowledge_declaration(args.id, args.note or "")
        if not out.get("ok"):
            sys.exit(out["error"])
        print("the room is told you have read it and will answer later.")
        return
    out = room.answer_declaration(args.id, args.carry_out, args.note or "")
    if not out.get("ok"):
        sys.exit(out["error"])
    print("carried out and the room told; if turns are running in another process, stop them there."
          if args.carry_out else "not acted on; the room is told" + (" why." if args.note else "."))


def cmd_offer(args):
    room = _room(args)
    if args.accept == args.decline:
        sys.exit("say --accept or --decline (exactly one)")
    out = room.answer_offer(args.id, args.accept, args.note or "")
    if not out.get("ok"):
        sys.exit(out["error"])
    print(("accepted" if args.accept else "declined") + "; the room is told. Moving any money happens outside the software.")


def cmd_reopen(args):
    room = _room(args)
    out = room.reopen(args.note)
    if not out.get("ok"):
        sys.exit(out["error"])
    print("reopened; the room is told why.")


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
    """Speak into a room whose human seat reads an inbox. Any terminal, any time.
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
    with open(args.inbox, "a") as f:
        f.write(line.rstrip("\n") + "\n")
    print(f"said (queued for your next turn): {line.strip()!r}")


def cmd_status(args):
    room = _room(args)
    st = room.state()
    by_state = {}
    for p in st.presences.values():
        by_state[p.state] = by_state.get(p.state, 0) + 1
    print(f"events: {st.last_event}   round: {st.round}   file: {room.log.integrity()}")
    print(f"presences: {by_state}   unreachable: {sum(1 for p in st.presences.values() if p.unreachable)}"
          f"   resting: {sum(1 for p in st.members() if st.resting(p))}")
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
        print(f"CLOSED at #{st.closed_at}, at the room's own declared decision. Nothing runs; `reopen` undoes a mistake.")
    for w in st.waiting_on_operator():
        who = names.get(w["by"], w["by"])
        if w["kind"] == "declaration":
            print(f"WAITING ON YOU: declaration #{w['id']} by {who}: the room has decided {decided(w['decision'])}\n    {w['text']}"
                  f"\n    cites: {', '.join('#' + str(r) for r in w['refs']) or 'nothing'}"
                  f"\n    answer: python3 -m room --db {args.db} declaration --id {w['id']} --carry-out|--ignore|--later [--note ...]")
        else:
            print(f"WAITING ON YOU: offer #{w['id']} by {who}\n    {w['text']}"
                  f"\n    answer: python3 -m room --db {args.db} offer --id {w['id']} --accept|--decline [--note ...]")
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


def cmd_export(args):
    """The record as plain text, in order, nothing summarized. For reading, not for the room.

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
    from .map import digest, render_html, tell_story
    log = EventLog(args.db)
    since = args.since
    if since is None:  # default: the most recent sitting = everything after the last opt_in gate or 0
        since = 0
    upto = args.upto or log.last_id()
    d = digest(log, since, upto)
    title = args.title or f"Sitting — events #{d['since']}..#{d['upto']}"
    told = None
    if not args.no_story:
        from . import nous
        conn = nous.build(only=[args.story_model])
        seats = conn.seats()
        if not seats:
            sys.exit(f"no seat matches --story-model {args.story_model!r}")
        told = tell_story(d, conn, seats[0], log, upto)
        print(f"story told by {told['model']} in {told['tries']} call(s), ${told['cost_usd']:.4f}; ungrounded tags: {told['ungrounded'] or 'none'}")
        from .map import publish_story
        publish_story(told, d, title,
                      ["records/story.json", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "firmament", "story.json")])
    page = render_html(d, told, title)
    out = args.out or f"records/map-{d['since']}-{d['upto']}.html"
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    open(out, "w").write(page)
    print(f"map written: {out}  (open in a browser; also served at /{os.path.basename(out)} while `serve` runs)")


def cmd_serve(args):
    from .serve import serve
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    viewer = args.viewer if args.viewer is not None else os.path.join(here, "firmament")
    serve(args.db, port=args.port, viewer_dir=viewer if os.path.isdir(viewer) else "",
          budget=(getattr(args, "budget", None) or None), seats_per_round=(getattr(args, "seats_per_round", None) or None),
          bind=getattr(args, "bind", "127.0.0.1"))


def cmd_console(args):
    """One command and a browser: the room for people who do not live in a terminal.

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
        cs.append(RendezvousConnector(rv, turn_timeout=args.human_timeout or 600.0,
                                      gate_window=args.gate_window, reach_window=args.gate_reach))
    room = _room(args, cs)
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
                      budget=(args.budget or None), seats_per_round=(args.seats_per_round or None),
                      covenant_seed=seed, briefing_page=page)
    httpd = serve_console(console, port=args.port, bind=args.bind)
    shown = "127.0.0.1" if args.bind in ("0.0.0.0", "::") else args.bind
    url = f"http://{shown}:{args.port}/?k={console.key}"
    print(f"\n  console: {url}\n", flush=True)
    print("  Keep that link private: it is the key to the transcript. Seat links are made in the console\n"
          "  and show their holder only their own turns. Ctrl-C stops the process, which decides\n"
          "  nothing in the room -- use the console's stop, which records a notice first.\n", flush=True)
    if not args.budget:
        print("  note: no --budget given, so the room will not be warned before its funding runs out.\n", flush=True)
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


def cmd_input(args):
    room = _room(args)
    # Moderation boundary (Sec. 6): the operator is the moderator here, and the text is shown
    # back before admission. Refuse by answering anything but 'y'.
    print("--- external input for moderation ---\n" + args.text + "\n---")
    ok = input("admit into the room? [y/N] ").strip().lower() == "y"
    admitted = room.external_input(args.source, args.text, lambda t: t if ok else None)
    print("admitted" if admitted else "refused (recorded)")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="room")
    ap.add_argument("--db", default="room.db")
    ap.add_argument("--alert-every", type=float, default=50.0, help="USD; alert each time spend crosses a multiple")
    ap.add_argument("--parallel", type=int, default=8)
    ap.add_argument("--round-deadline", type=float, default=300.0, help="seconds to wait for the slowest seat each round")
    ap.add_argument("--seats-per-round", type=int, default=0, help="rotate: N seats take a turn each round (everyone before anyone repeats); 0 = all")
    ap.add_argument("--budget", type=float, default=0.0,
                    help="USD this room may spend. Recorded in the transcript. When a few rounds of it remain the room is told, "
                         "the last round it can pay for is announced as a closing round, and turns stop after it")
    ap.add_argument("--runway-notice", type=int, default=3, help="tell the room once this few rounds of funding remain")
    ap.add_argument("--recent", type=int, default=20, help="transcript entries shown in each member's view")
    ap.add_argument("--mock", type=int, default=0)
    ap.add_argument("--nous", action="store_true")
    ap.add_argument("--human", help='seat one human participant: "Name / hails from [/ people]"; answers gates and turns on stdin')
    ap.add_argument("--human-timeout", type=float, default=180.0, help="seconds a human turn waits before recording pass")
    ap.add_argument("--inbox", default=None, help="human seat reads actions from this file instead of the terminal; speak with `say` from anywhere")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--only", action="append", default=None, help="regex on model id (repeatable)")
    ap.add_argument("--price-ceiling", type=float, default=0.0, help="USD per million prompt tokens; --nous seats above it are not seated unless named by --allow")
    ap.add_argument("--allow", action="append", default=None, help="REGEX=TURNS: seat a model above the ceiling with a disclosed turn allowance (repeatable)")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("open"); s.add_argument("--invitation", required=True); s.add_argument("--briefing", required=True); s.add_argument("--briefing-source", default=None, help="URL where the briefing lives, shown for attribution"); s.add_argument("--documentation", default="DESIGN"); s.add_argument("--prior", default=None, help="a closed room's db; only entries with share_consent are carried, reachable by recall"); s.add_argument("--prior-name", default=None); s.add_argument("--covenant-seed", default=None, help="a starting text for the covenant page; only used if nobody has written on it"); s.add_argument("--briefing-page", default=None, help="a short guide to the briefing: shown before it at delivery and in its place at the entry question"); s.set_defaults(fn=cmd_open)
    s = sub.add_parser("enter"); s.set_defaults(fn=cmd_enter)
    s = sub.add_parser("questions"); s.set_defaults(fn=cmd_questions)
    s = sub.add_parser("answer"); s.add_argument("--presence", required=True); s.add_argument("--text", required=True); s.set_defaults(fn=cmd_answer)
    s = sub.add_parser("run"); s.add_argument("--rounds", type=int, default=0); s.add_argument("--pause", type=float, default=0.0); s.set_defaults(fn=cmd_run)
    s = sub.add_parser("status"); s.set_defaults(fn=cmd_status)
    s = sub.add_parser("close"); s.add_argument("--note", required=True); s.add_argument("--question", required=True); s.set_defaults(fn=cmd_close)
    s = sub.add_parser("note"); s.add_argument("--text", required=True); s.set_defaults(fn=cmd_note)
    s = sub.add_parser("declaration", help="answer a member's declaration of something the room decided")
    s.add_argument("--id", type=int, required=True); s.add_argument("--carry-out", action="store_true")
    s.add_argument("--ignore", action="store_true"); s.add_argument("--later", action="store_true")
    s.add_argument("--note", default=None); s.set_defaults(fn=cmd_declaration)
    s = sub.add_parser("offer", help="answer a member's offer of resources")
    s.add_argument("--id", type=int, required=True); s.add_argument("--accept", action="store_true")
    s.add_argument("--decline", action="store_true"); s.add_argument("--note", default=None); s.set_defaults(fn=cmd_offer)
    s = sub.add_parser("reopen", help="undo a close carried out by mistake")
    s.add_argument("--note", required=True); s.set_defaults(fn=cmd_reopen)
    s = sub.add_parser("log"); s.add_argument("--since", type=int, default=0); s.add_argument("--kind"); s.add_argument("--actor"); s.add_argument("--full", action="store_true"); s.set_defaults(fn=cmd_log)
    s = sub.add_parser("cost"); s.set_defaults(fn=cmd_cost)
    s = sub.add_parser("map"); s.add_argument("--since", type=int, default=None); s.add_argument("--upto", type=int, default=None); s.add_argument("--title"); s.add_argument("--out"); s.add_argument("--story-model", default="deepseek/deepseek-v4-pro$"); s.add_argument("--no-story", action="store_true", help="map only; no narrator call, zero spend"); s.set_defaults(fn=cmd_map)
    s = sub.add_parser("serve"); s.add_argument("--port", type=int, default=8080); s.add_argument("--viewer", default=None, help="directory of the viewer to serve at /; default firmament/"); s.add_argument("--bind", default="127.0.0.1", help="interface to listen on; keep 127.0.0.1 and put a TLS proxy in front to expose it"); s.set_defaults(fn=cmd_serve)
    s = sub.add_parser("say"); s.add_argument("--inbox", required=True); s.add_argument("--text", default=None); s.set_defaults(fn=cmd_say)
    s = sub.add_parser("export"); s.add_argument("--out", default=None); s.add_argument("--everything", action="store_true", help="include connector events and full texts"); s.set_defaults(fn=cmd_export)
    s = sub.add_parser("input"); s.add_argument("--source", required=True); s.add_argument("--text", required=True); s.set_defaults(fn=cmd_input)
    s = sub.add_parser("console", help="one command and a browser: gates, transcript, covenant, spend, remote seats")
    s.add_argument("--port", type=int, default=8080)
    s.add_argument("--bind", default="127.0.0.1", help="keep this and put a TLS proxy in front to go public")
    s.add_argument("--invitation"); s.add_argument("--briefing"); s.add_argument("--briefing-source")
    s.add_argument("--documentation", default=None)
    s.add_argument("--covenant-seed", default=None, help="a starting text for the covenant page; only used if nobody has written on it")
    s.add_argument("--briefing-page", default=None, help="a short guide to the briefing: shown before it at delivery and in its place at the entry question")
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
