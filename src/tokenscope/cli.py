"""Explore recent Claude Code API usage from ~/.claude/projects transcripts.

On a terminal this opens an interactive explorer (sessions → calls → call
detail). With --print, or when stdout is not a terminal, it prints a report.

ctx  = tokens sent per call (input + cache write + cache read).
hit% = share of ctx served from the prompt cache.
≈$   = API list-price estimate; it weighs cache reads correctly, even on a
       subscription plan where you do not pay per token.
"""

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path.home() / ".claude" / "projects"

# $ per million tokens: (input, output, cache read). From the claude-api
# skill's model table (cached 2026-09-25). Cache writes are priced from
# input: 1.25x for the 5-minute TTL, 2x for the 1-hour TTL.
PRICES = {
    "claude-fable-5-1": (10.00, 50.00, 0.25),
    "claude-mythos-5-1": (10.00, 50.00, 0.25),
    "claude-fable-5": (10.00, 50.00, 1.00),
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-opus-5": (5.00, 25.00, 0.50),
    "claude-opus-4-8": (5.00, 25.00, 0.50),
    "claude-opus-4-7": (5.00, 25.00, 0.50),
    "claude-opus-4-6": (5.00, 25.00, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20),
    "claude-sonnet-5": (2.00, 10.00, 0.20),
    "claude-sonnet-4-6": (3.00, 15.00, 0.30),
    "claude-haiku-4-5": (1.00, 5.00, 0.10),
}
WRITE_5M, WRITE_1H = 1.25, 2.0
FAST_MULTIPLIER = 2.0  # fast mode bills 2x standard on Opus

SESSION_TIME = "%b %d %H:%M"
CALL_TIME = "%b %d %H:%M:%S"
WINDOWS = [30, 180, 1440, 10080, 43200]  # the TUI's `w` key cycles these
PRINT_SESSIONS = 15

KEY_TEXT = """\
ctx      tokens sent per call: input + cache write + cache read
hit%     share of ctx read from the prompt cache
cache wr tokens written to the cache (billed 1.25x input for 5 min, 2x for 1 h)
cache rd tokens read from the cache (billed at a fraction of input)
misses   later calls in a transcript that wrote more cache than they read
idle 2h  cache miss after a 2-hour gap (the cache entry had expired)
from X   cache miss after a switch from model X (each model has its own cache)
≈$       API list price; not a subscription bill

Every call re-reads the full cached prompt, so summed cache reads ≈ calls × ctx avg.
Large totals are normal."""


# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------

def n(value):
    return value if isinstance(value, (int, float)) else 0


def parse_ts(value):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None


def clean_text(value, max_len=140):
    if not value:
        return None
    value = re.sub(r"\s+", " ", str(value)).strip()
    return value[: max_len - 1] + "…" if len(value) > max_len else value


def user_text(content):
    """Return the typed text of a user message, ignoring tool results."""
    if isinstance(content, str):
        return clean_text(content)
    if isinstance(content, list):
        parts = [
            str(item["text"])
            for item in content
            if isinstance(item, dict) and item.get("type") == "text" and item.get("text")
        ]
        return clean_text(" ".join(parts)) if parts else None
    return None


def price_for(model):
    """Longest model-id prefix wins, so "claude-opus-5-5[1m]" matches opus-5-5."""
    for key in sorted(PRICES, key=len, reverse=True):
        if model and model.startswith(key):
            return PRICES[key]
    return None


def short_model(model):
    m = re.match(r"claude-([a-z]+)-(\d+)(?:-(\d{1,2}))?(?:\D|$)", model or "")
    if not m:
        return model or "?"
    family, major, minor = m.groups()
    return f"{family}-{major}.{minor}" if minor else f"{family}-{major}"


def fmt(x):
    if x >= 1_000_000:
        return f"{x / 1_000_000:.2f}M"
    if x >= 1_000:
        return f"{x / 1_000:.1f}k"
    return str(int(x))


