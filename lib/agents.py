#!/usr/bin/env python3
"""Live Claude Code and Codex sessions for the grivera.agents Omarchy plugin.

Reads what the CLIs already keep on disk, so nothing has to be configured:

  Claude Code  ~/.claude/sessions/<pid>.json   live registry (busy/idle/waiting)
               ~/.claude/projects/*/<id>.jsonl transcript (title, token usage)
  Codex        ~/.codex/state_*.sqlite          thread index
               ~/.codex/sessions/**/rollout-*   rollout (turns, approvals, tokens)

Standard library only.

  agents status [--json]       one-shot report
  agents daemon                JSON state lines on stdout, commands on stdin
  agents focus [ID|next]       focus a session's terminal window; `next` picks
                               the one waiting longest, else the latest finished
"""

import collections
import datetime
import glob
import json
import os
import re
import socket
import sqlite3
import struct
import subprocess
import sys
import threading
import time

HOME = os.path.expanduser("~")
CLAUDE_DIR = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(HOME, ".claude")
CODEX_DIR = os.environ.get("CODEX_HOME") or os.path.join(HOME, ".codex")
STATE_DIR = os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.join(HOME, ".local/state"), "grivera-agents")
CONFIG_FILE = os.path.join(STATE_DIR, "config.json")
ASSETS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets")

DEFAULT_CONFIG = {
    "notifyFinished": True,   # a turn that ran at least minTurnSeconds ended
    "notifyWaiting": True,    # a session stopped to ask you something
    # Off by default: Omarchy's notification server saves each notification by passing
    # its text to bash as an argument, where other local users can read it in /proc.
    "notifyReplies": False,   # put the agent's last reply in "finished" notifications
    "notifyDetails": False,   # session title (often the first prompt), folder and wait reason
    "minTurnSeconds": 20,
}

POLL_BUSY = 1.0
POLL_IDLE = 2.5
BURN_WINDOW = 300             # seconds of history behind "tokens per minute"
CODEX_RECENT = 12 * 3600      # threads touched this recently are candidates
CODEX_LINGER = 10 * 60        # a Codex thread with no process stays listed this long
SUBAGENT_RESCAN = 10

PROVIDERS = {"claude": "Claude Code", "codex": "Codex"}
CLAUDE_STATES = {"busy": "working", "idle": "idle", "waiting": "waiting"}
CODEX_WAIT_EVENTS = {
    "exec_approval_request": "approve command",
    "apply_patch_approval_request": "approve edit",
    "request_permissions": "permission prompt",
    "request_user_input": "input needed",
    "elicitation_request": "input needed",
}


# ---------------------------------------------------------------- helpers

def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def parse_ts(s):
    if not s:
        return None
    try:
        return datetime.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def proc_stat(pid):
    """(ppid, starttime) from /proc/<pid>/stat, or None if it's gone."""
    try:
        with open("/proc/%d/stat" % pid) as f:
            raw = f.read()
    except (OSError, ValueError):
        return None
    fields = raw[raw.rfind(")") + 2:].split()
    return int(fields[1]), fields[19]


def ancestors(pid):
    chain = []
    while pid and pid > 1 and len(chain) < 64:
        chain.append(pid)
        st = proc_stat(pid)
        if not st:
            break
        pid = st[0]
    return chain


def short_path(path):
    if not path:
        return ""
    if path == HOME:
        return "~"
    if path.startswith(HOME + "/"):
        return "~" + path[len(HOME):]
    return path


def one_line(text, limit=180):
    text = re.sub(r"[*_`#>]+", "", text or "")
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


class Tail:
    """Incremental JSONL reader. `reset` is set when the file was replaced or
    truncated, so callers can drop what they accumulated from it."""

    def __init__(self, path):
        self.path = path
        self.pos = 0
        self.buf = b""
        self.ino = None
        self.mtime = 0

    def read(self):
        try:
            st = os.stat(self.path)
        except OSError:
            return [], False
        self.mtime = st.st_mtime
        reset = False
        if st.st_ino != self.ino or st.st_size < self.pos:
            reset = self.ino is not None
            self.ino, self.pos, self.buf = st.st_ino, 0, b""
        if st.st_size == self.pos:
            return [], reset
        try:
            with open(self.path, "rb") as f:
                f.seek(self.pos)
                data = f.read()
        except OSError:
            return [], reset
        self.pos += len(data)
        lines = (self.buf + data).split(b"\n")
        self.buf = lines.pop()
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
        return out, reset


class Burn:
    """Fresh tokens (input + cache writes + output, not cache reads) over the
    last BURN_WINDOW seconds, as tokens per minute."""

    def __init__(self):
        self.events = collections.deque()

    def add(self, ts, n):
        if n > 0 and ts and ts > time.time() - BURN_WINDOW:
            self.events.append((ts, n))

    def rate(self, now):
        while self.events and self.events[0][0] < now - BURN_WINDOW:
            self.events.popleft()
        return sum(n for _, n in self.events) * 60.0 / BURN_WINDOW


