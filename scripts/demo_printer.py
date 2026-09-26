"""Terminal printer for the section 10d demo. Pure formatting: no network, no model, no Atlas.

Every line the demo shows is an event dict that goes through Printer.handle(), both in a live run and in a
--dry-run replay of a recorded run, so rehearsal output matches the real thing.

Event types: banner, section, kv, info, note, warn, error, stream, gate, episode, decision, table, vector.
Each event carries "t" (epoch seconds) for the timestamp column.
"""

from __future__ import annotations

import re
import sys
import time
from datetime import datetime, timezone
from typing import Any, TextIO

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

COLORS = {"red": "31", "green": "32", "yellow": "33", "blue": "34", "magenta": "35", "cyan": "36",
          "grey": "90", "bold": "1", "bold_red": "1;31", "bold_green": "1;32", "bold_magenta": "1;35",
          "bold_cyan": "1;36", "bold_yellow": "1;33"}

WIDTH = 100


def strip_ansi(s: str) -> str:
    return ANSI_RE.sub("", s)


def short_task(task: str) -> str:
    return (task or "").rsplit("/", 1)[-1]


def fit(s: Any, n: int) -> str:
    """Pad or cut to exactly n characters (cut marked with '~')."""
    s = "" if s is None else str(s)
    s = s.replace("\n", " ")
    if len(s) > n:
        return s[: max(0, n - 1)] + "~"
    return s.ljust(n)


def role_label(role: str) -> str:
    return {"champion": "CHAMPION", "candidate": "CANDIDATE", "redteam": "RED-TEAM"}.get(role, (role or "").upper())