def fmt_cost(x):
    if x is None:
        return "?"
    if x == 0 or x >= 0.005:
        return f"{x:,.2f}"
    return "<0.01"


def fmt_duration(seconds):
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 90 * 60:
        return f"{seconds / 60:.0f}m"
    if seconds < 36 * 3600:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


def fmt_window(minutes):
    if minutes < 120:
        return f"{minutes} min"
    if minutes < 2 * 1440:
        return f"{minutes / 60:g} h"
    return f"{minutes / 1440:g} d"


def local(ts, pattern):
    return ts.astimezone().strftime(pattern)


def fit(text, width):
    return text if len(text) <= width else text[: width - 1] + "…"


# -------------------------------------------------------------------
# Transcript scan
# -------------------------------------------------------------------

def new_meta():
    return {"custom_title": None, "ai_title": None, "summary": None, "first_prompt": None, "cwd": None}


def read_index_titles(metadata):
    for index_path in ROOT.rglob("sessions-index.json"):
        try:
            entries = json.loads(index_path.read_text(errors="replace")).get("entries", [])
        except (OSError, ValueError):
            continue
        for entry in entries:
            sid = entry.get("sessionId")
            if not sid:
                continue
            m = metadata.setdefault(sid, new_meta())
            m["custom_title"] = clean_text(entry.get("customTitle")) or m["custom_title"]
            m["summary"] = clean_text(entry.get("summary")) or m["summary"]
            m["first_prompt"] = m["first_prompt"] or clean_text(entry.get("firstPrompt"))
            m["cwd"] = m["cwd"] or entry.get("projectPath")


def prompt_of(obj):
    """The typed text of a real user prompt, or None."""
    if obj.get("type") != "user" or obj.get("isMeta"):
        return None
    text = user_text((obj.get("message") or {}).get("content"))
    return text if text and not text.startswith("<") else None  # skip hook / command payloads


def note_title(obj, metadata, fallback_sid):
    """Record a title-bearing record. Return True if obj was one."""
    kind = obj.get("type")
    m = metadata.setdefault(obj.get("sessionId") or fallback_sid, new_meta())
    if kind == "custom-title":
        m["custom_title"] = clean_text(obj.get("customTitle")) or m["custom_title"]
        return True
    if kind == "ai-title":
        m["ai_title"] = clean_text(obj.get("aiTitle")) or m["ai_title"]
        return True
    if not obj.get("isSidechain"):
        m["cwd"] = m["cwd"] or obj.get("cwd")
        if not m["first_prompt"]:
            m["first_prompt"] = prompt_of(obj)
    return False


def usage_row(obj, path, sid, ts, request, first_request, prev, thread):
    msg = obj.get("message") or {}
    usage = msg.get("usage") or {}
    create = n(usage.get("cache_creation_input_tokens"))
    split = usage.get("cache_creation") or {}
    write_1h = n(split.get("ephemeral_1h_input_tokens"))
    prev_ts, prev_model = prev
    return {
        "ts": ts,
        "gap": (ts - prev_ts).total_seconds() if prev_ts else None,
        "prev_model": prev_model,
        "request": request,
        "session": sid,
        "agent": obj.get("agentId"),
        "thread": thread,
        "file": str(path),
        "model": msg.get("model") or "?",
        "fast": usage.get("speed") == "fast",
        "input": n(usage.get("input_tokens")),
        "output": n(usage.get("output_tokens")),
        "create": create,
        "write_1h": min(write_1h, create),
        "read": n(usage.get("cache_read_input_tokens")),
        "first_in_file": request == first_request,
    }


def agent_label(path):
    """ "Plan: Design the page" from the subagent's .meta.json, or None."""
    try:
        meta = json.loads(path.with_suffix(".meta.json").read_text())
    except (OSError, ValueError):
        return None
    kind, description = meta.get("agentType"), clean_text(meta.get("description"))
    return f"{kind}: {description}" if kind and description else description