def round_rate(r):
    """Two significant figures, so a slowly decaying rate doesn't re-emit state
    every poll."""
    if r < 1:
        return 0
    digits = len(str(int(r))) - 2
    return int(round(r, -digits)) if digits > 0 else int(round(r))


# ---------------------------------------------------------------- Claude Code

class ClaudeTranscript:
    def __init__(self, path, main=True):
        self.tail = Tail(path)
        self.main = main
        self.clear()

    def clear(self):
        self.usage = {}           # message id -> (input, output, cacheRead, cacheWrite)
        self.tokens = [0, 0, 0, 0]
        self.burn = Burn()
        self.context = 0
        self.model = None
        self.title = None
        self.custom_title = None
        self.last_prompt = None
        self.last_text = None
        self.turn_start = None

    def update(self):
        records, reset = self.tail.read()
        if reset:
            self.clear()
        for rec in records:
            self.feed(rec)

    def feed(self, o):
        t = o.get("type")
        if t == "assistant":
            m = o.get("message") or {}
            model = m.get("model")
            if model and not model.startswith("<"):
                self.model = model
            for block in m.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "text" and block.get("text", "").strip():
                    self.last_text = block["text"]
            u = m.get("usage")
            mid = m.get("id") or o.get("uuid")
            if not u or not mid:
                return
            new = (u.get("input_tokens") or 0, u.get("output_tokens") or 0,
                   u.get("cache_read_input_tokens") or 0, u.get("cache_creation_input_tokens") or 0)
            # One API message is logged once per content block, each repeating
            # the usage seen so far: count only what grew since the last copy.
            old = self.usage.get(mid, (0, 0, 0, 0))
            delta = [max(0, a - b) for a, b in zip(new, old)]
            self.usage[mid] = tuple(max(a, b) for a, b in zip(new, old))
            for i in range(4):
                self.tokens[i] += delta[i]
            self.burn.add(parse_ts(o.get("timestamp")), delta[0] + delta[1] + delta[3])
            if not o.get("isSidechain"):
                self.context = new[0] + new[2] + new[3]
        elif t == "user" and not o.get("isSidechain") and not o.get("isMeta"):
            content = (o.get("message") or {}).get("content")
            text = content if isinstance(content, str) else None
            if isinstance(content, list):
                parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
                if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
                    parts = []
                text = " ".join(parts) if parts else None
            if text and not text.lstrip().startswith("<"):
                self.last_prompt = text
                self.turn_start = parse_ts(o.get("timestamp"))
        elif t == "ai-title":
            self.title = o.get("aiTitle") or self.title
        elif t == "custom-title":
            self.custom_title = o.get("customTitle") or self.custom_title
        elif t == "last-prompt":
            self.last_prompt = o.get("lastPrompt") or self.last_prompt


class ClaudeSession:
    def __init__(self, pid):
        self.pid = pid
        self.session_id = None
        self.transcript = None
        self.subagents = {}
        self.sub_scan = 0
        self.reg = {}

    @staticmethod
    def transcript_path(session_id, cwd):
        slug = re.sub(r"[^A-Za-z0-9]", "-", cwd or "")
        path = os.path.join(CLAUDE_DIR, "projects", slug, session_id + ".jsonl")
        if os.path.exists(path):
            return path
        found = glob.glob(os.path.join(CLAUDE_DIR, "projects", "*", session_id + ".jsonl"))
        return found[0] if found else path

    def update(self, reg, now):
        self.reg = reg
        sid = reg.get("sessionId")
        if sid and sid != self.session_id:
            # /clear and /resume keep the process but switch transcripts.
            self.session_id = sid
            self.transcript = ClaudeTranscript(self.transcript_path(sid, reg.get("cwd")))
            self.subagents, self.sub_scan = {}, 0
        if not self.transcript:
            return
        self.transcript.update()
        if now - self.sub_scan > SUBAGENT_RESCAN:
            self.sub_scan = now
            base = os.path.splitext(self.transcript.tail.path)[0]
            for path in glob.glob(os.path.join(base, "subagents", "*.jsonl")):
                if path not in self.subagents:
                    self.subagents[path] = ClaudeTranscript(path, main=False)
        for sub in self.subagents.values():
            sub.update()

    def snapshot(self, now):
        reg, tr = self.reg, self.transcript
        parts = [tr] + list(self.subagents.values()) if tr else []
        tokens = [sum(p.tokens[i] for p in parts) for i in range(4)]
        burn = sum(p.burn.rate(now) for p in parts)
        name = reg.get("name") if reg.get("nameSource") in ("user", "peer") else None
        title = (name or (tr and (tr.custom_title or tr.title))
                 or one_line(tr and tr.last_prompt, 60) or os.path.basename(reg.get("cwd") or "") or "Claude Code")
        state = CLAUDE_STATES.get(reg.get("status"), "idle")
        since = (reg.get("statusUpdatedAt") or reg.get("startedAt") or now * 1000) / 1000.0
        return {
            "id": "claude:%d" % self.pid,
            "provider": "claude",
            "pid": self.pid,
            "sessionId": self.session_id,
            "title": title,
            "titleMatch": tr.title if tr else None,
            "cwd": short_path(reg.get("cwd")),
            "kind": reg.get("kind") or "interactive",
            "state": state,
            "waitingFor": reg.get("waitingFor") if state == "waiting" else None,
            "since": since,
            "startedAt": (reg.get("startedAt") or 0) / 1000.0,
            "turnStart": tr.turn_start if tr else None,
            "model": tr.model if tr else None,
            "tokens": {"input": tokens[0], "output": tokens[1], "cacheRead": tokens[2], "cacheWrite": tokens[3],
                       "fresh": tokens[0] + tokens[1] + tokens[3]},
            "context": tr.context if tr else 0,
            "contextWindow": None,
            "burn": round_rate(burn),
            "lastPrompt": one_line(tr.last_prompt if tr else None, 140),
            "lastText": one_line(tr.last_text if tr else None, 240),
        }