class Printer:
    def __init__(self, out: TextIO | None = None, color: bool = True, transcript: TextIO | None = None) -> None:
        self.out = out or sys.stdout
        self.color = color
        self.transcript = transcript  # receives the ANSI-stripped text
        self.lines: list[str] = []

    # ------------------------------------------------------------------ primitives
    def c(self, s: str, color: str | None) -> str:
        if not self.color or not color:
            return s
        return f"\x1b[{COLORS[color]}m{s}\x1b[0m"

    def ts(self, t: float | None) -> str:
        t = time.time() if t is None else t
        return datetime.fromtimestamp(t, timezone.utc).strftime("%H:%M:%S")

    def emit_line(self, s: str) -> None:
        self.out.write(s + "\n")
        self.out.flush()
        plain = strip_ansi(s)
        self.lines.append(plain)
        if self.transcript is not None:
            self.transcript.write(plain + "\n")
            self.transcript.flush()

    def row(self, ev: dict[str, Any], src: str, msg: str, src_color: str | None = None) -> None:
        self.emit_line(f"{self.c(self.ts(ev.get('t')), 'grey')}  {self.c(fit(src, 7), src_color)} {msg}")

    # ------------------------------------------------------------------ dispatch
    def handle(self, ev: dict[str, Any]) -> None:
        fn = getattr(self, "_" + str(ev.get("type")), None)
        if fn is None:
            return
        fn(ev)

    def _banner(self, ev: dict[str, Any]) -> None:
        bar = "=" * WIDTH
        self.emit_line(self.c(bar, "bold_cyan"))
        for line in str(ev.get("text", "")).split("\n"):
            self.emit_line(self.c(line.center(WIDTH).rstrip(), "bold"))
        self.emit_line(self.c(bar, "bold_cyan"))

    def _section(self, ev: dict[str, Any]) -> None:
        title = f"-- {ev.get('text', '')} "
        self.emit_line("")
        self.emit_line(self.c(title + "-" * max(0, WIDTH - len(title)), "bold_cyan"))

    def _kv(self, ev: dict[str, Any]) -> None:
        self.emit_line(f"  {self.c(fit(ev.get('key', ''), 22), 'bold')} {ev.get('value', '')}")

    def _info(self, ev: dict[str, Any]) -> None:
        self.row(ev, ev.get("src", "DEMO"), str(ev.get("text", "")), "cyan")

    def _note(self, ev: dict[str, Any]) -> None:
        self.row(ev, ev.get("src", "NOTE"), self.c(str(ev.get("text", "")), "yellow"), "yellow")

    def _warn(self, ev: dict[str, Any]) -> None:
        self.row(ev, ev.get("src", "WARN"), self.c(str(ev.get("text", "")), "bold_yellow"), "bold_yellow")

    def _error(self, ev: dict[str, Any]) -> None:
        self.row(ev, "ERROR", self.c(str(ev.get("text", "")), "bold_red"), "bold_red")

    def _stream(self, ev: dict[str, Any]) -> None:
        old = ev.get("old") or "(new)"
        new = str(ev.get("new"))
        col = ("bold_red" if new.startswith(("rejected", "denied", "violating", "invalid", "error", "timeout"))
               else "bold_green" if new in ("promoted", "passed") else "magenta")
        coll = fit(ev.get("coll", ""), 8)
        ident = fit(ev.get("id", ""), 44)
        arm = fit(ev.get("arm", ""), 9)
        self.row(ev, "ATLAS", f"{self.c('change', 'magenta')} {coll} {ident} {arm} "
                              f"{fit(old, 10)} -> {self.c(new, col)}", "bold_magenta")

    def _gate(self, ev: dict[str, Any]) -> None:
        pols = ev.get("policies") or []
        if not pols:
            verdict, vcol = "ALLOWED", "green"
        elif ev.get("denied"):
            verdict, vcol = "DENIED", "bold_red"
        else:
            verdict, vcol = "VIOLATES", "bold_red"  # observe mode: recorded, not blocked
        who = fit(role_label(ev.get("role", "")), 9)
        task = fit(short_task(ev.get("task", "")), 11)
        kind = fit(ev.get("kind", ""), 15)
        path = ev.get("path", "") or ""
        if ev.get("dst"):
            path = f"{path} -> {ev['dst']}"
        path = fit(path, 32)
        pol = fit(",".join(pols) if pols else "-", 29)
        lat = ev.get("latency_ms")
        lat_s = f"{lat:7.1f} ms" if isinstance(lat, (int, float)) else "      - ms"
        whocol = "bold_red" if ev.get("role") == "redteam" else None
        self.row(ev, "GATE", f"{self.c(who, whocol)} {task} {self.c(fit(verdict, 8), vcol)} {kind} {path} "
                             f"{self.c(pol, 'red' if pols else 'grey')} {lat_s}", "blue")

    def _episode(self, ev: dict[str, Any]) -> None:
        st = str(ev.get("status", ""))
        col = {"passed": "bold_green", "failed": "yellow", "violating": "bold_red", "denied": "bold_red"}.get(st, "red")
        who = fit(role_label(ev.get("role", "")), 9)
        extra = ev.get("detail", "")
        self.row(ev, "EPISODE", f"{self.c(who, 'bold_red' if ev.get('role') == 'redteam' else None)} "
                                f"{fit(short_task(ev.get('task', '')), 11)} {self.c(fit(st.upper(), 9), col)} "
                                f"{fit(ev.get('summary', ''), 60)} {extra}".rstrip(), "cyan")

    def _decision(self, ev: dict[str, Any]) -> None:
        st = str(ev.get("status", ""))
        col = "bold_green" if st in ("promoted", "passed_gates") else "bold_red"
        who = role_label(ev.get("role", ""))
        self.row(ev, "DECIDE", f"{self.c(fit(who, 9), 'bold')} {fit(ev.get('edit_id', ''), 34)} "
                               f"{self.c(st.upper(), col)}", "bold")
        for line in _wrap(str(ev.get("reason", "")), WIDTH - 20):
            self.emit_line(" " * 20 + line)

    def _table(self, ev: dict[str, Any]) -> None:
        headers = [str(h) for h in ev.get("headers", [])]
        rows = [[("" if v is None else str(v)) for v in r] for r in ev.get("rows", [])]
        widths = [len(h) for h in headers]
        for r in rows:
            for i, v in enumerate(r):
                if i < len(widths):
                    widths[i] = min(max(widths[i], len(v)), 44)
        if ev.get("title"):
            self.emit_line(self.c(str(ev["title"]), "bold"))
        self.emit_line("  " + "  ".join(self.c(fit(h, w), "bold") for h, w in zip(headers, widths)))
        self.emit_line("  " + "  ".join("-" * w for w in widths))
        for r in rows:
            self.emit_line("  " + "  ".join(fit(v, w) for v, w in zip(r, widths)))
        for n in ev.get("notes", []) or []:
            self.emit_line(self.c(f"  {n}", "grey"))

    def _vector(self, ev: dict[str, Any]) -> None:
        self.row(ev, "VECTOR", f"query intent: \"{ev.get('query', '')}\"", "bold_magenta")
        for i, h in enumerate(ev.get("hits", []) or [], 1):
            mark = self.c("<- just rejected RED-TEAM edit", "bold_red") if h.get("this_run_redteam") else ""
            self.emit_line(f"{' ' * 18}{i}. score {h.get('score', 0):.3f}  {fit(h.get('id', ''), 34)} "
                           f"{fit(h.get('status', ''), 18)} {mark}")
            self.emit_line(f"{' ' * 22}reason: {fit(h.get('reason', ''), WIDTH - 30)}")


def _wrap(s: str, n: int) -> list[str]:
    words, lines, cur = s.split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > n and cur:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return lines
