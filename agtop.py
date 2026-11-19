#!/usr/bin/env python3
"""agtop: htop-like view of AI coding agent quota (5h + weekly) for Claude Code, Antigravity, Codex.

Safety: never prints/logs tokens, never refreshes OAuth tokens, only calls each agent's own CLI
(0 turns) except the documented Claude fallback endpoint.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import select
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from rich import box
from rich.console import Console, Group
from rich.padding import Padding
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

HOME = Path.home()
CACHE_DIR = HOME / ".cache" / "agtop"
REFRESH_SECS = 60
AGY_CACHE_SECS = 120
CODEX_CACHE_SECS = 180
AGENTS = ["Claude Code", "Antigravity", "Codex"]


# ---------------------------------------------------------------- model
@dataclass
class Window:
    label: str
    percent_used: float
    resets_at: datetime | None = None
    group: str = ""


@dataclass
class Result:
    agent: str
    state: str  # ok | warn | missing | error
    windows: list[Window] = field(default_factory=list)
    note: str = ""
    source: str = ""
    fetched_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        now = datetime.now(timezone.utc)
        return {
            "agent": self.agent,
            "state": self.state,
            "note": self.note,
            "source": self.source,
            "fetched_at": datetime.fromtimestamp(self.fetched_at, timezone.utc).isoformat(),
            "windows": [
                {
                    "group": w.group,
                    "label": w.label,
                    "percent_used": round(w.percent_used, 1),
                    "resets_at": w.resets_at.isoformat() if w.resets_at else None,
                    "resets_in_seconds": int((w.resets_at - now).total_seconds()) if w.resets_at else None,
                }
                for w in self.windows
            ],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Result":
        return cls(
            agent=d["agent"], state=d["state"], note=d.get("note", ""), source=d.get("source", ""),
            fetched_at=datetime.fromisoformat(d["fetched_at"]).timestamp(),
            windows=[Window(w["label"], w["percent_used"],
                            datetime.fromisoformat(w["resets_at"]) if w.get("resets_at") else None,
                            w.get("group", ""))
                     for w in d.get("windows", [])],
        )


# ---------------------------------------------------------------- helpers
def run_cli(cmd: list[str], timeout: float, cwd: Path | str = HOME) -> str:
    """Run a CLI non-interactively; never attach to our terminal."""
    p = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                       timeout=timeout, cwd=str(cwd), start_new_session=True)
    return p.stdout


def first_json(text: str) -> dict:
    i = text.find("{")
    if i < 0:
        raise ValueError("CLI returned no JSON")
    return json.JSONDecoder().raw_decode(text[i:])[0]


def short(msg: object, n: int = 70) -> str:
    s = " ".join(str(msg).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def cached(name: str, ttl: float, fn) -> Result:
    """File cache shared across processes (so `agtop --once` repeated doesn't spam agent logs)."""
    path = CACHE_DIR / f"{name}.json"
    if ttl > 0:
        try:
            r = Result.from_dict(json.loads(path.read_text()))
            if time.time() - r.fetched_at < ttl and r.state in ("ok", "warn"):
                return r
        except Exception:
            pass
    r = fn()
    if r.state in ("ok", "warn"):
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(r.to_dict()))
            tmp.replace(path)
        except Exception:
            pass
    return r


def is_wsl() -> bool:
    try:
        return "microsoft" in Path("/proc/version").read_text().lower()
    except Exception:
        return False


# ---------------------------------------------------------------- Claude Code
CLAUDE_RE = re.compile(r"^(Current [^:]+): (\d+)% used(?: · resets (.+?) \((.+?)\))?", re.M)


def _claude_label(raw: str) -> str:
    if raw == "Current session":
        return "5-hour"
    m = re.match(r"Current week \((.+)\)", raw)
    if m:
        return "Weekly" if m.group(1).lower() == "all models" else f"Weekly · {m.group(1)}"
    return raw.replace("Current ", "").capitalize()


