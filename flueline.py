#!/usr/bin/env python3
"""Flueline: offline chimney and flue service planner.

Reads one plain-text flue file and reports, for every appliance, when its
next sweep is due, whether it is out of service after a failed inspection,
how close its flue sits to combustibles, and how much soot risk has built up.
Standard library only; no network, no accounts.
"""

import argparse
import calendar
import datetime as dt
import json
import re
import shlex
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass, field

# Placeholder defaults. Set hours_limit= and min_clearance_cm= to the figures
# in your appliance manual and local rules; these are not safety standards.
FUEL_HOURS_LIMIT = {"wood": 150.0, "coal": 150.0, "pellet": 400.0, "oil": None, "gas": None}
APPLIANCE_TYPES = ("stove", "insert", "boiler", "fireplace", "heater")
DEFAULT_INTERVAL_MONTHS = 12
DEFAULT_WARN_DAYS = 30
DEFAULT_MIN_CLEARANCE_CM = 60.0
HOURS_WARN_FRACTION = 0.9
STATES = ("ok", "due-soon", "overdue", "out-of-service")
SOOT_BANDS = ((4, "low"), (6, "moderate"), (8, "high"))  # score below bound; else critical

DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]*")
CONFIG_OPTIONS = {"today", "warn_days", "min_clearance_cm"}
FLUE_OPTIONS = {"fuel", "type", "clearance_cm", "interval_months", "hours_limit"}
EVENT_OPTIONS = {"sweep": {"soot", "by"}, "burn": {"hours"}, "inspect": {"result", "note"}}

EXAMPLE = """\
# Flueline plan. One directive per line; '#' starts a comment.
config today=2026-10-19 warn_days=30 min_clearance_cm=60

flue stove-lounge fuel=wood type=stove clearance_cm=75
flue insert-bedroom fuel=wood type=insert clearance_cm=45
flue boiler-garage fuel=oil type=boiler
flue hob-kitchen fuel=gas type=heater

sweep stove-lounge 2025-12-01 soot=7 by="Dan Hall"
sweep stove-lounge 2026-02-20 soot=5 by="Dan Hall"
burn stove-lounge 2025-12-15 hours=55
burn stove-lounge 2026-03-14 hours=40
burn stove-lounge 2026-10-02 hours=72

sweep insert-bedroom 2025-11-05 soot=2 by="Ana Reyes"
burn insert-bedroom 2026-01-12 hours=30

sweep boiler-garage 2025-09-01 soot=1 by="Lee Park"

sweep hob-kitchen 2026-04-01 by="Lee Park"
inspect hob-kitchen 2026-08-30 result=fail note="cracked flue joint"
"""


class LineError(Exception):
    """A problem with one line of the plan file."""


@dataclass
class Flue:
    name: str
    line: int
    fuel: str
    kind: str
    clearance_cm: float | None
    interval_months: int
    hours_limit: float | None


@dataclass
class Event:
    line: int
    kind: str  # sweep, burn or inspect
    name: str
    date: dt.date
    soot: float | None = None
    hours: float | None = None
    result: str | None = None
    note: str = ""
    by: str = ""


@dataclass
class Finding:
    line: int
    level: str  # "error" or "warning"
    message: str


@dataclass
class Plan:
    config: dict = field(default_factory=dict)
    flues: dict = field(default_factory=dict)
    events: list = field(default_factory=list)
    findings: list = field(default_factory=list)

    def error(self, line, message):
        self.findings.append(Finding(line, "error", message))


@dataclass
class Status:
    name: str
    fuel: str
    kind: str
    state: str
    next_due: dt.date | None
    hours_since_sweep: float | None
    hours_limit: float | None
    soot_score: float | None
    soot_label: str
    notes: list

    def to_dict(self):
        data = asdict(self)
        data["next_due"] = self.next_due.isoformat() if self.next_due else None
        return data


# --- value parsing -----------------------------------------------------------