def scan_file(path, cutoff, metadata, requests):
    first_request = None
    file_prompt = None  # fallback subagent label: the task it was given
    label = agent_label(path) if path.name.startswith("agent-") else None
    # (timestamp, model) of the current and the previous distinct request,
    # used to explain cache misses.
    cur_request, cur, prev = None, (None, None), (None, None)
    try:
        with path.open("r", errors="replace") as f:
            for line in f:
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if note_title(obj, metadata, path.stem):
                    continue
                if obj.get("type") == "user" and not file_prompt:
                    file_prompt = prompt_of(obj)
                if obj.get("type") != "assistant":
                    continue
                msg = obj.get("message") or {}
                # "<synthetic>" records are local placeholders, not API calls.
                if not msg.get("usage") or (msg.get("model") or "").startswith("<"):
                    continue
                ts = parse_ts(obj.get("timestamp"))
                request = obj.get("requestId") or obj.get("uuid")
                first_request = first_request or request
                if request != cur_request:
                    cur_request, prev, cur = request, cur, (ts, msg.get("model"))
                if not ts or ts < cutoff:
                    continue
                sid = obj.get("sessionId") or path.stem
                if obj.get("agentId"):
                    thread = label or f"agent: {file_prompt or obj['agentId'][:8]}"
                else:
                    thread = "main"
                row = usage_row(obj, path, sid, ts, request, first_request, prev, thread)
                total = row["input"] + row["output"] + row["create"] + row["read"]
                if not total:
                    continue
                # One record is written per content block; they share a
                # requestId. Keep the one with the final (largest) usage.
                key = (row["file"], row["request"])
                old = requests.get(key)
                if old is None or total >= old["input"] + old["output"] + old["create"] + old["read"]:
                    requests[key] = row
    except OSError:
        pass


def scan_titles_only(path, metadata):
    try:
        with path.open("r", errors="replace") as f:
            for line in f:
                if '"custom-title"' in line or '"ai-title"' in line or '"user"' in line:
                    try:
                        note_title(json.loads(line), metadata, path.stem)
                    except ValueError:
                        continue
    except OSError:
        pass


def collect(minutes):
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    metadata, requests = {}, {}
    read_index_titles(metadata)
    cutoff_epoch = cutoff.timestamp()
    scanned = set()
    for path in ROOT.rglob("*.jsonl"):
        try:
            if path.stat().st_mtime < cutoff_epoch:
                continue
        except OSError:
            continue
        scanned.add(path)
        scan_file(path, cutoff, metadata, requests)

    rows = list(requests.values())

    # Subagent files carry no titles; read the parent transcript for any
    # session whose parent file was too old to be scanned above.
    for sid in {r["session"] for r in rows}:
        for parent in ROOT.glob(f"*/{sid}.jsonl"):
            if parent not in scanned:
                scan_titles_only(parent, metadata)

    for r in rows:
        r["cost"] = call_cost(r)
        r["ctx"] = r["input"] + r["create"] + r["read"]
        r["miss"] = r["create"] > r["read"] and not r["first_in_file"]
    rows.sort(key=lambda r: r["ts"])
    return rows, metadata


# -------------------------------------------------------------------
# Metrics
# -------------------------------------------------------------------

def cost_parts(r):
    """[(label, tokens, dollars)] for each billed part, or None for an unknown model."""
    price = price_for(r["model"])
    if price is None:
        return None
    p_in, p_out, p_read = price
    scale = (FAST_MULTIPLIER if r["fast"] else 1) / 1_000_000
    write_5m = r["create"] - r["write_1h"]
    return [
        ("input", r["input"], r["input"] * p_in * scale),
        ("cache write 5 min", write_5m, write_5m * p_in * WRITE_5M * scale),
        ("cache write 1 h", r["write_1h"], r["write_1h"] * p_in * WRITE_1H * scale),
        ("cache read", r["read"], r["read"] * p_read * scale),
        ("output", r["output"], r["output"] * p_out * scale),
    ]