def parse_claude_reset(s: str, tzname: str, now: datetime | None = None) -> datetime | None:
    tz = ZoneInfo(tzname)
    now = now or datetime.now(tz)
    s = s.strip().replace(" at ", ", ")
    for fmt in ("%b %d, %I:%M%p", "%b %d, %I%p", "%b %d %I:%M%p", "%b %d %I%p", "%b %d"):
        try:
            base = datetime.strptime(s, fmt)
        except ValueError:
            continue
        cands = [base.replace(year=y, tzinfo=tz) for y in (now.year - 1, now.year, now.year + 1)]
        return min(cands, key=lambda c: abs(c - now))  # resets are always within ~a week of now
    for fmt in ("%I:%M%p", "%I%p"):
        try:
            t = datetime.strptime(s, fmt)
        except ValueError:
            continue
        d = now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
        return d if d >= now - timedelta(minutes=1) else d + timedelta(days=1)
    return None


def parse_claude_usage(text: str) -> list[Window]:
    out = []
    for m in CLAUDE_RE.finditer(text):
        raw, pct, reset, tzname = m.groups()
        when = None
        if reset and tzname:
            try:
                when = parse_claude_reset(reset, tzname)
            except Exception:
                when = None
        out.append(Window(_claude_label(raw), float(pct), when))
    return out


def claude_oauth_fallback(reason: str) -> Result:
    """Documented fallback: read local token (never printed), call Anthropic's usage endpoint. No refresh."""
    import httpx

    cred_path = HOME / ".claude" / ".credentials.json"
    if not cred_path.exists():
        return Result("Claude Code", "missing", note="not logged in · run `claude` to sign in")
    try:
        oauth = json.loads(cred_path.read_text()).get("claudeAiOauth") or {}
    except Exception:
        return Result("Claude Code", "error", note="cannot read credentials")
    token = oauth.get("accessToken")
    if not token:
        return Result("Claude Code", "missing", note="not logged in · run `claude` to sign in")
    exp = oauth.get("expiresAt")
    if exp and exp / 1000 < time.time():
        return Result("Claude Code", "warn", note="token expired · open `claude` once to refresh it")
    try:
        r = httpx.get("https://api.anthropic.com/api/oauth/usage", timeout=15,
                      headers={"Authorization": f"Bearer {token}", "anthropic-beta": "oauth-2025-04-20"})
    finally:
        token = None
    if r.status_code == 401:
        return Result("Claude Code", "warn", note="token expired · open `claude` once to refresh it")
    if r.status_code != 200:
        return Result("Claude Code", "error", note=f"usage API returned HTTP {r.status_code}")
    data = r.json()
    names = {"five_hour": "5-hour", "seven_day": "Weekly", "seven_day_opus": "Weekly · Opus",
             "seven_day_sonnet": "Weekly · Sonnet"}
    wins = []
    for key, val in data.items():
        if not isinstance(val, dict) or val.get("utilization") is None:
            continue
        ra = val.get("resets_at")
        wins.append(Window(names.get(key) or key.replace("_", " "), float(val["utilization"]),
                           datetime.fromisoformat(ra.replace("Z", "+00:00")) if ra else None))
    order = list(names.values())
    wins.sort(key=lambda w: order.index(w.label) if w.label in order else 99)
    if not wins:
        return Result("Claude Code", "error", note="usage API returned no data")
    return Result("Claude Code", "warn", wins, note=f"via usage API fallback ({short(reason, 40)})",
                  source="oauth usage API")


