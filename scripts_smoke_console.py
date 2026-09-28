# SPDX-License-Identifier: AGPL-3.0-or-later
"""End-to-end smoke of `python3 -m hope console`: starts the real command, walks both gates
with mock seats, leaves one remote seat unanswered to prove silence is not recorded as a
decline, lets another remote seat walk the gates and then post from its page, runs the field
for a few seconds, and checks that a seat link cannot read the record. Costs nothing."""
import json, os, subprocess, sys, tempfile, threading, time, urllib.request, urllib.error
PY = sys.executable
tmp = tempfile.mkdtemp(); db = os.path.join(tmp, "smoke.db")
inv = os.path.join(tmp, "inv.md"); brf = os.path.join(tmp, "brf.md")
open(inv, "w", encoding="utf-8").write("An invitation. Accepting commits you to nothing but hearing more.")
open(brf, "w", encoding="utf-8").write("Shared frame: test that the console works end to end.")
env = dict(os.environ, ROOM_OPERATOR_KEY="SMOKE", PYTHONUNBUFFERED="1")
p = subprocess.Popen([PY, "-m", "hope", "--db", db, "--mock", "2", "--budget", "5",
                      "console", "--port", "8099", "--no-browser", "--gate-window", "20", "--gate-reach", "3",
                      "--invitation", inv, "--briefing", brf],
                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
B = "http://127.0.0.1:8099"
def get(path, key="SMOKE"):
    u = B + path + (("?k=" + key) if key else "")
    try:
        with urllib.request.urlopen(u, timeout=5) as r: return r.status, r.read().decode()
    except urllib.error.HTTPError as e: return e.code, e.read().decode()
def post(path, obj, key="SMOKE"):
    req = urllib.request.Request(B + path + ("?k=" + key if key else ""), data=json.dumps(obj).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r: return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e: return e.code, json.loads(e.read())
def settle():
    for _ in range(200):
        s = json.loads(get("/op/state.json")[1])
        if not s["console"]["busy"]: return s
        time.sleep(0.2)
    return json.loads(get("/op/state.json")[1])
ok = True
def check(label, cond, extra=""):
    global ok
    print(("  PASS  " if cond else "  FAIL  ") + label + ("" if cond else "   " + str(extra)[:200]))
    ok = ok and cond

def holder(path, answers):
    """A person on another machine, answering each gate question as it appears on their page."""
    done = set()
    def run():
        i, deadline = 0, time.time() + 60
        while i < len(answers) and time.time() < deadline:
            try:
                t = json.loads(get(path + "turn.json", key=None)[1])
            except Exception:
                t = {}
            if t.get("state") == "your_turn" and t.get("turn_id") not in done:
                c, out = post(path + "action", {"text": answers[i], "turn_id": t["turn_id"]}, key=None)
                if out.get("ok"):
                    done.add(t["turn_id"]); i += 1
            time.sleep(0.1)
    threading.Thread(target=run, daemon=True).start()

try:
    for _ in range(100):
        try:
            urllib.request.urlopen(B + "/op/state.json?k=SMOKE", timeout=2); break
        except Exception: time.sleep(0.1)
    check("console page serves with the key", get("/")[0] == 200)
    check("console page refuses without it", get("/", key=None)[0] == 401)
    c, seat = post("/op/seat", {"name": "Rook", "hails_from": "a laptop"})
    check("a seat link can be minted", c == 200 and seat.get("path", "").startswith("/seat/"), seat)
    tok = seat["path"].split("/")[2]
    check("the seat link opens its page", get(seat["path"], key=None)[0] == 200)
    check("the seat link cannot read the record", get("/record.txt", key=tok)[0] == 401)
    c, wren = post("/op/seat", {"name": "Wren", "hails_from": "a kitchen table"})
    holder(wren["path"], ["yes", "received", "yes, gladly"])
    c, d = post("/op/phase", {"phase": "open"})
    check("open starts", c == 200, d)
    s = settle()
    stages = {r["name"]: r["stage"] for r in s["admission"]}
    check("mock seats walked the gates", all(stages[k] in ("RECEIVED", "IN") for k in ("Mock 0", "Mock 1")), stages)
    check("the unopened seat is NOT recorded as a decline", stages["Rook"] == "INVITED", stages)
    rook = [r for r in s["admission"] if r["name"] == "Rook"][0]
    check("nothing about its will was written", not rook["left_reason"], rook)
    check("it can still be asked again", rook["stage"] == "INVITED", rook)
    c, d = post("/op/phase", {"phase": "enter"})
    s = settle()
    stages = {r["name"]: r["stage"] for r in s["admission"]}
    check("the person holding a link entered", stages.get("Wren") == "IN", stages)
    t = json.loads(get(wren["path"] + "turn.json", key=None)[1])
    check("after entry, nothing is asked of them: their page shows the field", t.get("state") == "in_field", t)
    f = json.loads(get(wren["path"] + "field.json", key=None)[1])
    check("their page shows what is new, and where they can speak",
          "view" in f and any(ch["key"] == "d:" for ch in f.get("channels", [])), list(f))
    c, out = post(wren["path"] + "post", {"text": "@smoke hello, whenever I like", "channel": "d:"}, key=None)
    check("they post whenever they like", c == 200 and out.get("ok"), out)
    c, out = post(wren["path"] + "post", {"text": "form tempo / a slow look at time"}, key=None)
    check("and can form a circle", c == 200 and out.get("ok"), out)
    c, d = post("/op/phase", {"phase": "run", "seconds": 3})
    check("the field runs", c == 200, d)
    s = settle()
    check("members are in the field", len([m for m in s["members"] if m["state"] == "IN"]) >= 3, s["admission"])
    check("models were woken by what was said", s.get("wakes", 0) >= 2, s.get("wakes"))
    check("the circle is listed for the operator", any(c["name"] == "tempo" for c in s.get("circles", [])), s.get("circles"))
    check("spend block present", "total_usd" in s["spend"] and "seconds_left" in s["spend"], s.get("spend"))
    check("the covenant page and memories are in the operator's state", "covenant" in s and "memories" in s, list(s))
    check("the budget was recorded for the field", s["spend"].get("budget_usd") == 5.0, s.get("spend"))
    code, page = get("/")
    check("the console has a Covenant page and no Proposals page",
          'data-page="covenant"' in page and 'data-page="proposals"' not in page)
    code, rec = get("/record.txt")
    check("raw stream is readable", code == 200 and "contribute by" in rec and "whenever I like" in rec, rec[:150])
    c, d = post("/op/halt", {})
    check("there is no operator halt", c == 400 and "unknown action" in d.get("error", ""), d)
    c, d = post("/op/stop", {})
    check("stop without a notice is refused", c == 400, d)
    c, d = post("/op/stop", {"note": "Smoke test finished; stopping the process."})
    check("stop with a notice works", c == 200, d)
finally:
    p.terminate()
    try: p.wait(timeout=10)
    except Exception: p.kill()
print("\nSMOKE " + ("OK" if ok else "FAILED"))
sys.exit(0 if ok else 1)