def claude_registry():
    out = {}
    for path in glob.glob(os.path.join(CLAUDE_DIR, "sessions", "*.json")):
        reg = load_json(path, None)
        if not isinstance(reg, dict) or not isinstance(reg.get("pid"), int):
            continue
        st = proc_stat(reg["pid"])
        # Registry files can outlive their process; procStart guards pid reuse.
        if not st or (reg.get("procStart") and str(reg["procStart"]) != st[1]):
            continue
        out[reg["pid"]] = reg
    return out


# ---------------------------------------------------------------- Codex

class CodexThread:
    def __init__(self, row):
        self.id = row["id"]
        self.row = row
        self.tail = Tail(row["rollout_path"])
        self.cwd = row.get("cwd")
        self.model = row.get("model")
        self.busy = False
        self.waiting = None
        self.since = None
        self.turn_start = None
        self.started = (row.get("created_at_ms") or 0) / 1000.0 or None
        self.tokens = None
        self.context = 0
        self.window = None
        self.burn = Burn()
        self.last_prompt = row.get("first_user_message")
        self.last_text = None
        self.pid = None

    def update(self):
        records, reset = self.tail.read()
        if reset:
            self.__init__(self.row)
            records, _ = self.tail.read()
        for rec in records:
            self.feed(rec)

    def feed(self, o):
        ts = parse_ts(o.get("timestamp"))
        t = o.get("type")
        p = o.get("payload") if isinstance(o.get("payload"), dict) else {}
        if t == "session_meta":
            self.cwd = p.get("cwd") or self.cwd
            self.started = self.started or parse_ts(p.get("timestamp"))
            return
        if t == "turn_context":
            self.model = p.get("model") or self.model
            return
        if t != "event_msg":
            return
        e = p.get("type")
        if e == "task_started":
            self.busy, self.waiting, self.since, self.turn_start = True, None, ts, ts
            self.window = p.get("model_context_window") or self.window
        elif e in ("task_complete", "turn_aborted"):
            self.busy, self.waiting, self.since = False, None, ts
            if p.get("last_agent_message"):
                self.last_text = p["last_agent_message"]
        elif e in CODEX_WAIT_EVENTS:
            self.waiting, self.since = CODEX_WAIT_EVENTS[e], ts
        elif e == "token_count":
            info = p.get("info") or {}
            total = info.get("total_token_usage")
            if total:
                fresh = ((total.get("input_tokens") or 0) - (total.get("cached_input_tokens") or 0)
                         + (total.get("output_tokens") or 0))
                if self.tokens:
                    self.burn.add(ts, fresh - self.tokens["fresh"])
                self.tokens = {"input": (total.get("input_tokens") or 0) - (total.get("cached_input_tokens") or 0),
                               "output": total.get("output_tokens") or 0,
                               "cacheRead": total.get("cached_input_tokens") or 0,
                               "cacheWrite": total.get("cache_write_input_tokens") or 0,
                               "fresh": fresh}
            last = info.get("last_token_usage") or {}
            self.context = last.get("input_tokens") or self.context
            self.window = info.get("model_context_window") or self.window
        elif e == "user_message":
            self.last_prompt = p.get("message") or self.last_prompt
        elif self.waiting and e in ("exec_command_begin", "patch_apply_begin", "item_started", "agent_message"):
            # The approval was answered and the turn moved on.
            self.waiting, self.since = None, ts

    def snapshot(self, now):
        state = "waiting" if self.waiting else "working" if self.busy else "idle"
        title = self.row.get("name") or self.row.get("title") or one_line(self.last_prompt, 60) or "Codex"
        return {
            "id": "codex:" + self.id,
            "provider": "codex",
            "pid": self.pid,
            "sessionId": self.id,
            "title": one_line(title, 80),
            "titleMatch": None,
            "cwd": short_path(self.cwd),
            "kind": self.row.get("originator") or self.row.get("source") or "",
            "state": state,
            "waitingFor": self.waiting,
            "since": self.since or self.started or now,
            "startedAt": self.started or 0,
            "turnStart": self.turn_start,
            "model": self.model,
            "tokens": self.tokens or {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "fresh": 0},
            "context": self.context,
            "contextWindow": self.window,
            "burn": round_rate(self.burn.rate(now)),
            "lastPrompt": one_line(self.last_prompt, 140),
            "lastText": one_line(self.last_text, 240),
        }