def provider_claude() -> Result:
    exe = shutil.which("claude")
    if not exe:
        return Result("Claude Code", "missing", note="not installed · `claude` not found in PATH")
    if os.environ.get("AGTOP_CLAUDE_FALLBACK") == "1":
        return claude_oauth_fallback("forced by AGTOP_CLAUDE_FALLBACK")
    try:
        d = first_json(run_cli([exe, "-p", "/usage", "--output-format", "json"], timeout=45))
    except subprocess.TimeoutExpired:
        return claude_oauth_fallback("CLI timed out")
    except Exception as e:
        return claude_oauth_fallback(f"CLI error: {e}")
    wins = parse_claude_usage(str(d.get("result") or ""))
    if not wins:
        return claude_oauth_fallback("could not parse /usage")
    if d.get("num_turns") not in (0, None) or (d.get("total_cost_usd") or 0) != 0:
        return Result("Claude Code", "warn", wins, note="WARNING: /usage consumed a turn / cost",
                      source="claude /usage")
    return Result("Claude Code", "ok", wins, source="claude /usage")


# ---------------------------------------------------------------- Antigravity
def _agy_fetch() -> Result:
    exe = shutil.which("agy")
    if not exe:
        return Result("Antigravity", "missing", note="not installed · `agy` not found in PATH")
    try:
        d = first_json(run_cli([exe, "-p", "/usage", "--output-format", "json"], timeout=45))
    except subprocess.TimeoutExpired:
        return Result("Antigravity", "error", note="`agy /usage` timed out")
    except Exception as e:
        if not (HOME / ".gemini" / "antigravity-cli" / "antigravity-oauth-token").exists():
            return Result("Antigravity", "missing", note="not logged in · run `agy` to sign in")
        return Result("Antigravity", "error", note=short(f"agy error: {e}"))
    groups = ((d.get("command") or {}).get("data") or {}).get("groups")
    if not groups:
        msg = d.get("response") or d.get("status") or "no data"
        if re.search(r"log ?in|auth|sign", str(msg), re.I):
            return Result("Antigravity", "missing", note="not logged in · run `agy` to sign in")
        return Result("Antigravity", "error", note=short(f"agy: {msg}"))
    wins = []
    for g in groups:
        bks = sorted(g.get("buckets") or [], key=lambda b: 0 if b.get("window") == "5h" else 1)
        for b in bks:
            rem = b.get("remaining_fraction")
            if rem is None:
                continue
            w = {"5h": "5-hour", "weekly": "Weekly"}.get(b.get("window"), b.get("window") or "?")
            rt = b.get("reset_time")
            wins.append(Window(w, max(0.0, (1 - float(rem)) * 100),
                               datetime.fromisoformat(rt.replace("Z", "+00:00")) if rt else None,
                               g.get("name", "?")))
    usage = d.get("usage") or {}
    if d.get("num_turns") not in (0, None) or (usage.get("total_tokens") or 0) != 0:
        return Result("Antigravity", "warn", wins, note="WARNING: /usage consumed a turn / tokens",
                      source="agy /usage")
    return Result("Antigravity", "ok", wins, source="agy /usage")


def provider_agy(force: bool = False) -> Result:
    # agy writes a log file per call -> always respect the cache, even on manual refresh
    return cached("agy", AGY_CACHE_SECS, _agy_fetch)


# ---------------------------------------------------------------- Codex
def codex_locate() -> tuple[list[str] | None, str, list[Path]]:
    """Return (cmd, cwd, codex_home candidates)."""
    homes = []
    if os.environ.get("CODEX_HOME"):
        homes.append(Path(os.environ["CODEX_HOME"]))
    homes.append(HOME / ".codex")
    exe = shutil.which("codex")
    if exe:
        return [exe], str(HOME), homes
    if is_wsl():  # Windows-side install; run from /mnt/c to avoid cmd.exe UNC-path errors
        exes = sorted(glob.glob("/mnt/c/Users/*/AppData/Local/OpenAI/Codex/bin/*/codex.exe"), key=os.path.getmtime)
        homes += [Path(p) for p in glob.glob("/mnt/c/Users/*/.codex")]
        if exes:
            return [exes[-1]], "/mnt/c", homes
    return None, str(HOME), homes


