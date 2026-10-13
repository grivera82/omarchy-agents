"""Spend and plan limits for Claude Code, Codex, OpenCode and Grok.

Spend is what the tokens on this machine would cost at API list prices
(lib/prices.json), read from the same local files the session list uses.
Claude Pro/Max and ChatGPT plans don't bill per token, so for them it's an
estimate of value, not a bill. OpenCode records the cost of each message
itself, so its figure is the one OpenCode reports. Grok Build records the
cost of each turn when xAI reports it (API-key traffic) and is priced from
the table otherwise.

Plan limits (5-hour and weekly windows) come from the records Omarchy's
`omarchy-agent-usage-update` collectors write, which the built-in agents
widget also reads. This module never talks to the network itself.

Parsed token rows are cached in ~/.cache/grivera-agents/usage.json so a
refresh only reads what was appended since the last one.
"""

import datetime
import glob
import json
import os
import re
import sqlite3
import subprocess
import time

HOME = os.path.expanduser("~")
CLAUDE_DIR = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(HOME, ".claude")
CODEX_DIR = os.environ.get("CODEX_HOME") or os.path.join(HOME, ".codex")
OPENCODE_DIR = os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.join(HOME, ".local/share"), "opencode")
GROK_DIR = os.environ.get("GROK_HOME") or os.path.join(HOME, ".grok")
OMARCHY_USAGE = os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.join(HOME, ".local/state"),
                             "omarchy", "agents", "usage")
CACHE_FILE = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.join(HOME, ".cache"),
                          "grivera-agents", "usage.json")
PRICES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prices.json")

DAYS = 30                     # history kept and reported
CACHE_VERSION = 1
LIMITS_STALE = 10 * 60        # ask Omarchy to refresh limits older than this...
LIMITS_RETRY = 5 * 60         # ...at most this often

# A row is one model request:
#   [key, ts, model, input, output, cacheRead, cacheWrite5m, cacheWrite1h, fast, requestInput]
# `input` excludes cached tokens; `requestInput` is the whole prompt size, for
# long-context pricing. Claude logs a message once per content block with the
# usage so far, so rows sharing a key are merged by taking the maximum.


def local_day(ts):
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def parse_ts(s):
    if not s:
        return None
    try:
        return datetime.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def read_lines(path, entry):
    """New complete lines of `path` since `entry["pos"]`, starting over (and
    clearing the entry) when the file was replaced or truncated."""
    try:
        st = os.stat(path)
    except OSError:
        return []
    if entry.get("ino") != st.st_ino or st.st_size < entry.get("pos", 0):
        entry.clear()
        entry.update(ino=st.st_ino, pos=0, rows=[], state={})
    if st.st_size == entry["pos"]:
        return []
    try:
        with open(path, "rb") as f:
            f.seek(entry["pos"])
            data = f.read()
    except OSError:
        return []
    end = data.rfind(b"\n") + 1
    entry["pos"] += end
    out = []
    for line in data[:end].split(b"\n"):
        if line:
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


# ---------------------------------------------------------------- sources

def claude_rows(o, entry):
    if o.get("type") != "assistant":
        return
    m = o.get("message") or {}
    u = m.get("usage")
    model = m.get("model")
    ts = parse_ts(o.get("timestamp"))
    if not u or not ts or not model or model.startswith("<"):
        return
    cw = u.get("cache_creation") or {}
    cw1h = cw.get("ephemeral_1h_input_tokens") or 0
    cw5m = cw.get("ephemeral_5m_input_tokens") or 0
    if not cw:
        cw5m = u.get("cache_creation_input_tokens") or 0
    inp, cr = u.get("input_tokens") or 0, u.get("cache_read_input_tokens") or 0
    key = "%s:%s" % (m.get("id") or o.get("uuid"), o.get("requestId") or "")
    entry["rows"].append([key, ts, model, inp, u.get("output_tokens") or 0, cr, cw5m, cw1h,
                          u.get("speed") == "fast", inp + cr + cw5m + cw1h])


def codex_rows(o, entry):
    st = entry["state"]
    p = o.get("payload") if isinstance(o.get("payload"), dict) else {}
    if o.get("type") == "turn_context":
        st["model"] = p.get("model") or st.get("model")
        return
    if o.get("type") != "event_msg" or p.get("type") != "token_count":
        return
    info = p.get("info") or {}
    total = info.get("total_token_usage")
    ts = parse_ts(o.get("timestamp"))
    if not total or not ts:
        return
    now = [total.get("input_tokens") or 0, total.get("cached_input_tokens") or 0, total.get("output_tokens") or 0]
    prev = st.get("total") or [0, 0, 0]
    st["total"] = now
    d = [max(0, a - b) for a, b in zip(now, prev)]
    if not any(d):
        return
    last = info.get("last_token_usage") or {}
    entry["rows"].append([None, ts, st.get("model") or "codex", d[0] - min(d[0], d[1]), d[2], d[1], 0, 0,
                          False, last.get("input_tokens") or d[0]])