def parse_date(text):
    if not DATE_RE.fullmatch(text):
        raise LineError(f"date '{text}' must look like YYYY-MM-DD")
    try:
        return dt.date.fromisoformat(text)
    except ValueError:
        raise LineError(f"'{text}' is not a real calendar date") from None


def parse_number(text, label, low, high):
    try:
        value = float(text)
    except ValueError:
        raise LineError(f"{label} '{text}' is not a number") from None
    if not low <= value <= high:
        raise LineError(f"{label} {text} must be between {low:g} and {high:g}")
    return value


def parse_int(text, label, low, high):
    if not text.isdigit():
        raise LineError(f"{label} '{text}' must be a whole number")
    value = int(text)
    if not low <= value <= high:
        raise LineError(f"{label} {value} must be between {low} and {high}")
    return value


def split_args(args, allowed):
    positional, options = [], {}
    for token in args:
        key, sep, value = token.partition("=")
        if not sep:
            positional.append(token)
        elif key not in allowed:
            raise LineError(f"unknown option '{key}'")
        elif key in options:
            raise LineError(f"option '{key}' given twice")
        else:
            options[key] = value
    return positional, options


def single_name(positional):
    if len(positional) != 1:
        raise LineError("expected exactly one flue name")
    name = positional[0]
    if not NAME_RE.fullmatch(name):
        raise LineError(f"flue name '{name}' must be lowercase letters, digits and hyphens")
    return name


# --- directives --------------------------------------------------------------

def directive_config(plan, line, args):
    positional, opts = split_args(args, CONFIG_OPTIONS)
    if positional:
        raise LineError("config takes only key=value options")
    if "today" in opts:
        plan.config["today"] = parse_date(opts["today"])
    if "warn_days" in opts:
        plan.config["warn_days"] = parse_int(opts["warn_days"], "warn_days", 0, 365)
    if "min_clearance_cm" in opts:
        plan.config["min_clearance_cm"] = parse_number(
            opts["min_clearance_cm"], "min_clearance_cm", 0, 1000)


def directive_flue(plan, line, args):
    positional, opts = split_args(args, FLUE_OPTIONS)
    name = single_name(positional)
    if name in plan.flues:
        raise LineError(f"flue '{name}' is already defined on line {plan.flues[name].line}")
    fuel = opts.get("fuel")
    if fuel not in FUEL_HOURS_LIMIT:
        raise LineError(f"fuel must be one of: {', '.join(FUEL_HOURS_LIMIT)}")
    kind = opts.get("type", "stove")
    if kind not in APPLIANCE_TYPES:
        raise LineError(f"type must be one of: {', '.join(APPLIANCE_TYPES)}")
    clearance = None
    if "clearance_cm" in opts:
        clearance = parse_number(opts["clearance_cm"], "clearance_cm", 0, 1000)
    interval = DEFAULT_INTERVAL_MONTHS
    if "interval_months" in opts:
        interval = parse_int(opts["interval_months"], "interval_months", 1, 120)
    limit = FUEL_HOURS_LIMIT[fuel]
    if "hours_limit" in opts:
        raw = opts["hours_limit"]
        limit = None if raw == "none" else parse_number(raw, "hours_limit", 1, 10000)
    plan.flues[name] = Flue(name, line, fuel, kind, clearance, interval, limit)


def event_directive(kind):
    def handler(plan, line, args):
        positional, opts = split_args(args, EVENT_OPTIONS[kind])
        if len(positional) != 2:
            raise LineError(f"{kind} needs a flue name and a date")
        name = single_name(positional[:1])
        event = Event(line, kind, name, parse_date(positional[1]))
        if kind == "sweep":
            if "soot" in opts:
                event.soot = parse_number(opts["soot"], "soot", 0, 10)
            event.by = opts.get("by", "")
        elif kind == "burn":
            if "hours" not in opts:
                raise LineError("burn needs hours=")
            event.hours = parse_number(opts["hours"], "hours", 0.1, 1000)
        else:
            if opts.get("result") not in ("pass", "fail"):
                raise LineError("inspect needs result=pass or result=fail")
            event.result = opts["result"]
            event.note = opts.get("note", "")
        plan.events.append(event)
    return handler