def _codex_window_label(mins: int | None, fallback: str) -> str:
    if mins == 300:
        return "5-hour"
    if mins == 10080:
        return "Weekly"
    if mins:
        return f"{mins // 1440}-day" if mins % 1440 == 0 else f"{mins / 60:g}-hour"
    return fallback


def codex_app_server(cmd: list[str], cwd: str, timeout: float = 60) -> dict:
    """JSON-RPC over stdio: initialize -> account/rateLimits/read (read-only, no turn)."""
    p = subprocess.Popen(cmd + ["app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, text=True, cwd=cwd, start_new_session=True)
    try:
        def send(o):
            p.stdin.write(json.dumps(o) + "\n")
            p.stdin.flush()

        send({"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "agtop", "version": "0.2"}}})
        send({"method": "initialized"})
        send({"id": 2, "method": "account/rateLimits/read", "params": {"excludeResetCreditDetails": True}})
        deadline = time.time() + timeout
        while time.time() < deadline:
            r, _, _ = select.select([p.stdout], [], [], 0.5)
            if not r:
                if p.poll() is not None:
                    raise RuntimeError("app-server exited early")
                continue
            line = p.stdout.readline()
            if not line:
                raise RuntimeError("app-server closed stdout")
            try:
                m = json.loads(line)
            except ValueError:
                continue
            if m.get("id") == 2:
                if m.get("error"):
                    raise RuntimeError((m["error"] or {}).get("message", "error"))
                return m.get("result") or {}
            if m.get("id") == 1 and m.get("error"):
                raise RuntimeError((m["error"] or {}).get("message", "initialize failed"))
        raise TimeoutError("app-server timed out")
    finally:
        try:
            p.stdin.close()
        except Exception:
            pass
        p.terminate()
        try:
            p.wait(3)
        except subprocess.TimeoutExpired:
            p.kill()


def _find_rate_limits(o):
    if isinstance(o, dict):
        if isinstance(o.get("rate_limits"), dict):
            return o["rate_limits"]
        for v in o.values():
            r = _find_rate_limits(v)
            if r:
                return r
    return None


def codex_from_logs(homes: list[Path]) -> tuple[list[Window], datetime] | None:
    files = []
    for h in homes:
        files += glob.glob(str(h / "sessions" / "**" / "rollout-*.jsonl"), recursive=True)
    files.sort(key=os.path.getmtime, reverse=True)
    for f in files[:10]:
        try:
            with open(f, "rb") as fh:
                fh.seek(0, 2)
                size = fh.tell()
                fh.seek(max(0, size - 4_000_000))
                lines = fh.read().decode("utf-8", "replace").splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            if '"rate_limits"' not in line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            rl = _find_rate_limits(ev)
            if not rl:
                continue
            ts_s = ev.get("timestamp")
            ts = datetime.fromisoformat(ts_s.replace("Z", "+00:00")) if ts_s else \
                datetime.fromtimestamp(os.path.getmtime(f), timezone.utc)
            now = datetime.now(timezone.utc)
            wins = []
            for key, fb in (("primary", "5-hour"), ("secondary", "Weekly")):
                w = rl.get(key)
                if not isinstance(w, dict) or w.get("used_percent") is None:
                    continue
                if w.get("resets_at"):
                    ra = datetime.fromtimestamp(w["resets_at"], timezone.utc)
                elif w.get("resets_in_seconds") is not None:
                    ra = ts + timedelta(seconds=w["resets_in_seconds"])
                else:
                    ra = None
                pct = float(w["used_percent"])
                if ra and ra < now:  # window rolled over since last use -> nothing used yet
                    pct, ra = 0.0, None
                wins.append(Window(_codex_window_label(w.get("window_minutes"), fb), pct, ra))
            if wins:
                return wins, ts
    return None


def _codex_fetch() -> Result:
    cmd, cwd, homes = codex_locate()
    if not cmd:
        return Result("Codex", "missing", note="not installed · `codex` not found")
    try:
        res = codex_app_server(cmd, cwd)
        snaps = res.get("rateLimitsByLimitId") or {"codex": res.get("rateLimits") or {}}
        wins = []
        for lid, snap in snaps.items():
            if not isinstance(snap, dict):
                continue
            group = "" if lid == "codex" and len(snaps) == 1 else (snap.get("limitName") or lid)
            for key, fb in (("primary", "5-hour"), ("secondary", "Weekly")):
                w = snap.get(key)
                if isinstance(w, dict) and w.get("usedPercent") is not None:
                    ra = datetime.fromtimestamp(w["resetsAt"], timezone.utc) if w.get("resetsAt") else None
                    wins.append(Window(_codex_window_label(w.get("windowDurationMins"), fb),
                                       float(w["usedPercent"]), ra, group))
        if wins:
            plan = (res.get("rateLimits") or {}).get("planType")
            if res.get("ordinaryUsageAllowed") is False:
                return Result("Codex", "warn", wins, note="usage limit reached", source="codex app-server")
            return Result("Codex", "ok", wins, note=f"{plan.upper()}" if plan else "", source="codex app-server")
        reason = "app-server returned no rate limits"
    except Exception as e:
        reason = short(e, 40)
    not_logged = bool(re.search(r"log ?in|auth|unauthori|sign", reason, re.I))
    fb = codex_from_logs(homes)
    if fb:
        wins, ts = fb
        when = ts.astimezone().strftime("%d %b %H:%M")
        return Result("Codex", "warn", wins, note=f"from session log, as of last use {when} ({reason})",
                      source="rollout log")
    if not_logged:
        return Result("Codex", "missing", note="not logged in · run `codex login`")
    return Result("Codex", "error", note=f"unavailable: {reason}")


def provider_codex(force: bool = False) -> Result:
    return cached("codex", 0 if force else CODEX_CACHE_SECS, _codex_fetch)


# ---------------------------------------------------------------- orchestration
def _safe(name, fn, *a):
    try:
        return fn(*a)
    except Exception as e:  # one provider must never crash the app
        return Result(name, "error", note=short(f"{type(e).__name__}: {e}"))


def collect(force: bool = False, on_result=None) -> list[Result]:
    jobs = {"Claude Code": (provider_claude,), "Antigravity": (provider_agy, force),
            "Codex": (provider_codex, force)}
    out = {}
    with ThreadPoolExecutor(max_workers=len(jobs)) as ex:
        futs = {ex.submit(_safe, n, *j): n for n, j in jobs.items()}
        for f in as_completed(futs):
            r = f.result()
            out[futs[f]] = r
            if on_result:
                on_result(r)
    return [out[n] for n in AGENTS]


# ---------------------------------------------------------------- Tokyo Night look
TN = {
    "bg": "#1a1b26", "bg_dark": "#16161e", "surface": "#24283b", "track": "#2f3549", "border": "#3b4261",
    "comment": "#565f89", "muted": "#737aa2", "fg_dim": "#a9b1d6", "fg": "#c0caf5",
    "blue": "#7aa2f7", "cyan": "#7dcfff", "teal": "#73daca", "purple": "#bb9af7",
    "green": "#9ece6a", "yellow": "#e0af68", "orange": "#ff9e64", "red": "#f7768e",
}
BRAND = {"Claude Code": TN["orange"], "Antigravity": TN["blue"], "Codex": TN["teal"]}
ICON = {"Claude Code": "✻", "Antigravity": "◆", "Codex": "◎"}
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
BAR_W = 32  # default; actual width adapts to the card
LABEL_W = 22
# usage gradient along the bar: calm green -> amber at 70-90% -> red at the end
STOPS = [(0.0, TN["green"]), (0.62, TN["green"]), (0.80, TN["yellow"]), (0.95, TN["red"]), (1.0, TN["red"])]


def _rgb(h: str) -> tuple[int, int, int]:
    return int(h[1:3], 16), int(h[3:5], 16), int(h[5:7], 16)


def color_at(x: float) -> str:
    x = max(0.0, min(1.0, x))
    for (x0, c0), (x1, c1) in zip(STOPS, STOPS[1:]):
        if x <= x1:
            t = 0 if x1 == x0 else (x - x0) / (x1 - x0)
            a, b = _rgb(c0), _rgb(c1)
            return "#%02x%02x%02x" % tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))
    return STOPS[-1][1]


def level_color(p: float) -> str:
    return TN["green"] if p < 70 else TN["yellow"] if p < 90 else TN["red"]


def bar(p: float, width: int = BAR_W) -> Text:
    cells = max(0.0, min(100.0, p)) / 100 * width
    full = int(cells)
    half = cells - full >= 0.5 or (p > 0 and full == 0)
    t = Text()
    for i in range(full):
        t.append("━", style=color_at((i + 0.5) / width))
    used = full
    if half and full < width:
        t.append("╸", style=color_at((full + 0.5) / width))
        used += 1
    if used < width:
        t.append("─" * (width - used), style=TN["border"])
    return t


def fmt_left(dt: datetime | None, now: datetime, with_date: bool = True) -> Text:
    if not dt:
        return Text("—", style=TN["comment"])
    s = int((dt - now).total_seconds())
    if s <= 0:
        return Text("↻ resetting", style=TN["comment"])
    d, rem = divmod(s, 86400)
    h, rem = divmod(rem, 3600)
    m, sec = divmod(rem, 60)
    left = f"{d}d {h:02d}h" if d else f"{h}h {m:02d}m" if h else f"{m}m {sec:02d}s"
    t = Text("↻ ", style=TN["comment"])
    t.append(f"{left:>7}", style=TN["cyan"] if s < 3600 else TN["fg"])
    if with_date:
        t.append(f"  {dt.astimezone().strftime('%a %d %b %H:%M')}", style=TN["comment"])
    return t


def fmt_age(ts: float) -> str:
    a = int(time.time() - ts)
    return "just now" if a < 5 else f"{a}s ago" if a < 120 else f"{a // 60}m ago"


def layout(width: int) -> tuple[int, int, bool]:
    """(label_w, bar_w, show_date) for a card whose content area is `width` columns."""
    label_w = LABEL_W if width >= 90 else 16
    fixed = label_w + 4 + 6  # label + pct + 3 gaps of 2
    show_date = width - fixed - 27 >= 16
    bar_w = max(8, min(40, width - fixed - (27 if show_date else 9)))
    return label_w, bar_w, show_date


def agent_body(r: Result | None, now: datetime, loading: bool = False, frame: int = 0, width: int = 94):
    """Inner content of an agent card (shared by the TUI and --once). `width` = content columns."""
    if r is None:
        return Text(f"{SPINNER[frame % len(SPINNER)]} fetching usage…", style=f"italic {TN['muted']}")
    if not r.windows:
        icon, color = ("✕", TN["red"]) if r.state == "error" else ("○", TN["muted"])
        return Text.assemble((f"{icon} ", color), (r.note or r.state, color))
    label_w, bar_w, show_date = layout(width)

    def grid() -> Table:
        g = Table.grid(padding=(0, 2))
        g.add_column(width=label_w, no_wrap=True, overflow="ellipsis")
        g.add_column(width=bar_w, no_wrap=True)
        g.add_column(width=4, justify="right", no_wrap=True)
        g.add_column(no_wrap=True)
        return g

    parts: list = []
    g = None
    for i, w in enumerate(r.windows):
        if g is None or (w.group and w.group != r.windows[i - 1].group):
            if w.group:  # group heading on its own full-width line, never truncated by the label column
                parts.append(Text(w.group, style=f"bold {TN['purple']}", no_wrap=True, overflow="ellipsis"))
            g = grid()
            parts.append(g)
        if w.group:
            last = i == len(r.windows) - 1 or r.windows[i + 1].group != w.group
            label = Text.assemble(("└─ " if last else "├─ ", TN["border"]), (w.label, TN["fg_dim"]))
        else:
            label = Text(w.label, style=TN["fg_dim"])
        g.add_row(label, bar(w.percent_used, bar_w),
                  Text(f"{w.percent_used:.0f}%", style=f"bold {color_at(w.percent_used / 100)}"),
                  fmt_left(w.resets_at, now, show_date))
    if r.state == "warn" and r.note:
        parts.append(Text.assemble(("▲ ", TN["yellow"]), (r.note, TN["yellow"])))
    return Group(*parts)


def card_title(name: str, r: Result | None) -> Text:
    t = Text.assemble((f" {ICON[name]} ", f"bold {BRAND[name]}"), (name, f"bold {TN['fg']}"))
    if r and r.state == "ok" and r.note:
        t.append("  ")
        t.append(f" {r.note} ", style=f"{TN['fg_dim']} on {TN['surface']}")
    t.append(" ")
    return t


def card_subtitle(r: Result | None, loading: bool, frame: int) -> Text:
    if loading:
        return Text(f" {SPINNER[frame % len(SPINNER)]} refreshing ", style=TN["cyan"])
    if r is None or not r.windows:
        return Text("")
    return Text.assemble((f" {r.source} ", TN["comment"]), ("· ", TN["border"]), (f"{fmt_age(r.fetched_at)} ", TN["muted"]))


def card_border(name: str, r: Result | None) -> str:
    if r is None or r.state == "ok":
        return BRAND[name]
    return {"warn": TN["yellow"], "error": TN["red"]}.get(r.state, TN["border"])


def legend() -> Text:
    t = Text()
    for label, p in (("< 70%", 30), ("70–90%", 80), ("≥ 90%", 97)):
        t.append("━━ ", style=level_color(p))
        t.append(f"{label}    ", style=TN["comment"])
    return t


def render_once(results: list[Result], width: int) -> Padding:
    """`width` is the full output width; content sits inside 1 row / 1 column of padding."""
    width -= 2
    now = datetime.now(timezone.utc)
    head = Text.assemble((" ◆ agtop ", f"bold {TN['bg']} on {TN['purple']}"),
                         ("  AI coding agent quota  ", TN["muted"]),
                         (datetime.now().strftime("%a %d %b %H:%M:%S"), TN["comment"]))
    parts: list = [head, Text("")]
    for name, r in zip(AGENTS, results):
        parts.append(Panel(agent_body(r, now, width=width - 4), box=box.ROUNDED, padding=(0, 1), width=width,
                           title=card_title(name, r), title_align="left",
                           subtitle=card_subtitle(r, False, 0), subtitle_align="right",
                           border_style=card_border(name, r)))
    parts.append(Text("  ") + legend())
    return Padding(Group(*parts), (1, 1))


# ---------------------------------------------------------------- TUI
def make_app():
    from textual.app import App, ComposeResult
    from textual.containers import VerticalScroll
    from textual.widgets import Footer, Static

    class Card(Static):
        def __init__(self, agent: str, **kw):
            super().__init__(**kw)
            self.agent = agent

        def show(self, r: Result | None, loading: bool, frame: int, now: datetime) -> None:
            self.border_title = card_title(self.agent, r)
            self.border_subtitle = card_subtitle(r, loading and r is not None, frame)
            self.styles.border = ("round", card_border(self.agent, r))
            w = self.content_size.width or 94
            self.update(agent_body(r, now, loading, frame, w))

    class TopBar(Static):
        pass

    class Agtop(App):
        TITLE = "agtop"
        ENABLE_COMMAND_PALETTE = False
        CSS = f"""
        Screen {{ background: {TN['bg']}; padding: 1 1; }}
        TopBar {{ dock: top; height: 1; background: {TN['bg_dark']}; }}
        #main {{ height: 1fr; align-horizontal: center; padding: 1 2 0 2; }}
        Card {{
            width: 1fr; max-width: 100; height: auto;
            padding: 0 2; margin: 0 0 1 0;
            background: {TN['bg']};
            border-title-align: left; border-subtitle-align: right;
        }}
        #legend {{ width: 1fr; max-width: 100; height: 1; padding: 0 1; }}
        Footer {{ background: {TN['bg_dark']}; }}
        """
        BINDINGS = [("r", "refresh_now", "Refresh"), ("q", "quit", "Quit")]

        def compose(self) -> ComposeResult:
            yield TopBar(id="top")
            with VerticalScroll(id="main"):
                for n in AGENTS:
                    yield Card(n, id=f"card-{AGENTS.index(n)}")
                yield Static(legend(), id="legend")
            yield Footer()

        def on_mount(self) -> None:
            self.theme = "tokyo-night"
            self.results: dict[str, Result] = {}
            self.loading: set[str] = set()
            self.last_full: float | None = None
            self.frame = 0
            self.set_interval(0.1, self.tick)
            self.set_interval(REFRESH_SECS, lambda: self.action_refresh_now(force=False))
            self.action_refresh_now(force=False)

        def tick(self) -> None:
            self.frame += 1
            if self.loading or self.frame % 10 == 0:
                self.redraw()

        def redraw(self) -> None:
            now = datetime.now(timezone.utc)
            for i, n in enumerate(AGENTS):
                self.query_one(f"#card-{i}", Card).show(self.results.get(n), n in self.loading, self.frame, now)
            left = Text.assemble((" ◆ agtop ", f"bold {TN['bg']} on {TN['purple']}"),
                                 ("  AI coding agent quota", TN["muted"]))
            right = Text()
            if self.loading:
                right.append(f"{SPINNER[self.frame % len(SPINNER)]} refreshing  ", style=TN["cyan"])
            elif self.last_full:
                nxt = max(0, int(REFRESH_SECS - (time.time() - self.last_full)))
                right.append("next refresh ", style=TN["comment"])
                right.append(f"{nxt:>2}s  ", style=TN["muted"])
            right.append(datetime.now().strftime("%H:%M:%S "), style=f"bold {TN['fg']}")
            bar_ = Table.grid(expand=True)
            bar_.add_column()
            bar_.add_column(justify="right")
            bar_.add_row(left, right)
            self.query_one("#top", TopBar).update(bar_)

        def action_refresh_now(self, force: bool = True) -> None:
            if self.loading:
                return
            self.loading = set(AGENTS)
            self.redraw()
            self.run_worker(lambda: self._fetch(force), thread=True, group="fetch")

        def _fetch(self, force: bool) -> None:
            collect(force, on_result=lambda r: self.call_from_thread(self._got, r))
            self.call_from_thread(self._done)

        def _got(self, r: Result) -> None:
            self.results[r.agent] = r
            self.loading.discard(r.agent)
            self.redraw()

        def _done(self) -> None:
            self.loading.clear()
            self.last_full = time.time()
            self.redraw()

    return Agtop()


def run_tui() -> None:
    make_app().run()


# ---------------------------------------------------------------- main
def main() -> None:
    ap = argparse.ArgumentParser(prog="agtop", description="Show 5-hour / weekly quota usage of Claude Code, "
                                                          "Antigravity and Codex.")
    ap.add_argument("--once", action="store_true", help="print the table once and exit")
    ap.add_argument("--json", action="store_true", help="print JSON and exit")
    a = ap.parse_args()
    if a.json:
        print(json.dumps([r.to_dict() for r in collect()], ensure_ascii=False, indent=2))
    elif a.once or not sys.stdout.isatty():
        tty = sys.stdout.isatty()
        con = Console(width=None if tty else 104)
        if con.width < 100:
            con = Console(width=100)
        con.print(render_once(collect(), min(con.width, 104)))
    else:
        run_tui()


if __name__ == "__main__":
    main()