SOURCES = {
    "claude": (lambda: glob.glob(os.path.join(CLAUDE_DIR, "projects", "*", "*.jsonl"))
               + glob.glob(os.path.join(CLAUDE_DIR, "projects", "*", "*", "subagents", "*.jsonl")), claude_rows),
    "codex": (lambda: glob.glob(os.path.join(CODEX_DIR, "sessions", "*", "*", "*", "rollout-*.jsonl"))
              + glob.glob(os.path.join(CODEX_DIR, "archived_sessions", "rollout-*.jsonl")), codex_rows),
}


def opencode_rows(since):
    """(rows, cost per row) for OpenCode assistant messages, cost as OpenCode
    recorded it. Child (subagent) sessions are included."""
    path = os.path.join(OPENCODE_DIR, "opencode.db")
    if not os.path.exists(path):
        return []
    try:
        con = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=1)
        try:
            q = con.execute(
                "select id, time_created, json_extract(data, '$.model.id'), json_extract(data, '$.model.providerID'),"
                " json_extract(data, '$.tokens.input'), json_extract(data, '$.tokens.output'),"
                " json_extract(data, '$.tokens.reasoning'), json_extract(data, '$.tokens.cache.read'),"
                " json_extract(data, '$.tokens.cache.write'), json_extract(data, '$.cost')"
                " from session_message where type = 'assistant' and time_created > ?", (int(since * 1000),))
            rows = q.fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return []
    out = []
    for mid, created, model, provider, inp, outp, reason, cr, cw, cost in rows:
        out.append(([mid, created / 1000.0, model or "?", inp or 0, (outp or 0) + (reason or 0), cr or 0, cw or 0, 0,
                     False, 0], float(cost or 0), provider or ""))
    return out


def epoch(v):
    """Seconds since the epoch from an ISO string or a number in s or ms."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return v / 1000.0 if v > 1e11 else float(v)
    return parse_ts(v) if isinstance(v, str) else None


def grok_rows(since, prices):
    """(row, cost, "recorded" or "table") per Grok Build turn from each
    session's usage.json, which is what `grok usage <id>` prints. Cost is the
    one Grok recorded (costUsdTicks, 1e10 per USD) when it's complete, else
    the table's. Turns carry no id, so one copied into a fork counts again."""
    out, seen = [], set()
    for path in glob.glob(os.path.join(GROK_DIR, "sessions", "*", "*", "usage.json")):
        try:
            if os.path.getmtime(path) < since:
                continue
            with open(path) as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        sid = data.get("sessionId") or os.path.basename(os.path.dirname(path))
        session = data.get("session") if isinstance(data.get("session"), dict) else {}
        default_model = session.get("primaryModelId") or "grok"
        for turn in data.get("turns") or []:
            if not isinstance(turn, dict):
                continue
            ts = epoch(turn.get("endedAt")) or epoch(data.get("updatedAt"))
            key = "%s:%s" % (sid, turn.get("turnId") or turn.get("turnNumber"))
            if not ts or ts < since or key in seen:
                continue
            seen.add(key)
            per_model = turn.get("modelUsage")
            parts = [(m, u) for m, u in per_model.items() if isinstance(u, dict)] if isinstance(per_model, dict) else []
            for model, u in parts or [(turn.get("modelId") or default_model, turn)]:
                row, cost = grok_row(key + ":" + model, ts, model, u)
                if row:
                    out.append((row, cost, "recorded") if cost is not None else (row, prices.cost(row), "table"))
    return out


def grok_tokens(u):
    """[uncached input, output, cache read, cache write] from a Grok usage
    summary. Grok's inputTokens is the whole prompt, cache hits included, and
    outputTokens includes reasoning."""
    def n(*names):
        for name in names:
            v = u.get(name)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                return int(v)
        return 0
    cr = n("cachedReadTokens", "cacheReadTokens", "cacheReadInputTokens")
    cw = n("cacheCreationTokens", "cacheCreationInputTokens")
    return [max(0, n("inputTokens") - cr), n("outputTokens"), cr, cw]


