"""Headless screenshot sweep of the web console (for docs/README).

Visits every page of a running instance with the JAYNET_WEB_TOKEN admin
bearer — login, chat, all admin tabs, all account tabs — and saves PNGs
into screenshots/. Before each shot, sensitive content is blurred:
usernames, chat/project titles, message and run text. UI labels and
titles stay untouched.

Usage (live venv, from the dev checkout):
  /srv/jaynet-orchestrator/.venv/bin/python scripts/screenshot_pages.py
  /srv/jaynet-orchestrator/.venv/bin/python scripts/screenshot_pages.py --base http://127.0.0.1:8071 --out screenshots

Uses the SYSTEM chromium — Playwright's bundled build does not run on
Arch (same rule as tools/browser/session.py).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ENV_FILE = os.path.expanduser("~/.config/jaynet.env")
if not os.path.exists(ENV_FILE):
    ENV_FILE = os.path.expanduser("~/.config/orchestrator.env")  # legacy name
VIEWPORT = {"width": 1480, "height": 940}

# Blurred before the shot. Selectors are per page/tab; missing elements
# are fine. Keep this list in sync when the GUI grows new data views.
REDACT_CSS = ".jnredact{filter:blur(7px)!important;opacity:.85;user-select:none!important}"
CHAT_REDACT = ["#who", "#mmWho", "#chatList .ttl", "#chatList .pbadge",
               "#projSelect", "#log",
               # ToDos side panel: item titles/descs/notes are model content
               "#todoPanel .ttitle", "#todoPanel .tdesc", "#todoPanel .tnote"]
ADMIN_REDACT = {
    "status": ["#logs"],
    "processes": [".proc-log pre"],
    "presets": [],
    "prompt": [],
    "config": [],
    "tools": [],
    "access": ["#users td:first-child", "#usageRows td:first-child"],
    "flags": ["#flagRows td:nth-child(2)", "#flagRows td:nth-child(3)",
              "#flagRows td:nth-child(4)", "#flagDetail",
              "#reportRows td:nth-child(2)", "#reportRows td:nth-child(3)",
              "#reportRows td:nth-child(5)"],
    "rag": ["#ragRows td:nth-child(1)", "#ragRows td:nth-child(2)"],
    "studio": [],
    # Eval tab: case ids are public seeds, but the results table's judge
    # notes are model-generated free text — blur them.
    "eval": ["#evResultRows td:nth-child(5)"],
    "backup": [],
}

# The Eval tab has sub-views behind #evSub (Cases | Statistics | Proposals).
# The main loop shot already captures Cases; these get their own PNGs.
# (sub-view button, output file, selectors to redact)
EVAL_SUBVIEWS = [
    ("stats", "admin-eval-stats.png", []),
    ("proposals", "admin-eval-proposals.png", ["#evPropRows td:nth-child(3)"]),
    ("benchmark", "admin-eval-benchmark.png", []),
]
ACCOUNT_REDACT = {
    "usage": ["#who", "#runs tr td:nth-child(6)"],
    "settings": ["#who"],
    "security": ["#who"],
}


def read_env(path: str) -> dict:
    out = {}
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def redact(page, selectors: list) -> int:
    page.add_style_tag(content=REDACT_CSS)
    return page.evaluate(
        """sels => { let n = 0;
           for (const s of sels)
             for (const el of document.querySelectorAll(s))
               { el.classList.add("jnredact"); n++; }
           return n; }""", selectors)


def settle(page, ms: int = 800):
    try:
        page.wait_for_load_state("networkidle", timeout=6000)
    except Exception:
        pass   # polling tabs (process logs) never go fully idle
    page.wait_for_timeout(ms)


def shot(page, out: Path, name: str, full: bool, selectors: list):
    n = redact(page, selectors)
    page.screenshot(path=str(out / name), full_page=full)
    print(f"  {name:28s} redacted {n} element(s)")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--env-file", default=ENV_FILE)
    ap.add_argument("--base", default=None,
                    help="base URL (default: http://127.0.0.1:$JAYNET_WEB_PORT)")
    ap.add_argument("--out", default=str(Path(__file__).parent.parent / "screenshots"))
    args = ap.parse_args()

    env = read_env(args.env_file) if Path(args.env_file).exists() else {}
    token = env.get("JAYNET_WEB_TOKEN") or env.get("ORCH_WEB_TOKEN", "")
    if not token:
        sys.exit(f"JAYNET_WEB_TOKEN not found in {args.env_file}")
    base = args.base or f"http://127.0.0.1:{env.get('JAYNET_WEB_PORT') or env.get('ORCH_WEB_PORT', '8071')}"
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit("playwright missing from this venv")
    import shutil
    chromium = shutil.which("chromium") or shutil.which("chromium-browser")
    if not chromium:
        sys.exit("no system chromium found (bundled build does not run on Arch)")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, executable_path=chromium)

        # login page: anonymous context (a valid bearer would redirect home)
        ctx = browser.new_context(viewport=VIEWPORT)
        page = ctx.new_page()
        print(f"GET {base}/login")
        page.goto(base + "/login", wait_until="domcontentloaded")
        settle(page)
        shot(page, out, "login.png", full=False, selectors=[])
        ctx.close()

        # everything else rides the admin bearer
        ctx = browser.new_context(viewport=VIEWPORT,
                                  extra_http_headers={"Authorization": f"Bearer {token}"})
        page = ctx.new_page()

        print(f"GET {base}/")
        page.goto(base + "/", wait_until="domcontentloaded")
        page.wait_for_selector("#chatList .chatItem, #chatList .empty", timeout=10000)
        # open the most recent chat so the shot shows a real conversation
        # (its content is blurred below — structure is the point)
        if page.query_selector("#chatList .chatItem"):
            page.click("#chatList .chatItem")
            page.wait_for_selector("#log .msg", timeout=10000)
        settle(page, 1200)
        shot(page, out, "chat.png", full=False, selectors=CHAT_REDACT)

        print(f"GET {base}/admin")
        page.goto(base + "/admin", wait_until="domcontentloaded")
        settle(page, 1000)
        for tab in ADMIN_REDACT:
            page.click(f'.tab[data-tab="{tab}"]')
            settle(page)
            if tab == "status":
                # 40 blurred run rows make the page absurdly tall; six read fine
                page.evaluate("""() => { const l = document.querySelector("#logs");
                     if (l) [...l.children].slice(6).forEach(el => el.remove()); }""")
            shot(page, out, f"admin-{tab}.png", full=True,
                 selectors=ADMIN_REDACT[tab])
            if tab == "eval":
                for sub, name, sel in EVAL_SUBVIEWS:
                    page.click(f'#evSub button[data-evsub="{sub}"]')
                    settle(page)
                    shot(page, out, name, full=True, selectors=sel)

        print(f"GET {base}/account")
        page.goto(base + "/account", wait_until="domcontentloaded")
        settle(page, 1000)
        for tab in ACCOUNT_REDACT:
            page.click(f'.tab[data-tab="{tab}"]')
            settle(page)
            shot(page, out, f"account-{tab}.png", full=True,
                 selectors=ACCOUNT_REDACT[tab])

        browser.close()

    # README hero: a tight crop of the (manually shot) chat-run.png — prompt,
    # tool calls, answer, token footer; the blurred chat.png is not hero material.
    src = out / "chat-run.png"
    if src.exists():
        try:
            from PIL import Image
            img = Image.open(src)
            img.crop((0, 0, img.width, min(810, img.height))).save(out / "chat-hero.png")
        except ImportError:
            print("note: PIL not installed — skipping chat-hero.png crop")
    print(f"done — {len(list(out.glob('*.png')))} PNGs in {out}")


if __name__ == "__main__":
    main()