def codex_rows(now):
    """Recent top-level threads from Codex's sqlite index, or from the rollout
    files themselves if the schema moved on."""
    dbs = sorted(glob.glob(os.path.join(CODEX_DIR, "state_*.sqlite")),
                 key=lambda p: int(re.sub(r"\D", "", os.path.basename(p)) or 0))
    if dbs:
        try:
            con = sqlite3.connect("file:%s?mode=ro" % dbs[-1], uri=True, timeout=1)
            con.row_factory = sqlite3.Row
            rows = [dict(r) for r in con.execute(
                "select * from threads where updated_at_ms > ? order by updated_at_ms desc limit 30",
                (int((now - CODEX_RECENT) * 1000),))]
            con.close()
            return [r for r in rows if r.get("rollout_path") and not r.get("archived")
                    and not r.get("agent_role") and r.get("thread_source", "user") in ("user", None)]
        except sqlite3.Error:
            pass
    rows = []
    for path in glob.glob(os.path.join(CODEX_DIR, "sessions", "*", "*", "*", "rollout-*.jsonl")):
        try:
            if os.path.getmtime(path) < now - CODEX_RECENT:
                continue
        except OSError:
            continue
        m = re.search(r"([0-9a-f]{8}-[0-9a-f-]{27})\.jsonl$", path)
        if m:
            rows.append({"id": m.group(1), "rollout_path": path})
    return rows


def codex_processes():
    """Codex CLI processes (not the shared app-server daemon): pid -> (cwd, open files)."""
    procs = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        try:
            with open("/proc/%d/comm" % pid) as f:
                if f.read().strip() != "codex":
                    continue
            with open("/proc/%d/cmdline" % pid, "rb") as f:
                if b"app-server" in f.read():
                    continue
            cwd = os.readlink("/proc/%d/cwd" % pid)
        except OSError:
            continue
        files = set()
        try:
            for fd in os.listdir("/proc/%d/fd" % pid):
                try:
                    files.add(os.readlink("/proc/%d/fd/%s" % (pid, fd)))
                except OSError:
                    pass
        except OSError:
            pass
        procs[pid] = (cwd, files)
    return procs


# ---------------------------------------------------------------- windows

def hypr_json(*args):
    try:
        out = subprocess.run(["hyprctl", "-j"] + list(args), capture_output=True, text=True, timeout=3).stdout
        return json.loads(out)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def strip_spinner(title):
    return re.sub(r"^[^\w(\[]+", "", title or "").strip()


def find_window(session, clients=None):
    """The Hyprland client hosting a session: the window whose process is an
    ancestor of the agent. One terminal process can own many windows (Ghostty),
    so prefer the window whose title the agent set. Returns (client, exact)."""
    if not session.get("pid"):
        return None, False
    clients = clients if clients is not None else hypr_json("clients") or []
    chain = set(ancestors(session["pid"]))
    cands = [c for c in clients if c.get("pid") in chain]
    if not cands:
        return None, False
    want = session.get("titleMatch") or session.get("title")
    for c in cands:
        if want and strip_spinner(c.get("title")) == want:
            return c, True
    if len(cands) == 1:
        return cands[0], True
    cands.sort(key=lambda c: c.get("focusHistoryID", 99))
    return cands[0], False


def focus_window(client):
    if not client or not client.get("address"):
        return False
    target = "address:" + client["address"]
    # Hyprland 0.55+ takes Lua dispatchers; older releases the classic form.
    for args in (["hyprctl", "dispatch", 'hl.dsp.focus({ window = "%s" })' % target],
                 ["hyprctl", "dispatch", "focuswindow", target]):
        try:
            out = subprocess.run(args, capture_output=True, text=True, timeout=3)
        except (OSError, subprocess.SubprocessError):
            return False
        if out.returncode == 0 and "error" not in (out.stdout + out.stderr).lower():
            return True
    return False