DIRECTIVES = {
    "config": directive_config,
    "flue": directive_flue,
    "sweep": event_directive("sweep"),
    "burn": event_directive("burn"),
    "inspect": event_directive("inspect"),
}


def parse_text(text):
    plan = Plan()
    for number, raw in enumerate(text.splitlines(), start=1):
        try:
            tokens = shlex.split(raw, comments=True)
        except ValueError as exc:
            plan.error(number, f"unbalanced quotation marks ({exc})")
            continue
        if not tokens:
            continue
        verb, *args = tokens
        try:
            handler = DIRECTIVES.get(verb)
            if handler is None:
                raise LineError(f"unknown directive '{verb}'")
            handler(plan, number, args)
        except LineError as exc:
            plan.error(number, str(exc))
    return plan


# --- planning maths ----------------------------------------------------------

def add_months(day, months):
    index = day.month - 1 + months
    year, month = day.year + index // 12, index % 12 + 1
    return dt.date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def worse(a, b):
    return max(a, b, key=STATES.index)


def soot_label(score):
    for bound, label in SOOT_BANDS:
        if score < bound:
            return label
    return "critical"


def evaluate(flue, events, today, warn_days, min_clearance):
    sweeps = sorted((e for e in events if e.kind == "sweep"), key=lambda e: e.date)
    burns = [e for e in events if e.kind == "burn"]
    inspections = sorted((e for e in events if e.kind == "inspect"), key=lambda e: e.date)
    last = sweeps[-1] if sweeps else None
    state, notes = "ok", []
    next_due = hours_since = None

    if last is None:
        state = "overdue"
        notes.append("never swept")
    else:
        next_due = add_months(last.date, flue.interval_months)
        hours_since = round(sum(b.hours for b in burns if b.date > last.date), 2)
        days_left = (next_due - today).days
        if days_left <= 0:
            state = "overdue"
            notes.append(f"sweep date passed on {next_due}")
        elif days_left <= warn_days:
            state = worse(state, "due-soon")
            notes.append(f"sweep due {next_due} ({days_left} days)")
        if flue.hours_limit:
            if hours_since >= flue.hours_limit:
                state = "overdue"
                notes.append(f"burned {hours_since:g} h of {flue.hours_limit:g} h since last sweep")
            elif hours_since >= HOURS_WARN_FRACTION * flue.hours_limit:
                state = worse(state, "due-soon")
                notes.append(f"{hours_since:g} of {flue.hours_limit:g} h burned since last sweep")

    if inspections and inspections[-1].result == "fail":
        failed = inspections[-1]
        state = "out-of-service"
        detail = f": {failed.note}" if failed.note else ""
        notes.append(f"failed inspection on {failed.date}{detail}")

    if flue.clearance_cm is not None and flue.clearance_cm < min_clearance:
        notes.append(f"clearance {flue.clearance_cm:g} cm is below {min_clearance:g} cm")

    soot_score, label = None, "unknown"
    if last is not None and last.soot is not None:
        burn_part = 0.0
        if flue.hours_limit and hours_since is not None:
            burn_part = 3 * min(hours_since / flue.hours_limit, 1)
        soot_score = round(min(10.0, last.soot + burn_part), 1)
        label = soot_label(soot_score)
        if label in ("high", "critical"):
            notes.append(f"soot risk {label}")

    return Status(flue.name, flue.fuel, flue.kind, state, next_due, hours_since,
                  flue.hours_limit, soot_score, label, notes)


def analyse(plan, today):
    findings = list(plan.findings)
    grouped = defaultdict(list)
    for event in plan.events:
        if event.name not in plan.flues:
            findings.append(Finding(event.line, "error",
                                    f"{event.kind} refers to unknown flue '{event.name}'"))
        elif event.date > today:
            findings.append(Finding(event.line, "warning",
                                    f"{event.kind} dated {event.date} is after {today} and is ignored"))
        else:
            grouped[event.name].append(event)
    warn_days = plan.config.get("warn_days", DEFAULT_WARN_DAYS)
    min_clearance = plan.config.get("min_clearance_cm", DEFAULT_MIN_CLEARANCE_CM)
    statuses = [evaluate(flue, grouped[name], today, warn_days, min_clearance)
                for name, flue in plan.flues.items()]
    findings.sort(key=lambda f: (f.line, f.message))
    return findings, statuses