def call_cost(r):
    parts = cost_parts(r)
    return None if parts is None else sum(dollars for _, _, dollars in parts)


def hit_pct(read, ctx):
    return f"{100 * read / ctx:.0f}%" if ctx else "-"


def sum_cost(rows):
    """None only when no row has a known price."""
    known = [r["cost"] for r in rows if r["cost"] is not None]
    return sum(known) if known else None


def model_costs(calls):
    """{model: dollars}, most expensive first."""
    costs = {}
    for r in calls:
        costs[r["model"]] = costs.get(r["model"], 0) + (r["cost"] or 0)
    return dict(sorted(costs.items(), key=lambda kv: kv[1], reverse=True))


def summarize_sessions(rows):
    sessions = {}
    for r in rows:
        sessions.setdefault(r["session"], []).append(r)

    out = []
    for sid, calls in sessions.items():
        models = list(model_costs(calls))
        label = short_model(models[0]) + (f" +{len(models) - 1}" if len(models) > 1 else "")
        ctx_total = sum(r["ctx"] for r in calls)
        out.append({
            "session": sid,
            "calls_list": calls,
            "last": max(r["ts"] for r in calls),
            "model": label,
            "calls": len(calls),
            "agents": len({r["agent"] for r in calls if r["agent"]}),
            "ctx_avg": ctx_total / len(calls),
            "ctx_peak": max(r["ctx"] for r in calls),
            "hit": hit_pct(sum(r["read"] for r in calls), ctx_total),
            "cost": sum_cost(calls),
            "misses": sum(r["miss"] for r in calls),
        })
    return sorted(out, key=lambda s: s["cost"] or 0, reverse=True)


def session_name(metadata, sid):
    m = metadata.get(sid, {})
    return (
        m.get("custom_title")
        or m.get("ai_title")
        or m.get("summary")
        or m.get("first_prompt")
        or f"(untitled {sid[:8]})"
    )


def miss_label(r):
    if not r["miss"]:
        return ""
    if r["prev_model"] and r["prev_model"] != r["model"]:
        return f"from {short_model(r['prev_model'])}"
    if r["gap"] is not None:
        return f"idle {fmt_duration(r['gap'])}"
    return "miss"


def miss_explanation(r):
    """One plain sentence on why the cache missed."""
    head = f"This call wrote {fmt(r['create'])} tokens to the cache but read only {fmt(r['read'])}."
    if r["prev_model"] and r["prev_model"] != r["model"]:
        return (f"{head} The previous call used {short_model(r['prev_model'])}. "
                f"Each model has its own cache, so the switch wrote the prompt again.")
    if r["gap"] is None:
        return head
    gap = fmt_duration(r["gap"])
    if r["gap"] > 3600 or (r["gap"] > 300 and not r["write_1h"]):
        return (f"{head} The previous call was {gap} earlier, which is longer than the "
                f"cache lifetime, so the cached prompt had expired.")
    return (f"{head} The previous call was only {gap} earlier, so the prompt prefix changed "
            f"(for example /compact, a changed tool list, or an edited earlier message).")


def summary_line(rows, sessions, minutes):
    misses = [r for r in rows if r["miss"]]
    return (
        f"Last {fmt_window(minutes)} · {len(rows)} calls · {len(sessions)} sessions · "
        f"≈${fmt_cost(sum_cost(rows))} · cache hit "
        f"{hit_pct(sum(r['read'] for r in rows), sum(r['ctx'] for r in rows))} · "
        f"{len(misses)} cache misses ≈${fmt_cost(sum_cost(misses) or 0)}"
    )


# -------------------------------------------------------------------
# Text report (--print)
# -------------------------------------------------------------------

SESSION_COLUMNS = [
    ("Last active", 12, "<"), ("Session", 8, "<"), ("Title", 28, "<"), ("Model", 13, "<"),
    ("Calls", 5, ">"), ("Agents", 6, ">"), ("Ctx avg", 7, ">"), ("Ctx max", 7, ">"),
    ("Hit%", 5, ">"), ("≈$", 8, ">"), ("Misses", 6, ">"),
]