def is_focused(session):
    active = hypr_json("activewindow") or {}
    if not active.get("address"):
        return False
    win, exact = find_window(session)
    return bool(exact and win and win.get("address") == active["address"])


# ---------------------------------------------------------------- tracker

class Tracker:
    def __init__(self):
        self.claude = {}
        self.codex = {}

    def scan(self):
        now = time.time()
        reg = claude_registry()
        for pid in list(self.claude):
            if pid not in reg:
                del self.claude[pid]
        for pid, r in reg.items():
            self.claude.setdefault(pid, ClaudeSession(pid)).update(r, now)

        rows = codex_rows(now)
        procs = codex_processes()
        locks = " ".join(os.listdir(os.path.join(CODEX_DIR, "thread-writer-locks"))) \
            if os.path.isdir(os.path.join(CODEX_DIR, "thread-writer-locks")) else ""
        live = {}
        claimed = set()
        for row in rows:          # newest first
            th = self.codex.get(row["id"]) or CodexThread(row)
            th.row = row
            th.update()
            pid = next((p for p, (_, files) in procs.items() if row["rollout_path"] in files), None)
            if pid is None:
                pid = next((p for p, (cwd, _) in procs.items() if cwd == th.cwd and p not in claimed), None)
            th.pid = pid
            if pid:
                claimed.add(pid)
            fresh = now - th.tail.mtime < CODEX_LINGER
            if pid or row["id"] in locks or fresh:
                live[row["id"]] = th
        self.codex = live

        sessions = [s.snapshot(now) for s in self.claude.values()] + [t.snapshot(now) for t in self.codex.values()]
        order = {"waiting": 0, "working": 1, "idle": 2}
        sessions.sort(key=lambda s: (order[s["state"]], s["since"] if s["state"] != "idle" else -s["since"]))
        return sessions


def summarize(sessions):
    counts = {"waiting": 0, "working": 0, "idle": 0}
    for s in sessions:
        counts[s["state"]] += 1
    return counts


def pick_next(sessions):
    waiting = [s for s in sessions if s["state"] == "waiting"]
    if waiting:
        return min(waiting, key=lambda s: s["since"])
    idle = [s for s in sessions if s["state"] == "idle"]
    return max(idle, key=lambda s: s["since"]) if idle else (sessions[0] if sessions else None)


# ---------------------------------------------------------------- notifications
#
# Notifications go straight to the notification server over the session bus.
# notify-send would put the text (an agent's last reply) on its command line,
# where any local user can read it from /proc/<pid>/cmdline.

DBUS_ALIGN = {"y": 1, "b": 4, "i": 4, "u": 4, "x": 8, "t": 8, "d": 8,
              "s": 4, "o": 4, "g": 1, "v": 1, "a": 4, "(": 8, "{": 8}
DBUS_FIXED = {"y": "B", "b": "I", "i": "i", "u": "I", "x": "q", "t": "Q", "d": "d"}


def dbus_types(sig):
    """Split a signature into its complete types: "sa{sv}i" -> s, a{sv}, i."""
    out, i = [], 0
    while i < len(sig):
        j = i
        while sig[j] == "a":
            j += 1
        if sig[j] in "({":
            depth = 0
            while True:
                depth += sig[j] in "({"
                depth -= sig[j] in ")}"
                j += 1
                if not depth:
                    break
        else:
            j += 1
        out.append(sig[i:j])
        i = j
    return out


def dbus_pad(buf, n):
    buf.extend(b"\0" * (-len(buf) % n))


def dbus_write(buf, sig, val):
    """Append one value of complete type `sig`; variants are (signature, value)."""
    c = sig[0]
    if c in DBUS_FIXED:
        dbus_pad(buf, DBUS_ALIGN[c])
        buf.extend(struct.pack("<" + DBUS_FIXED[c], val))
    elif c in "so":
        raw = val.encode()
        dbus_pad(buf, 4)
        buf.extend(struct.pack("<I", len(raw)) + raw + b"\0")
    elif c == "g":
        buf.extend(bytes([len(val)]) + val.encode() + b"\0")
    elif c == "v":
        dbus_write(buf, "g", val[0])
        dbus_write(buf, val[0], val[1])
    elif c == "a":
        dbus_pad(buf, 4)
        at = len(buf)
        buf.extend(b"\0\0\0\0")
        dbus_pad(buf, DBUS_ALIGN[sig[1]])
        start = len(buf)
        for item in (val.items() if sig[1] == "{" else val):
            dbus_write(buf, sig[1:], item)
        struct.pack_into("<I", buf, at, len(buf) - start)
    else:
        dbus_pad(buf, 8)
        for t, v in zip(dbus_types(sig[1:-1]), val):
            dbus_write(buf, t, v)


