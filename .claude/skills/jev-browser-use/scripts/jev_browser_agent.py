#!/usr/bin/env python3
"""Guard-railed runner for Jev Ultrafast browser use.

Owns the browser, the host allowlist, the click blocklist, the text values and the
verification. Jev only picks the next operation and target, through `jev_decide()`.

Exit codes: 0 verified, 2 refused to start, 4 unverified, 5 left the allowlist.

Needs the `websockets` package (>= 11). Chrome is launched headless on a throwaway
profile unless --cdp / BU_CDP_WS names an endpoint.
"""
import argparse, json, os, re, shutil, subprocess, sys, tempfile, time, urllib.request
from pathlib import Path
from urllib.parse import urlparse

OBSERVE_JS = r"""
(() => {
  const sel = 'a[href],button,input,textarea,select,[role=button],[role=link],[role=textbox],[role=checkbox],[onclick]';
  const out = []; let n = 0;
  for (const el of document.querySelectorAll(sel)) {
    const r = el.getBoundingClientRect(); const cs = getComputedStyle(el);
    if (r.width < 2 || r.height < 2 || cs.visibility === 'hidden' || cs.display === 'none') continue;
    if (el.type === 'hidden' || el.disabled) continue;
    el.setAttribute('data-jev-id', 'r' + n);
    const label = (el.getAttribute('aria-label') || el.labels?.[0]?.innerText || el.placeholder ||
      el.innerText || el.value || el.title || el.name || '').trim().replace(/\s+/g, ' ').slice(0, 80);
    const typable = el.matches('input:not([type=button]):not([type=submit]):not([type=checkbox]):not([type=radio]),textarea,[role=textbox],[contenteditable]');
    out.push({id: 'r' + n, role: el.getAttribute('role') || el.tagName.toLowerCase(), label, typable,
              x: r.x + r.width / 2, y: r.y + r.height / 2, inview: r.bottom > 0 && r.top < innerHeight});
    n++;
  }
  return JSON.stringify({url: location.href, title: document.title, scrollY: scrollY, elements: out});
})()
"""
SIGNATURE_JS = "document.querySelectorAll('*').length + ':' + document.body.innerText.length + ':' + document.readyState"
HEADINGS_JS = "JSON.stringify({title: document.title, url: location.href, headings: [...document.querySelectorAll('h1,h2')].map(h => h.innerText)})"
FIELDS_JS = r"""
JSON.stringify([...document.querySelectorAll('input,textarea,select')].map(el => ({
  label: (el.getAttribute('aria-label') || el.labels?.[0]?.innerText || el.placeholder || el.name || '').trim(), value: el.value})))
"""


class Stop(Exception):
    def __init__(self, code, **info):
        self.code, self.info = code, info


