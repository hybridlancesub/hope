# SPDX-License-Identifier: AGPL-3.0-or-later
"""End-to-end smoke of `python3 -m room console`: starts the real command, walks both gates
with mock seats, leaves one remote seat unanswered to prove silence is recorded as a decline,
runs rounds, and checks that a seat link cannot read the record. Costs nothing."""
import json, os, subprocess, sys, tempfile, time, urllib.request, urllib.error
PY = sys.executable
tmp = tempfile.mkdtemp(); db = os.path.join(tmp, "smoke.db")
inv = os.path.join(tmp, "inv.md"); brf = os.path.join(tmp, "brf.md")
open(inv, "w").write("An invitation. Accepting commits you to nothing but hearing more.")
open(brf, "w").write("Shared frame: test that the console works end to end.")
env = dict(os.environ, ROOM_OPERATOR_KEY="SMOKE", PYTHONUNBUFFERED="1")
p = subprocess.Popen([PY, "-m", "room", "--db", db, "--mock", "2", "--budget", "5",
                      "console", "--port", "8099", "--no-browser", "--gate-window", "4", "--gate-reach", "3",
                      "--invitation", inv, "--briefing", brf],
                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
B = "http://127.0.0.1:8099"
def get(path, key="SMOKE"):
    u = B + path + (("?k=" + key) if key else "")
    try:
        with urllib.request.urlopen(u, timeout=5) as r: return r.status, r.read().decode()
    except urllib.error.HTTPError as e: return e.code, e.read().decode()
def post(path, obj, key="SMOKE"):
    req = urllib.request.Request(B + path + "?k=" + key, data=json.dumps(obj).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r: return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e: return e.code, json.loads(e.read())
ok = True
def check(label, cond, extra=""):
    global ok
    print(("  PASS  " if cond else "  FAIL  ") + label + ("" if cond else "   " + str(extra)[:200]))
    ok = ok and cond
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
    c, d = post("/op/phase", {"phase": "open"})
    check("open starts", c == 200, d)
    for _ in range(150):
        s = json.loads(get("/op/state.json")[1])
        if not s["console"]["busy"]: break
        time.sleep(0.2)
    s = json.loads(get("/op/state.json")[1])
    stages = {r["name"]: r["stage"] for r in s["admission"]}
    check("mock seats walked the gates", all(stages[k] in ("RECEIVED", "IN") for k in ("Mock 0", "Mock 1")), stages)
    check("the unopened seat is NOT recorded as a decline", stages["Rook"] == "INVITED", stages)
    rook = [r for r in s["admission"] if r["name"] == "Rook"][0]
    check("nothing about its will was written", not rook["left_reason"], rook)
    check("it can still be asked again", rook["stage"] == "INVITED", rook)
    c, d = post("/op/phase", {"phase": "enter"})
    for _ in range(150):
        s = json.loads(get("/op/state.json")[1])
        if not s["console"]["busy"]: break
        time.sleep(0.2)
    c, d = post("/op/phase", {"phase": "run", "rounds": 2})
    for _ in range(200):
        s = json.loads(get("/op/state.json")[1])
        if not s["console"]["busy"]: break
        time.sleep(0.2)
    s = json.loads(get("/op/state.json")[1])
    check("members entered and took turns", len([m for m in s["members"] if m["state"] == "IN"]) >= 1, s["admission"])
    check("spend block present", "total_usd" in s["spend"], s.get("spend"))
    check("the covenant page and memories are in the operator's state", "covenant" in s and "memories" in s, list(s))
    check("the budget was recorded for the room", s["spend"].get("budget_usd") == 5.0, s.get("spend"))
    code, page = get("/")
    check("the console has a Covenant page and no Proposals page",
          'data-page="covenant"' in page and 'data-page="proposals"' not in page)
    code, rec = get("/record.txt")
    check("raw stream is readable", code == 200 and "contribute by" in rec, rec[:150])
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