def dbus_read(data, pos, sig, end="<"):
    """One value of complete type `sig` at `pos` -> (value, new pos)."""
    c = sig[0]
    pos += -pos % DBUS_ALIGN[c]
    if c in DBUS_FIXED:
        fmt = end + DBUS_FIXED[c]
        return struct.unpack_from(fmt, data, pos)[0], pos + struct.calcsize(fmt)
    if c in "so":
        n = struct.unpack_from(end + "I", data, pos)[0]
        return data[pos + 4:pos + 4 + n].decode("utf-8", "replace"), pos + 5 + n
    if c == "g":
        n = data[pos]
        return data[pos + 1:pos + 1 + n].decode(), pos + 2 + n
    if c == "v":
        inner, pos = dbus_read(data, pos, "g", end)
        return dbus_read(data, pos, inner, end)
    if c == "a":
        n = struct.unpack_from(end + "I", data, pos)[0]
        pos += 4
        pos += -pos % DBUS_ALIGN[sig[1]]
        stop, items = pos + n, []
        while pos < stop:
            item, pos = dbus_read(data, pos, sig[1:], end)
            items.append(item)
        return (dict(items) if sig[1] == "{" else items), pos
    vals = []
    for t in dbus_types(sig[1:-1]):
        v, pos = dbus_read(data, pos, t, end)
        vals.append(v)
    return tuple(vals), pos