def grok_row(key, ts, model, u):
    inp, outp, cr, cw = grok_tokens(u)
    if not (inp or outp or cr or cw):
        return None, None
    calls = max(1, u.get("modelCalls") or 1)
    ticks = u.get("costUsdTicks")
    cost = None
    if isinstance(ticks, (int, float)) and not u.get("costIsPartial") and not u.get("usageIsIncomplete"):
        cost = ticks / 1e10
    # Long-context pricing goes by prompt size, which a turn of several calls
    # only gives on average.
    return [key, ts, model, inp, outp, cr, cw, 0, False, (inp + cr + cw) // calls], cost


# ---------------------------------------------------------------- pricing

class Prices:
    def __init__(self):
        data = {}
        try:
            with open(PRICES_FILE) as f:
                data = json.load(f)
        except (OSError, ValueError):
            pass
        self.models = data.get("models") or {}
        self.updated = data.get("updated")
        self.memo = {}

    def find(self, model):
        if model in self.memo:
            return self.memo[model]
        name = re.sub(r"-\d{8}$", "", model or "")
        hit = self.models.get(name)
        if hit is None:
            best = max((k for k in self.models if name.startswith(k + "-") or name.startswith(k + ".")),
                       key=len, default=None)
            hit = self.models.get(best)
        self.memo[model] = hit
        return hit

    def cost(self, row):
        p = self.find(row[2])
        if not p:
            return None
        inp_rate, out_rate = p["input"], p["output"]
        read_rate = p.get("cacheRead", inp_rate * 0.1)
        w5, w1h = p.get("cacheWrite5m", inp_rate * 1.25), p.get("cacheWrite1h", inp_rate * 2)
        in_mult = out_mult = p.get("fast", 1) if row[8] else 1
        lc = p.get("longContext")
        if lc and row[9] > lc["above"]:
            in_mult *= lc.get("input", 1)
            out_mult *= lc.get("output", 1)
        return (in_mult * (row[3] * inp_rate + row[5] * read_rate + row[6] * w5 + row[7] * w1h)
                + out_mult * row[4] * out_rate) / 1e6


# ---------------------------------------------------------------- limits

def omarchy_record(agent):
    try:
        with open(os.path.join(OMARCHY_USAGE, agent + ".json")) as f:
            rec = json.load(f)
        return rec if isinstance(rec, dict) else None
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------- ledger

class Ledger:
    def __init__(self):
        self.prices = Prices()
        self.cache = {"version": CACHE_VERSION, "files": {}}
        try:
            with open(CACHE_FILE) as f:
                data = json.load(f)
            if data.get("version") == CACHE_VERSION:
                self.cache = data
        except (OSError, ValueError, AttributeError):
            pass
        self.limits_asked = 0

    def save(self):
        os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
        tmp = CACHE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.cache, f, separators=(",", ":"))
        os.replace(tmp, CACHE_FILE)

    def scan(self, provider, since):
        list_files, parse = SOURCES[provider]
        files = self.cache["files"]
        seen = set()
        changed = False
        for path in list_files():
            try:
                if os.path.getmtime(path) < since:
                    continue
            except OSError:
                continue
            key = provider + ":" + path
            seen.add(key)
            entry = files.setdefault(key, {})
            before = (entry.get("ino"), entry.get("pos"))
            for o in read_lines(path, entry):
                parse(o, entry)
            if before != (entry.get("ino"), entry.get("pos")):
                changed = True
                entry["rows"] = [r for r in entry["rows"] if r[1] >= since]
        for key in [k for k in files if k.startswith(provider + ":") and k not in seen]:
            del files[key]
            changed = True
        merged, order = {}, []
        for key in seen:
            for r in files[key].get("rows", []):
                if r[0] is None:
                    order.append(r)
                    continue
                old = merged.get(r[0])
                if old is None:
                    merged[r[0]] = list(r)
                else:
                    for i in range(3, 8):
                        old[i] = max(old[i], r[i])
                    old[9] = max(old[9], r[9])
        return order + list(merged.values()), changed

    def refresh_limits(self, records):
        """Ask Omarchy's collectors for fresh limits when the records are old.
        That's the command the built-in agents widget runs on its own timer."""
        now = time.time()
        stale = [a for a, rec in records.items()
                 if not rec or now - (parse_ts(rec.get("updatedAt")) or 0) > LIMITS_STALE]
        if not stale or now - self.limits_asked < LIMITS_RETRY:
            return
        self.limits_asked = now
        try:
            subprocess.Popen(["omarchy-agent-usage-update", "--limits-only"] + stale,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
        except OSError:
            pass

    def report(self, refresh_limits=False):
        now = time.time()
        today = datetime.date.today()
        since = time.mktime((today - datetime.timedelta(days=DAYS - 1)).timetuple())
        days = [(today - datetime.timedelta(days=i)).isoformat() for i in range(DAYS - 1, -1, -1)]
        week = set(days[-7:])
        records = {a: omarchy_record(a) for a in ("claude", "codex")}
        if refresh_limits:
            self.refresh_limits(records)

        dirty = False
        priced = {}
        for provider in SOURCES:
            rows, changed = self.scan(provider, since)
            dirty = dirty or changed
            priced[provider] = [(r, self.prices.cost(r), "") for r in rows]
        priced["opencode"] = opencode_rows(since)
        priced["grok"] = grok_rows(since, self.prices)
        if dirty:
            try:
                self.save()
            except OSError:
                pass

        out = []
        for provider, name in (("claude", "Claude Code"), ("codex", "Codex"), ("opencode", "OpenCode"),
                               ("grok", "Grok")):
            by_day = {d: {"cost": 0.0, "tokens": 0} for d in days}
            models = {}
            unpriced = set()
            requests = 0
            # Grok's turns are partly recorded cost and partly table prices.
            estimated = provider != "opencode" and (
                provider != "grok" or any(src == "table" for _, _, src in priced[provider]))
            for row, cost, _ in priced[provider]:
                day = local_day(row[1])
                if day not in by_day:
                    continue
                fresh = row[3] + row[4] + row[6] + row[7]
                m = models.setdefault(row[2], {"model": row[2], "cost": 0.0, "today": 0.0, "tokens": 0,
                                               "cacheRead": 0, "requests": 0, "priced": cost is not None})
                m["tokens"] += fresh
                m["cacheRead"] += row[5]
                m["requests"] += 1
                requests += 1
                by_day[day]["tokens"] += fresh
                if cost is None:
                    unpriced.add(row[2])
                    continue
                m["cost"] += cost
                by_day[day]["cost"] += cost
                if day == days[-1]:
                    m["today"] += cost

            def total(keys, field):
                return sum(by_day[d][field] for d in keys)

            rec = records.get(provider) or {}
            limits = []
            for lim in rec.get("limits") or []:
                if isinstance(lim, dict) and lim.get("label"):
                    limits.append({"label": lim["label"], "percent": float(lim.get("percent") or 0),
                                   "resetsAt": parse_ts(lim.get("resetsAt"))})
            entry = {
                "id": provider,
                "name": name,
                "plan": rec.get("tierLabel") or "",
                "limits": limits,
                "limitsAt": parse_ts(rec.get("updatedAt")),
                "limitsNote": "" if provider in ("opencode", "grok") else (
                    "" if rec.get("ready") else (rec.get("usageStatusText") or rec.get("authHelpText")
                                                 or "Limits come from Omarchy's agent usage collectors.")),
                "todayPrompts": rec.get("todayPrompts"),
                "todaySessions": rec.get("todaySessions"),
                "spend": {"today": round(by_day[days[-1]]["cost"], 4), "week": round(total(week, "cost"), 4),
                          "month": round(total(days, "cost"), 4)},
                "tokens": {"today": by_day[days[-1]]["tokens"], "week": total(week, "tokens"),
                           "month": total(days, "tokens")},
                "days": [{"date": d, "cost": round(by_day[d]["cost"], 4), "tokens": by_day[d]["tokens"]}
                         for d in days[-14:]],
                "models": sorted(({**m, "cost": round(m["cost"], 4), "today": round(m["today"], 4)}
                                  for m in models.values()), key=lambda m: (-m["cost"], -m["tokens"])),
                "requests": requests,
                "unpriced": sorted(unpriced),
                "estimate": estimated,
                "active": requests > 0 or bool(limits),
            }
            out.append(entry)
        return {"providers": out, "pricesUpdated": self.prices.updated, "days": DAYS, "at": now}


def money(x):
    if x >= 100:
        return "$%.0f" % x
    if x >= 0.995 or x == 0:
        return "$%.2f" % x
    return "$%.3f" % x


def print_report(rep):
    for p in rep["providers"]:
        if not p["active"]:
            continue
        print("%s%s" % (p["name"], " (" + p["plan"] + ")" if p["plan"] else ""))
        for lim in p["limits"]:
            reset = ""
            if lim["resetsAt"]:
                reset = " · resets " + datetime.datetime.fromtimestamp(lim["resetsAt"]).strftime("%a %H:%M")
            print("  %-18s %3.0f%%%s" % (lim["label"], lim["percent"] * 100, reset))
        if p["limitsNote"]:
            print("  " + p["limitsNote"])
        s = p["spend"]
        label = "API-equivalent" if p["estimate"] else "cost"
        print("  %s: %s today · %s 7 days · %s 30 days" % (label, money(s["today"]), money(s["week"]), money(s["month"])))
        for m in p["models"][:5]:
            print("    %-28s %9s  %s tokens" % (m["model"][:28], money(m["cost"]) if m["priced"] else "no price",
                                               "{:,}".format(m["tokens"])))
    print("Prices as of %s (lib/prices.json)." % rep["pricesUpdated"])
