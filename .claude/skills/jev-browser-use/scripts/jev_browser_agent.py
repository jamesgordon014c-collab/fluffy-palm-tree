#!/usr/bin/env python3
"""Guard-railed runner for Jev Ultrafast browser use.

Drives jev_ultrafast.Agent one tick at a time and puts the guard rails between Jev's
choice (predict) and its execution (act): host allowlist, click blocklist, your own
typed values, step budget and independent verification.

Exit codes: 0 verified, 2 refused to start, 4 unverified, 5 left the allowlist.

Needs a jev-ultrafast checkout (JEV_ULTRAFAST_REPO) synced with `uv sync`; the script
re-executes itself with that checkout's interpreter.
"""
import argparse, json, os, re, shutil, subprocess, sys, tempfile, time, urllib.request
from pathlib import Path
from urllib.parse import urlparse

HEADINGS_JS = (
    "JSON.stringify({title: document.title, url: location.href,"
    " headings: [...document.querySelectorAll('h1,h2')].map(h => h.innerText)})"
)
FIELDS_JS = r"""
JSON.stringify([...document.querySelectorAll('input,textarea,select')].map(el => ({
  label: (el.getAttribute('aria-label') || el.labels?.[0]?.innerText || el.placeholder || el.name || '').trim(),
  value: el.value})))
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


def launch_chrome(path):
    """Headless Chrome on a throwaway profile. Returns (process, profile dir, browser ws URL)."""
    prof = tempfile.mkdtemp(prefix="jev-chrome-")
    proc = subprocess.Popen([path, "--headless=new", "--remote-debugging-port=0", f"--user-data-dir={prof}",
                             "--no-first-run", "--no-default-browser-check", "--no-sandbox", "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    port_file = Path(prof) / "DevToolsActivePort"
    for _ in range(100):
        if port_file.exists() and port_file.read_text().strip():
            port = port_file.read_text().split()[0]
            info = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=10))
            return proc, prof, info["webSocketDebuggerUrl"]
        time.sleep(0.1)
    proc.kill()
    shutil.rmtree(prof, ignore_errors=True)
    raise Stop(2, error="chrome did not start")


def load_key():
    """Put TYPESAFE_API_KEY in the environment from .env or the secrets file if it is not there."""
    if os.environ.get("TYPESAFE_API_KEY"):
        return True
    for d in [Path.cwd(), *Path.cwd().parents]:
        env = d / ".env"
        if env.is_file():
            m = re.search(r"^TYPESAFE_API_KEY=(.+)$", env.read_text(), re.M)
            if m:
                os.environ["TYPESAFE_API_KEY"] = m.group(1).strip().strip("'\"")
                return True
    secret = Path.home() / ".pi/agent/secrets/typesafe_api_key"
    if secret.is_file():
        os.environ["TYPESAFE_API_KEY"] = secret.read_text().strip()
        return True
    return False


def pick_text(label, texts):
    """Exact label wins, else exactly one --text label contained in the field label."""
    low = label.lower()
    for k, v in texts:
        if k.lower() == low:
            return v
    hits = [(k, v) for k, v in texts if k.lower() in low]
    return hits[0][1] if len(hits) == 1 else None


def ensure_repo_python(repo):
    """Re-exec under the checkout's interpreter so jev_ultrafast and browser_harness import.

    Decided by interpreter location, never by importing, because browser_harness reads its
    environment (BU_NAME, BH_RUNTIME_DIR, BU_CDP_WS) at import time.
    """
    venv = (Path(repo) / ".venv").resolve()
    if Path(sys.prefix).resolve() == venv:
        return
    py = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not py.is_file():
        raise Stop(2, error=f"no interpreter at {py}; run `uv sync` in {repo}")
    os.execv(str(py), [str(py), *sys.argv])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", required=True)
    ap.add_argument("--goal", required=True)
    ap.add_argument("--allow-hosts", required=True, help="comma-separated; subdomains included")
    ap.add_argument("--expect", help="text that must appear in the live title, heading or URL")
    ap.add_argument("--expect-field", action="append", default=[], metavar="LABEL=VALUE")
    ap.add_argument("--text", action="append", default=[], metavar="LABEL=VALUE")
    ap.add_argument("--max-ticks", type=int, default=10)
    ap.add_argument("--never-click", default=r"send|submit|pay|buy|purchase|place order|checkout|delete|remove|publish|subscribe")
    ap.add_argument("--cdp", default=os.environ.get("BU_CDP_WS"))
    ap.add_argument("--no-launch-chrome", action="store_true")
    ap.add_argument("--chrome-path")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    allowed = [h.strip().lower() for h in a.allow_hosts.split(",") if h.strip()]
    texts = [tuple(t.split("=", 1)) for t in a.text if "=" in t]
    fields = [tuple(t.split("=", 1)) for t in a.expect_field if "=" in t]
    blocker = re.compile(a.never_click, re.I) if a.never_click else None
    result = {"ok": False, "ticks": 0, "actions": []}
    proc = prof = runtime = agent = None
    code = 4
    try:
        repo = os.environ.get("JEV_ULTRAFAST_REPO")
        if not repo or not Path(repo).is_dir():
            raise Stop(2, error="JEV_ULTRAFAST_REPO must name a jev-ultrafast checkout")
        if not allowed:
            raise Stop(2, error="--allow-hosts is required")
        if not host_ok(a.url, allowed):
            raise Stop(2, error="start URL is outside --allow-hosts")
        if not load_key():
            raise Stop(2, error="no TYPESAFE_API_KEY (env, .env, ~/.pi/agent/secrets)")
        ensure_repo_python(repo)
        if a.cdp:
            ws_url, result["browser"] = a.cdp, "attached"
        else:
            if a.no_launch_chrome:
                raise Stop(2, error="--no-launch-chrome set but no --cdp / BU_CDP_WS given")
            chrome = find_chrome(a.chrome_path)
            if not chrome:
                raise Stop(2, error="no Chrome binary found; use --chrome-path")
            proc, prof, ws_url = launch_chrome(chrome)
            result["browser"] = "owned"
        runtime = tempfile.mkdtemp(prefix="jev-bh-")
        os.environ.update(BU_CDP_WS=ws_url, BH_RUNTIME_DIR=runtime, BH_TMP_DIR=runtime,
                          BU_NAME=f"jev{os.getpid()}")

        from jev_ultrafast import Agent
        from jev_ultrafast.browser import StalePage
        from jev_ultrafast.model import field_context

        agent = Agent(a.url, a.goal)
        state = agent.state
        for tick in range(1, a.max_ticks + 1):
            result["ticks"] = tick
            if not host_ok(state["page"]["url"], allowed):
                raise Stop(5, left_allowlist=state["page"]["url"])
            try:
                agent.command("predict")
                page, choice = state["page"], state["decision"]["choice"]
                if choice in {"DONE", "BLOCKED"}:
                    agent.command("act", {"fingerprint": page["fingerprint"]})
                    result["jev"] = choice.lower()
                    break
                action = next(x for x in page["actions"] if x["id"] == choice)
                label = action["label"].split(" → ")[0]
                if action["kind"] in {"click", "select"} and blocker and blocker.search(action["label"]):
                    raise Stop(4, blocked_click=action["label"])
                if action["kind"] == "fill":
                    value = pick_text(label, texts)
                    if value is None:
                        raise Stop(4, needs_text=label)
                    # Jev's own text helper is bypassed: the cached value matches its input exactly.
                    ctx = field_context(state["goal"], action, page, state["history"])
                    agent.pending_text = (ctx, value, {"model": "caller", "latency_ms": 0})
                agent.command("act", {"fingerprint": page["fingerprint"]})
                result["actions"].append({"kind": action["kind"], "label": label})
            except StalePage:
                state["decision"], state["status"] = None, "ready"
                state["page"] = agent.browser.observe(screenshot=False)
            if state["status"] in {"done", "blocked"}:
                break

        live = json.loads(agent.browser.evaluate(HEADINGS_JS))
        if not host_ok(live["url"], allowed):
            raise Stop(5, left_allowlist=live["url"])
        ok = True
        if a.expect:
            hay = " ".join([live["title"], live["url"], *live["headings"]]).lower()
            result["expect_found"] = ok = a.expect.lower() in hay
        if fields:
            vals = [(f["label"], f["value"]) for f in json.loads(agent.browser.evaluate(FIELDS_JS))]
            result["fields"] = {}
            for want_label, want in fields:
                got = next((v for lb, v in vals if lb.lower() == want_label.lower()), None) or \
                      next((v for lb, v in vals if want_label.lower() in lb.lower()), None)
                result["fields"][want_label] = {"want": want, "got": got}
                ok = ok and got == want
        if not a.expect and not fields:
            ok, result["note"] = False, "nothing to verify: pass --expect or --expect-field"
        result["ok"], code = ok, (0 if ok else 4)
    except Stop as s:
        result.update(s.info)
        code = s.code
    except Exception as e:  # an error after a mutation is never retried
        result["error"], code = f"{type(e).__name__}: {e}", 4
    finally:
        if agent:
            try:
                agent.close()
            except Exception:
                pass
        if "BU_CDP_WS" in os.environ and runtime:  # stop the daemon this run started
            try:
                from browser_harness.admin import restart_daemon
                restart_daemon()
            except Exception:
                pass
        if proc:
            proc.terminate()
        for d in (prof, runtime):
            if d:
                shutil.rmtree(d, ignore_errors=True)
    print(json.dumps(result, indent=None if a.json else 2))
    sys.exit(code)


if __name__ == "__main__":
    main()