class SessionBus:
    """Just enough of the D-Bus wire protocol to call methods and hear signals."""

    def __init__(self, match, on_signal):
        self.match = match              # AddMatch rule for the signals we want
        self.on_signal = on_signal      # (interface, member, args)
        self.sock = None
        self.serial = 0
        self.pending = {}               # serial -> [Event, reply args, error name]
        self.lock = threading.RLock()   # connect() calls Hello while holding it

    def connect(self):
        addrs = os.environ.get("DBUS_SESSION_BUS_ADDRESS") or \
            "unix:path=%s/bus" % (os.environ.get("XDG_RUNTIME_DIR") or "/run/user/%d" % os.getuid())
        for addr in addrs.split(";"):
            kind, _, rest = addr.partition(":")
            opts = dict(kv.split("=", 1) for kv in rest.split(",") if "=" in kv)
            if kind != "unix" or not ("path" in opts or "abstract" in opts):
                continue
            path = opts.get("path") or "\0" + opts["abstract"]
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(5)
            try:
                sock.connect(path)
                sock.sendall(b"\0AUTH EXTERNAL " + str(os.getuid()).encode().hex().encode() + b"\r\n")
                reply = b""
                while not reply.endswith(b"\r\n"):
                    chunk = sock.recv(256)
                    if not chunk:
                        raise OSError("bus closed during auth")
                    reply += chunk
                if not reply.startswith(b"OK"):
                    raise OSError("bus refused auth")
                sock.sendall(b"BEGIN\r\n")
            except OSError:
                sock.close()
                continue
            sock.settimeout(None)
            self.sock = sock
            threading.Thread(target=self.reader, args=(sock,), daemon=True).start()
            try:
                for member, sig, args in (("Hello", "", ()), ("AddMatch", "s", [self.match])):
                    self.call("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                              member, sig, args)
            except OSError:
                self.drop()
                raise
            return
        raise OSError("no session bus")

    def call(self, dest, path, iface, member, sig="", args=(), reply=True):
        body = bytearray()
        for t, v in zip(dbus_types(sig), args):
            dbus_write(body, t, v)
        fields = [(1, ("o", path)), (2, ("s", iface)), (3, ("s", member)), (6, ("s", dest))]
        if sig:
            fields.append((8, ("g", sig)))
        with self.lock:
            if self.sock is None:
                self.connect()
            self.serial += 1
            serial = self.serial
            msg = bytearray(struct.pack("<cBBBII", b"l", 1, 0 if reply else 1, 1, len(body), serial))
            dbus_write(msg, "a(yv)", fields)
            dbus_pad(msg, 8)
            waiter = self.pending[serial] = [threading.Event(), None, None] if reply else None
            try:
                self.sock.sendall(msg + body)
            except OSError:
                self.drop()
                raise
        if not reply:
            return None
        if not waiter[0].wait(5):
            self.pending.pop(serial, None)
            raise OSError("%s timed out" % member)
        if waiter[2]:
            raise OSError(waiter[2])
        return waiter[1]

    def drop(self):
        sock, self.sock = self.sock, None
        if sock:
            sock.close()
        for waiter in self.pending.values():
            if waiter:
                waiter[2] = "bus connection lost"
                waiter[0].set()
        self.pending.clear()

    def reader(self, sock):
        buf = b""

        def need(n):
            nonlocal buf
            while len(buf) < n:
                chunk = sock.recv(65536)
                if not chunk:
                    raise OSError("bus closed")
                buf += chunk

        try:
            while True:
                need(16)
                end = "<" if buf[:1] == b"l" else ">"
                kind = buf[1]
                body_len, _, fields_len = struct.unpack_from(end + "III", buf, 4)
                start = 16 + fields_len + (-fields_len % 8)
                need(start + body_len)
                raw, buf = buf[:start + body_len], buf[start + body_len:]
                fields = dict(dbus_read(raw, 12, "a(yv)", end)[0])
                args, pos = [], start
                for t in dbus_types(fields.get(8, "")):
                    v, pos = dbus_read(raw, pos, t, end)
                    args.append(v)
                if kind in (2, 3):          # method return, error
                    waiter = self.pending.pop(fields.get(5), None)
                    if waiter:
                        waiter[1], waiter[2] = args, (fields.get(4) if kind == 3 else None)
                        waiter[0].set()
                elif kind == 4:             # signal
                    self.on_signal(fields.get(2), fields.get(3), args)
        except (OSError, struct.error, ValueError, IndexError):
            with self.lock:
                if self.sock is sock:
                    self.drop()


class Notifier:
    DEST = "org.freedesktop.Notifications"
    PATH = "/org/freedesktop/Notifications"
    URGENCY = {"low": 0, "normal": 1, "critical": 2}

    def __init__(self, focus):
        self.focus = focus
        self.bus = SessionBus("type='signal',interface='%s'" % self.DEST, self.on_signal)
        self.ids = {}             # session id -> notification id, so updates replace
        self.owners = {}          # notification id -> session id, for clicks

    def icon(self, provider):
        path = os.path.join(ASSETS, provider + ".svg")
        return path if os.path.exists(path) else "dialog-information"

    def send(self, session, summary, body, urgency="normal"):
        sid = session["id"]
        hints = {"urgency": ("y", self.URGENCY[urgency]), "x-grivera-agents": ("s", sid)}
        try:
            nid = self.bus.call(self.DEST, self.PATH, self.DEST, "Notify", "susssasa{sv}i",
                                [PROVIDERS[session["provider"]], self.ids.get(sid, 0),
                                 self.icon(session["provider"]), summary, body,
                                 ["default", "Focus"], hints, -1])[0]
        except (OSError, IndexError):
            return
        self.ids[sid] = nid
        self.owners[nid] = sid

    def close(self, session_id):
        nid = self.ids.pop(session_id, None)
        if nid:
            self.owners.pop(nid, None)
            try:
                self.bus.call(self.DEST, self.PATH, self.DEST, "CloseNotification", "u", [nid], reply=False)
            except OSError:
                pass

    def on_signal(self, iface, member, args):
        if iface != self.DEST or not args:
            return
        if member == "ActionInvoked" and args[1:2] == ["default"]:
            sid = self.owners.get(args[0])
            if sid:
                threading.Thread(target=self.focus, args=(sid,), daemon=True).start()
        elif member == "NotificationClosed":
            sid = self.owners.pop(args[0], None)
            if sid and self.ids.get(sid) == args[0]:
                del self.ids[sid]


def human_duration(seconds):
    seconds = int(max(0, seconds))
    if seconds < 60:
        return "%ds" % seconds
    if seconds < 3600:
        return "%dm %02ds" % (seconds // 60, seconds % 60) if seconds < 600 else "%dm" % (seconds // 60)
    return "%dh %02dm" % (seconds // 3600, seconds % 3600 // 60)


def check_transitions(prev, sessions, config, notifier):
    now = time.time()
    current = {s["id"]: s for s in sessions}
    for sid, s in current.items():
        before = prev.get(sid)
        if before is None or before["state"] == s["state"]:
            continue
        if before["state"] == "waiting":
            notifier.close(sid)
        # Titles come from prompts, so by default notifications name only the agent.
        details = config.get("notifyDetails")
        who = s["title"] if details else PROVIDERS[s["provider"]]
        if s["state"] == "waiting" and config.get("notifyWaiting"):
            if not is_focused(s):
                why = (s.get("waitingFor") or "input needed") if details else "waiting for you"
                notifier.send(s, "%s needs you" % who, why[:1].upper() + why[1:] + (" · " + s["cwd"] if details else ""))
        elif s["state"] == "idle" and before["state"] == "working" and config.get("notifyFinished"):
            took = now - (s.get("turnStart") or before["since"])
            if took >= config.get("minTurnSeconds", 20) and not is_focused(s):
                reply = s.get("lastText") if config.get("notifyReplies") else None
                notifier.send(s, "%s finished" % who,
                              (reply or "Turn complete") + "\n" + human_duration(took) + (" · " + s["cwd"] if details else ""))
    for sid in prev:
        if sid not in current:
            notifier.close(sid)


# ---------------------------------------------------------------- daemon

def daemon():
    lock = threading.Lock()
    tracker = Tracker()
    config = dict(DEFAULT_CONFIG, **load_json(CONFIG_FILE, {}))
    wake = threading.Event()
    shared = {"sessions": []}

    def emit(obj):
        with lock:
            try:
                sys.stdout.write(json.dumps(obj, separators=(",", ":")) + "\n")
                sys.stdout.flush()
            except BrokenPipeError:
                os._exit(0)

    def focus(sid):
        s = next((x for x in shared["sessions"] if x["id"] == sid), None)
        if s is None and sid == "next":
            s = pick_next(shared["sessions"])
        win = find_window(s)[0] if s else None
        return focus_window(win)

    notifier = Notifier(focus)

    def poller():
        last = None
        prev = None
        while True:
            try:
                sessions = tracker.scan()
            except Exception as exc:  # keep the bar alive on a format change
                emit({"type": "log", "error": "scan failed: %s" % exc})
                sessions = shared["sessions"]
            shared["sessions"] = sessions
            if prev is not None:
                check_transitions(prev, sessions, config, notifier)
            prev = {s["id"]: s for s in sessions}
            state = {"sessions": sessions, "counts": summarize(sessions),
                     "burn": sum(s["burn"] for s in sessions), "config": config}
            blob = json.dumps(state, sort_keys=True)
            if blob != last:
                last = blob
                emit({"type": "state", "state": state})
            busy = any(s["state"] != "idle" for s in sessions)
            wake.wait(POLL_BUSY if busy else POLL_IDLE)
            wake.clear()

    threading.Thread(target=poller, daemon=True).start()

    for line in sys.stdin:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        cmd, ok, err = msg.get("cmd"), True, None
        if cmd == "refresh":
            wake.set()
        elif cmd == "focus":
            ok = focus(msg.get("session") or "next")
            err = None if ok else "no window found for that session"
        elif cmd == "config":
            for key in DEFAULT_CONFIG:
                if key in msg:
                    config[key] = type(DEFAULT_CONFIG[key])(msg[key])
            save_json(CONFIG_FILE, config)
            wake.set()
        elif cmd == "test":
            demo = {"id": "test", "provider": "claude", "title": "grivera.agents", "cwd": "~"}
            notifier.send(demo, "Notifications work", "This is what a finished session looks like.")
        else:
            ok, err = False, "unknown command"
        emit({"type": "result", "id": msg.get("id"), "cmd": cmd, "ok": ok, "error": err})


# ---------------------------------------------------------------- CLI

def fmt_tokens(n):
    n = n or 0
    if n >= 1e6:
        return "%.1fM" % (n / 1e6)
    if n >= 1e3:
        return "%.1fk" % (n / 1e3)
    return str(int(n))


def print_status(sessions):
    if not sessions:
        print("No Claude Code or Codex sessions running.")
        return
    now = time.time()
    for s in sessions:
        state = s["state"] + (" (%s)" % s["waitingFor"] if s.get("waitingFor") else "")
        ctx = fmt_tokens(s["context"]) + (" / " + fmt_tokens(s["contextWindow"]) if s.get("contextWindow") else "")
        print("%-11s %-34s %-26s %s" % (PROVIDERS[s["provider"]], s["title"][:34], state, human_duration(now - s["since"])))
        print("            %s · %s · ctx %s · %s fresh tokens · %s/min" % (
            s["cwd"], s.get("model") or "?", ctx, fmt_tokens(s["tokens"]["fresh"]), fmt_tokens(s["burn"])))


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "status"
    if cmd == "daemon":
        daemon()
    elif cmd == "status":
        sessions = Tracker().scan()
        if "--json" in argv:
            print(json.dumps({"sessions": sessions, "counts": summarize(sessions)}, indent=2))
        else:
            print_status(sessions)
    elif cmd == "focus":
        sessions = Tracker().scan()
        target = argv[2] if len(argv) > 2 else "next"
        s = pick_next(sessions) if target == "next" else next(
            (x for x in sessions if target in (x["id"], x["sessionId"], str(x["pid"]))), None)
        if not s:
            print("No matching session.", file=sys.stderr)
            return 1
        if not focus_window(find_window(s)[0]):
            print("Couldn't find a window for %s." % s["title"], file=sys.stderr)
            return 1
    else:
        print(__doc__.strip())
        return 0 if cmd in ("-h", "--help", "help") else 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv) or 0)