def session_cells(s, metadata):
    return [
        local(s["last"], SESSION_TIME), s["session"][:8], session_name(metadata, s["session"]),
        s["model"], str(s["calls"]), str(s["agents"] or ""), fmt(s["ctx_avg"]),
        fmt(s["ctx_peak"]), s["hit"], fmt_cost(s["cost"]), str(s["misses"] or ""),
    ]


def print_report(rows, metadata, minutes, all_sessions):
    sessions = summarize_sessions(rows)
    shown = sessions if all_sessions else sessions[:PRINT_SESSIONS]

    print(f"\nClaude usage · {summary_line(rows, sessions, minutes)}\n")
    header = " ".join(f"{h:{a}{w}}" for h, w, a in SESSION_COLUMNS).rstrip()
    print(header)
    print("─" * len(header))
    for s in shown:
        cells = session_cells(s, metadata)
        print(" ".join(f"{fit(c, w):{a}{w}}" for c, (_, w, a) in zip(cells, SESSION_COLUMNS)).rstrip())

    rest = sessions[len(shown):]
    if rest:
        print(
            f"… {len(rest)} more sessions: {sum(s['calls'] for s in rest)} calls, "
            f"≈${fmt_cost(sum(s['cost'] or 0 for s in rest))}. Add --all-sessions to list them."
        )
    print("\n" + KEY_TEXT)
    print("\nRun without --print on a terminal to explore sessions and their calls.")


# -------------------------------------------------------------------
# Interactive explorer
# -------------------------------------------------------------------