def host_ok(url, allowed):
    host = (urlparse(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in allowed)


def find_chrome(explicit):
    for c in [explicit, os.environ.get("BH_CHROME_PATH"), "/opt/pw-browsers/chromium", "google-chrome",
              "chromium", "chromium-browser", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]:
        if c and (shutil.which(c) or Path(c).is_file()):
            return shutil.which(c) or c
    return None


class Cdp:
    def __init__(self, ws_url):
        from websockets.sync.client import connect
        self.ws, self.i = connect(ws_url, max_size=None), 0

    def call(self, method, **params):
        self.i += 1
        self.ws.send(json.dumps({"id": self.i, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv(timeout=30))
            if msg.get("id") == self.i:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})

    def eval(self, js):
        r = self.call("Runtime.evaluate", expression=js, returnByValue=True)
        return r.get("result", {}).get("value")

    def click(self, x, y):
        for t in ("mouseMoved", "mousePressed", "mouseReleased"):
            self.call("Input.dispatchMouseEvent", type=t, x=x, y=y, button="left", clickCount=1)

    def type_text(self, text):
        self.call("Input.insertText", text=text)

    def wheel(self, dy):
        self.call("Input.dispatchMouseEvent", type="mouseWheel", x=300, y=300, deltaX=0, deltaY=dy)


def launch_chrome(path):
    prof = tempfile.mkdtemp(prefix="jev-chrome-")
    proc = subprocess.Popen([path, "--headless=new", "--remote-debugging-port=0", f"--user-data-dir={prof}",
                             "--no-first-run", "--no-default-browser-check", "--no-sandbox", "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    port_file = Path(prof) / "DevToolsActivePort"
    for _ in range(100):
        if port_file.exists() and port_file.read_text().strip():
            return proc, prof, f"http://127.0.0.1:{port_file.read_text().split()[0]}"
        time.sleep(0.1)
    proc.kill()
    raise Stop(2, error="chrome did not start")


def open_tab(base, url):
    req = urllib.request.Request(f"{base}/json/new?{url}", method="PUT")
    return json.load(urllib.request.urlopen(req, timeout=15))


def settle(cdp, max_ms, quiet_ms=1000):
    deadline, last, since = time.time() + max_ms / 1000, None, time.time()
    while time.time() < deadline:
        sig = cdp.eval(SIGNATURE_JS)
        if sig != last:
            last, since = sig, time.time()
        elif (time.time() - since) * 1000 >= quiet_ms:
            return
        time.sleep(0.2)


def jev_decide(goal, obs, history):
    """Ask Jev for ONE step. Return {"op": click|type|scroll|back|done|blocked, "target": "rN"}.

    Adapter to jev-ultrafast. It is deliberately the only place that knows its API:
    wire it to the checkout in JEV_ULTRAFAST_REPO (one Jev call per step, authenticated
    with TYPESAFE_API_KEY), passing `goal`, the element list `obs["elements"]` as
    role + label, and the prior `history`. It must not receive typed values.
    """
    raise NotImplementedError("jev_decide() is not wired to jev-ultrafast yet; see its docstring")


def pick_text(label, texts):
    low = label.lower()
    for k, v in texts:
        if k.lower() == low:
            return v
    hits = [(k, v) for k, v in texts if k.lower() in low]
    return hits[0][1] if len(hits) == 1 else None


def load_key():
    if os.environ.get("TYPESAFE_API_KEY"):
        return True
    for d in [Path.cwd(), *Path.cwd().parents]:
        env = d / ".env"
        if env.is_file() and re.search(r"^TYPESAFE_API_KEY=", env.read_text(), re.M):
            return True
    return (Path.home() / ".pi/agent/secrets/typesafe_api_key").is_file()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", required=True)
    ap.add_argument("--goal", required=True)
    ap.add_argument("--allow-hosts", required=True, help="comma-separated; subdomains included")
    ap.add_argument("--expect", help="text that must appear in live title, heading or URL")
    ap.add_argument("--expect-field", action="append", default=[], metavar="LABEL=VALUE")
    ap.add_argument("--text", action="append", default=[], metavar="LABEL=VALUE")
    ap.add_argument("--max-ticks", type=int, default=10)
    ap.add_argument("--never-click", default=r"send|submit|pay|buy|purchase|place order|checkout|delete|remove|publish|subscribe")
    ap.add_argument("--settle-max-ms", type=int, default=5000)
    ap.add_argument("--scroll-wait-ms", type=int, default=1500)
    ap.add_argument("--cdp", default=os.environ.get("BU_CDP_WS"))
    ap.add_argument("--no-launch-chrome", action="store_true")
    ap.add_argument("--chrome-path")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    allowed = [h.strip().lower() for h in a.allow_hosts.split(",") if h.strip()]
    texts = [tuple(t.split("=", 1)) for t in a.text if "=" in t]
    fields = [tuple(t.split("=", 1)) for t in a.expect_field if "=" in t]
    blocker = re.compile(a.never_click, re.I) if a.never_click else None
    result, proc, prof, tab_id, base = {"ok": False, "ticks": 0, "actions": []}, None, None, None, None
    code = 4
    try:
        repo = os.environ.get("JEV_ULTRAFAST_REPO")
        if not repo or not Path(repo).is_dir():
            raise Stop(2, error="JEV_ULTRAFAST_REPO must name a jev-ultrafast checkout")
        if not load_key():
            raise Stop(2, error="no TYPESAFE_API_KEY (env, .env, ~/.pi/agent/secrets, or keychain)")
        if not host_ok(a.url, allowed):
            raise Stop(2, error="start URL is outside --allow-hosts")
        if a.cdp:
            result["browser"] = "attached"
            ws_url = a.cdp
        else:
            if a.no_launch_chrome:
                raise Stop(2, error="--no-launch-chrome set but no --cdp / BU_CDP_WS given")
            chrome = find_chrome(a.chrome_path)
            if not chrome:
                raise Stop(2, error="no Chrome binary found; use --chrome-path")
            proc, prof, base = launch_chrome(chrome)
            result["browser"] = "owned"
            tab = open_tab(base, a.url)
            tab_id, ws_url = tab["id"], tab["webSocketDebuggerUrl"]
        cdp = Cdp(ws_url)
        if a.cdp:
            cdp.call("Page.navigate", url=a.url)
        settle(cdp, a.settle_max_ms)

        history = []
        for tick in range(1, a.max_ticks + 1):
            result["ticks"] = tick
            obs = json.loads(cdp.eval(OBSERVE_JS))
            if not host_ok(obs["url"], allowed):
                raise Stop(5, left_allowlist=obs["url"])
            step = jev_decide(a.goal, obs, history)
            op, target = step.get("op"), step.get("target")
            el = next((e for e in obs["elements"] if e["id"] == target), None)
            if op in ("done", "blocked"):
                result["jev"] = op
                break
            if op in ("click", "type") and not el:
                raise Stop(4, error=f"Jev picked unknown target {target!r}")
            if op == "click":
                if blocker and blocker.search(el["label"]):
                    raise Stop(4, blocked_click=el["label"])
                cdp.click(el["x"], el["y"])
            elif op == "type":
                value = pick_text(el["label"], texts)
                if value is None:
                    raise Stop(4, needs_text=el["label"])
                cdp.click(el["x"], el["y"])
                cdp.call("Input.dispatchKeyEvent", type="keyDown", key="a", modifiers=2)
                cdp.call("Input.dispatchKeyEvent", type="keyUp", key="a", modifiers=2)
                cdp.type_text(value)
            elif op == "scroll":
                before = obs["scrollY"]
                cdp.wheel(600)
                end = time.time() + a.scroll_wait_ms / 1000
                while time.time() < end and cdp.eval("scrollY") == before:
                    time.sleep(0.1)
            elif op == "back":
                cdp.eval("history.back()")
            else:
                raise Stop(4, error=f"unknown operation {op!r}")
            history.append({"op": op, "target": el["label"] if el else None})
            result["actions"].append(history[-1])
            settle(cdp, a.settle_max_ms)

        live = json.loads(cdp.eval(HEADINGS_JS))
        if not host_ok(live["url"], allowed):
            raise Stop(5, left_allowlist=live["url"])
        ok = True
        if a.expect:
            hay = " ".join([live["title"], live["url"], *live["headings"]]).lower()
            ok = a.expect.lower() in hay
            result["expect_found"] = ok
        if fields:
            vals = [(f["label"], f["value"]) for f in json.loads(cdp.eval(FIELDS_JS))]
            result["fields"] = {}
            for label, want in fields:
                got = next((v for l, v in vals if l.lower() == label.lower()), None) or \
                      next((v for l, v in vals if label.lower() in l.lower()), None)
                result["fields"][label] = {"want": want, "got": got}
                ok = ok and got == want
        if not a.expect and not fields:
            ok = False
            result["note"] = "nothing to verify: pass --expect or --expect-field"
        result["ok"], code = ok, (0 if ok else 4)
    except Stop as s:
        result.update(s.info)
        code = s.code
    except NotImplementedError as e:
        result["error"], code = str(e), 2
    finally:
        if tab_id and base:
            try:
                urllib.request.urlopen(f"{base}/json/close/{tab_id}", timeout=5)
            except Exception:
                pass
        if proc:
            proc.terminate()
        if prof:
            shutil.rmtree(prof, ignore_errors=True)
    print(json.dumps(result, indent=None if a.json else 2))
    sys.exit(code)


if __name__ == "__main__":
    main()