# --- rendering ---------------------------------------------------------------

def render_table(headers, rows):
    cells = [[str(c) for c in headers]] + [[str(c) for c in row] for row in rows]
    widths = [max(len(row[i]) for row in cells) for i in range(len(headers))]
    lines = ["  ".join(c.ljust(w) for c, w in zip(row, widths)).rstrip() for row in cells]
    return "\n".join(lines)


def hours_text(status):
    if status.hours_since_sweep is None:
        return "-"
    if status.hours_limit:
        return f"{status.hours_since_sweep:g}/{status.hours_limit:g}"
    return f"{status.hours_since_sweep:g}"


def soot_text(status):
    if status.soot_score is None:
        return status.soot_label
    return f"{status.soot_label} ({status.soot_score:g})"


def format_findings(path, findings):
    return [f"{path}:{f.line}: {f.level}: {f.message}" for f in findings]


def format_check(path, today, findings, statuses):
    rows = [[s.name, s.fuel, s.state, s.next_due or "-", hours_text(s), soot_text(s),
             "; ".join(s.notes) or "-"] for s in statuses]
    out = [f"Flueline check: {path} (today {today})", ""]
    out.append(render_table(["NAME", "FUEL", "STATE", "NEXT DUE", "HOURS SINCE SWEEP",
                             "SOOT", "NOTES"], rows))
    if findings:
        out += ["", "Findings:"] + format_findings(path, findings)
    return "\n".join(out)


def ics_escape(text):
    return (text.replace("\\", "\\\\").replace(";", "\\;")
                .replace(",", "\\,").replace("\n", "\\n"))


def ics_fold(line):
    chunks = [line[:75]]
    rest = line[75:]
    while rest:
        chunks.append(" " + rest[:74])
        rest = rest[74:]
    return "\r\n".join(chunks)