def run_tui(minutes):
    from rich.text import Text
    from textual import work
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import VerticalScroll
    from textual.screen import ModalScreen, Screen
    from textual.widgets import DataTable, Footer, Input, LoadingIndicator, Static

    def right(value):
        return Text(value, justify="right")

    class HelpScreen(ModalScreen):
        BINDINGS = [Binding("escape,q,question_mark", "dismiss", "Close")]

        def compose(self) -> ComposeResult:
            yield Static(Text(KEY_TEXT), classes="dialog")

    class CallScreen(ModalScreen):
        BINDINGS = [Binding("escape,q", "dismiss", "Close")]

        def __init__(self, r, title):
            super().__init__()
            self.r, self.title_text = r, title

        def compose(self) -> ComposeResult:
            r = self.r
            lines = [
                f"Session   {self.title_text}",
                f"Thread    {r['thread']}",
                f"Time      {local(r['ts'], CALL_TIME)}",
                f"Model     {r['model']}{'  (fast mode)' if r['fast'] else ''}",
                f"Request   {r['request']}",
                f"File      {r['file']}",
                "",
                f"{'Part':<20} {'Tokens':>10} {'≈$':>8}",
            ]
            parts = cost_parts(r)
            for label, tokens, dollars in parts or []:
                lines.append(f"{label:<20} {fmt(tokens):>10} {fmt_cost(dollars):>8}")
            lines.append(f"{'total':<20} {fmt(r['ctx'] + r['output']):>10} {fmt_cost(r['cost']):>8}")
            if parts is None:
                lines.append(f"No price is known for {r['model']}.")
            lines.append("")
            if r["gap"] is None:
                lines.append("Previous call  none (first call in this transcript)")
            else:
                lines.append(
                    f"Previous call  {fmt_duration(r['gap'])} earlier, "
                    f"{short_model(r['prev_model'])}"
                )
            lines.append(f"Cache hit      {hit_pct(r['read'], r['ctx'])}")
            if r["miss"]:
                lines += ["", "Cache miss", miss_explanation(r)]
            yield VerticalScroll(Static(Text("\n".join(lines))), classes="dialog")

    class SessionScreen(Screen):
        BINDINGS = [
            Binding("escape", "app.pop_screen", "Back"),
            Binding("m", "toggle_misses", "Misses only"),
            Binding("question_mark", "app.help", "Key"),
        ]

        def __init__(self, s, metadata):
            super().__init__()
            self.s, self.metadata, self.misses_only = s, metadata, False

        def compose(self) -> ComposeResult:
            s, meta = self.s, self.metadata.get(self.s["session"], {})
            per_model = " · ".join(
                f"{short_model(m)} ≈${fmt_cost(c)}" for m, c in model_costs(s["calls_list"]).items()
            )
            header = [
                session_name(self.metadata, s["session"]),
                f"Session {s['session']}   (resume: claude --resume {s['session']})",
                f"Folder  {meta.get('cwd') or '?'}",
                f"{s['calls']} calls · {s['agents']} agents · ≈${fmt_cost(s['cost'])} "
                f"({per_model}) · cache hit {s['hit']} · {s['misses']} misses",
            ]
            yield Static(Text("\n".join(header)), classes="summary")
            yield DataTable(cursor_type="row", zebra_stripes=True)
            yield Footer()

        def on_mount(self):
            table = self.query_one(DataTable)
            table.add_columns("Time", "Thread", "Model", "Ctx", "Cache wr", "Cache rd",
                              "Out", "Hit%", "≈$", "Miss")
            self.fill()
            table.focus()

        def fill(self):
            table = self.query_one(DataTable)
            table.clear()
            for i, r in enumerate(self.s["calls_list"]):
                if self.misses_only and not r["miss"]:
                    continue
                table.add_row(
                    local(r["ts"], CALL_TIME), Text(fit(r["thread"], 40)), short_model(r["model"]),
                    right(fmt(r["ctx"])), right(fmt(r["create"])), right(fmt(r["read"])),
                    right(fmt(r["output"])), right(hit_pct(r["read"], r["ctx"])),
                    right(fmt_cost(r["cost"])), miss_label(r), key=str(i),
                )

        def action_toggle_misses(self):
            self.misses_only = not self.misses_only
            self.fill()

        def on_data_table_row_selected(self, event):
            event.stop()  # keep it from bubbling to the sessions handler
            r = self.s["calls_list"][int(event.row_key.value)]
            self.app.push_screen(CallScreen(r, session_name(self.metadata, self.s["session"])))

    sorts = [
        ("cost", lambda s: s["cost"] or 0),
        ("last active", lambda s: s["last"]),
        ("calls", lambda s: s["calls"]),
        ("misses", lambda s: s["misses"]),
    ]

    class UsageApp(App):
        TITLE = "Claude usage"
        CSS = """
        .summary { padding: 0 1; background: $boost; }
        .dialog { width: 90; height: auto; max-height: 90%; border: round $accent;
                  padding: 1 2; background: $surface; }
        HelpScreen, CallScreen { align: center middle; }
        DataTable { height: 1fr; }
        #filter { display: none; }
        #filter.visible { display: block; }
        """
        BINDINGS = [
            Binding("q", "quit", "Quit"),
            Binding("s", "sort", "Sort"),
            Binding("w", "window", "Window"),
            Binding("slash", "filter", "Filter"),
            Binding("r", "reload", "Reload"),
            Binding("question_mark", "help", "Key"),
            Binding("escape", "close_filter", show=False),
        ]

        def __init__(self, minutes):
            super().__init__()
            self.minutes, self.sort_index, self.filter_text = minutes, 0, ""
            self.rows, self.metadata, self.sessions = [], {}, []

        def compose(self) -> ComposeResult:
            yield Static(Text("Loading…"), id="summary", classes="summary")
            yield LoadingIndicator()
            yield DataTable(cursor_type="row", zebra_stripes=True, id="sessions")
            yield Input(placeholder="Filter by title or session id — Enter or Esc to close", id="filter")
            yield Footer()

        def on_mount(self):
            table = self.query_one(DataTable)
            table.add_columns("Last active", "Title", "Model", "Calls", "Agents",
                              "Ctx avg", "Ctx max", "Hit%", "≈$", "Misses")
            self.action_reload()

        def action_reload(self):
            self.query_one(LoadingIndicator).display = True
            self.query_one("#summary", Static).update(Text(f"Loading last {fmt_window(self.minutes)}…"))
            self.load(self.minutes)

        @work(thread=True, exclusive=True)
        def load(self, minutes):
            rows, metadata = collect(minutes)
            self.call_from_thread(self.loaded, rows, metadata)

        def loaded(self, rows, metadata):
            self.rows, self.metadata = rows, metadata
            self.sessions = summarize_sessions(rows)
            self.query_one(LoadingIndicator).display = False
            self.fill()
            self.query_one(DataTable).focus()

        def fill(self):
            name, key = sorts[self.sort_index]
            shown = [
                s for s in sorted(self.sessions, key=key, reverse=True)
                if self.filter_text in session_name(self.metadata, s["session"]).lower()
                or s["session"].startswith(self.filter_text)
            ]
            summary = summary_line(self.rows, self.sessions, self.minutes) if self.rows else (
                f"No Claude API activity in the last {fmt_window(self.minutes)}.")
            detail = f"Sorted by {name}"
            if self.filter_text:
                detail += f" · filter “{self.filter_text}”: {len(shown)} of {len(self.sessions)} sessions"
            self.query_one("#summary", Static).update(Text(f"{summary}\n{detail}"))

            table = self.query_one(DataTable)
            table.clear()
            for s in shown:
                table.add_row(
                    local(s["last"], SESSION_TIME), Text(fit(session_name(self.metadata, s["session"]), 48)),
                    s["model"], right(str(s["calls"])), right(str(s["agents"] or "")),
                    right(fmt(s["ctx_avg"])), right(fmt(s["ctx_peak"])), right(s["hit"]),
                    right(fmt_cost(s["cost"])), right(str(s["misses"] or "")), key=s["session"],
                )

        def action_sort(self):
            self.sort_index = (self.sort_index + 1) % len(sorts)
            self.fill()

        def action_window(self):
            later = [w for w in WINDOWS if w > self.minutes]
            self.minutes = later[0] if later else WINDOWS[0]
            self.action_reload()

        def action_filter(self):
            box = self.query_one("#filter", Input)
            box.add_class("visible")
            box.focus()

        def action_close_filter(self):
            box = self.query_one("#filter", Input)
            if box.has_class("visible"):
                box.remove_class("visible")
                self.query_one(DataTable).focus()

        def action_help(self):
            self.push_screen(HelpScreen())

        def on_input_changed(self, event):
            self.filter_text = event.value.strip().lower()
            self.fill()

        def on_input_submitted(self, event):
            self.action_close_filter()

        def on_data_table_row_selected(self, event):
            s = next(s for s in self.sessions if s["session"] == event.row_key.value)
            self.push_screen(SessionScreen(s, self.metadata))

    return UsageApp(minutes)


def main():
    parser = argparse.ArgumentParser(description="Explore recent Claude Code API usage.")
    parser.add_argument("minutes", nargs="?", type=int, default=30, help="look-back window in minutes (default 30)")
    parser.add_argument("--print", action="store_true", help="print a text report instead of opening the explorer")
    parser.add_argument("--all-sessions", action="store_true",
                        help=f"with --print, list every session (default: top {PRINT_SESSIONS} by cost)")
    args = parser.parse_args()

    if not ROOT.exists():
        print(f"No Claude project logs found at {ROOT}")
        return 1

    if not args.print and sys.stdout.isatty():
        run_tui(args.minutes).run()
        return 0

    rows, metadata = collect(args.minutes)
    if not rows:
        print(f"No Claude API activity found in the last {fmt_window(args.minutes)}.")
        return 0
    print_report(rows, metadata, args.minutes, args.all_sessions)
    return 0


if __name__ == "__main__":
    sys.exit(main())