def build_ics(statuses, today):
    stamp = today.strftime("%Y%m%dT000000Z")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//flueline//EN", "CALSCALE:GREGORIAN"]
    for s in statuses:
        day = max(s.next_due, today) if s.next_due else today
        description = "; ".join(s.notes) or "no open issues"
        lines += [
            "BEGIN:VEVENT",
            f"UID:{s.name}-{day:%Y%m%d}@flueline",
            f"DTSTAMP:{stamp}",
            f"DTSTART;VALUE=DATE:{day:%Y%m%d}",
            f"SUMMARY:Sweep {s.name} ({s.state})",
            f"DESCRIPTION:{ics_escape(description)}",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(ics_fold(line) for line in lines) + "\r\n"


# --- commands ----------------------------------------------------------------

def load(path, cli_today):
    with open(path, encoding="utf-8") as handle:
        plan = parse_text(handle.read())
    today = cli_today or plan.config.get("today") or dt.date.today()
    return plan, today


def cli_date(text):
    try:
        return parse_date(text)
    except LineError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def cmd_check(args, plan, today):
    findings, statuses = analyse(plan, today)
    if args.appliance:
        if args.appliance not in plan.flues:
            print(f"flueline: no flue named '{args.appliance}'", file=sys.stderr)
            return 2
        statuses = [s for s in statuses if s.name == args.appliance]
    if args.json:
        payload = {"today": today.isoformat(),
                   "findings": [asdict(f) for f in findings],
                   "appliances": [s.to_dict() for s in statuses]}
        print(json.dumps(payload, indent=2))
    else:
        print(format_check(args.file, today, findings, statuses))
    if any(f.level == "error" for f in findings):
        return 2
    if args.fail_on != "none" and any(
            STATES.index(s.state) >= STATES.index(args.fail_on) for s in statuses):
        return 1
    return 0


def cmd_lint(args, plan, today):
    findings, _ = analyse(plan, today)
    if findings:
        print("\n".join(format_findings(args.file, findings)))
    else:
        print(f"{args.file}: no problems found")
    return 2 if any(f.level == "error" for f in findings) else 0


def cmd_history(args, plan, today):
    if args.appliance and args.appliance not in plan.flues:
        print(f"flueline: no flue named '{args.appliance}'", file=sys.stderr)
        return 2
    events = sorted((e for e in plan.events if not args.appliance or e.name == args.appliance),
                    key=lambda e: (e.date, e.line))
    rows = []
    for e in events:
        if e.kind == "sweep":
            detail = ", ".join(filter(None, [f"soot {e.soot:g}" if e.soot is not None else "",
                                             f"by {e.by}" if e.by else ""])) or "-"
        elif e.kind == "burn":
            detail = f"{e.hours:g} h"
        else:
            detail = e.result + (f": {e.note}" if e.note else "")
        rows.append([e.date, e.name, e.kind, detail])
    print(render_table(["DATE", "FLUE", "EVENT", "DETAIL"], rows) if rows else "No events.")
    return 0


def cmd_burns(args, plan, today):
    year = args.year or today.year
    totals = {name: [0.0] * 12 for name in plan.flues}
    for e in plan.events:
        if e.kind == "burn" and e.name in totals and e.date.year == year and e.date <= today:
            totals[e.name][e.date.month - 1] += e.hours
    rows = []
    for name, months in totals.items():
        rows.append([name] + [f"{h:.1f}" if h else "-" for h in months] + [f"{sum(months):.1f}"])
    headers = ["FLUE"] + list(calendar.month_abbr)[1:] + ["TOTAL"]
    print(f"Burn hours in {year} (up to {today})")
    print(render_table(headers, rows))
    return 0


def cmd_ics(args, plan, today):
    _, statuses = analyse(plan, today)
    with open(args.output, "w", encoding="utf-8", newline="") as handle:
        handle.write(build_ics(statuses, today))
    print(f"Wrote {len(statuses)} sweep events to {args.output}")
    return 0


def cmd_example(args, plan, today):
    print(EXAMPLE, end="")
    return 0


def build_parser():
    parser = argparse.ArgumentParser(prog="flueline",
                                     description="Offline chimney and flue service planner.")
    commands = parser.add_subparsers(dest="command", required=True)

    def add(name, help_text, handler):
        sub = commands.add_parser(name, help=help_text)
        sub.set_defaults(handler=handler)
        if name != "example":
            sub.add_argument("file", help="plain-text flue file")
            sub.add_argument("--today", type=cli_date, help="override today (YYYY-MM-DD)")
        return sub

    check = add("check", "show every appliance's status and set an exit code", cmd_check)
    check.add_argument("--appliance", help="only show this flue")
    check.add_argument("--json", action="store_true", help="print JSON instead of a table")
    check.add_argument("--fail-on", choices=STATES[1:] + ("none",), default="overdue",
                       help="exit 1 when any appliance reaches this state (default: overdue)")

    add("lint", "check the plan file for errors and warnings", cmd_lint)

    history = add("history", "list sweeps, burns and inspections in date order", cmd_history)
    history.add_argument("--appliance", help="only this flue")

    burns = add("burns", "monthly burn hours per flue", cmd_burns)
    burns.add_argument("--year", type=int, help="calendar year (default: today's year)")

    ics = add("ics", "write each flue's next sweep as an iCalendar file", cmd_ics)
    ics.add_argument("output", help="path of the .ics file to write")

    add("example", "print a sample plan to stdout", cmd_example)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.command == "example":
        return args.handler(args, None, None)
    try:
        plan, today = load(args.file, args.today)
    except OSError as exc:
        print(f"flueline: cannot read {args.file}: {exc.strerror}", file=sys.stderr)
        return 2
    return args.handler(args, plan, today)


if __name__ == "__main__":
    sys.exit(main())
