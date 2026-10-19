# Flueline 🔥

**An offline chimney and flue service planner.** Keep every appliance's sweep, burn and inspection history in one plain-text file, and see at a glance which flues are due, overdue or out of service.

Flueline is a single Python file using only the standard library. No accounts, no servers, no network calls, no API keys.

## Features

- **Plain-text plan file**: one directive per line (`flue`, `sweep`, `burn`, `inspect`, `config`), with `#` comments and quoted values.
- **Line-numbered lint**: every mistake is reported as `file:line: error: ...` (unknown fuel, bad date, unknown flue, duplicate names, unbalanced quotes).
- **Date-based sweep intervals**: next sweep = last sweep plus a configurable number of months, with month-end clamping (31 Jan + 1 month = 28/29 Feb).
- **Hours-based sweep triggers**: wood, coal and pellet appliances get a burn-hours limit by default; oil and gas default to date-only. Override per flue, or set `hours_limit=none`.
- **Four-level status per appliance**: `ok`, `due-soon` (inside a warning window or past 90% of the hours limit), `overdue`, and `out-of-service`.
- **Inspection log with pass/fail**: a failed inspection takes the appliance out of service until a later pass is logged.
- **Clearance check**: flags any flue whose clearance to combustibles is under your configured minimum.
- **Soot-risk heuristic**: combines the soot reading from the last sweep with burn hours since then, labelled low, moderate, high or critical.
- **Monthly burn report**: hours burned per flue for any year, by month, with totals.
- **Sweep and inspection history**: a date-ordered timeline, optionally filtered to one appliance.
- **Calendar export**: writes each flue's next sweep as an all-day iCalendar (`.ics`) event with proper escaping and line folding.
- **JSON output** for scripts with `check --json`.
- **CI gate**: `check --fail-on` exits 1 when any appliance reaches the chosen state; lint errors exit 2.
- **Built-in sample**: `flueline example` prints a complete working plan to start from.

## Quick start

```bash
python3 flueline.py example > house.flue
python3 flueline.py check house.flue
```

A plan file looks like this:

```text
config today=2026-10-19 warn_days=30 min_clearance_cm=60

flue stove-lounge fuel=wood type=stove clearance_cm=75
flue boiler-garage fuel=oil type=boiler

sweep stove-lounge 2026-02-20 soot=5 by="Dan Hall"
burn stove-lounge 2026-03-14 hours=40
burn stove-lounge 2026-10-02 hours=72

sweep boiler-garage 2025-09-01 soot=1 by="Lee Park"

inspect boiler-garage 2026-08-30 result=fail note="cracked flue joint"
```

With that plan, `check` reports the stove as `ok` (112 of 150 hours burned since its sweep, soot risk high) and the boiler as `out-of-service` (failed inspection) with its sweep also overdue. `check` exits with 1 because an appliance is overdue or worse.

### Commands

| Command | What it does |
| --- | --- |
| `check FILE [--appliance NAME] [--json] [--fail-on STATE\|none] [--today DATE]` | Status table, findings, and a CI exit code |
| `lint FILE [--today DATE]` | Only the line-numbered findings |
| `history FILE [--appliance NAME]` | Sweeps, burns and inspections in date order |
| `burns FILE [--year YYYY] [--today DATE]` | Monthly burn hours per flue |
| `ics FILE OUT.ics [--today DATE]` | Export next sweep dates as calendar events |
| `example` | Print a sample plan |

Exit codes: `0` clean, `1` the `--fail-on` gate tripped, `2` the file has errors or cannot be read.

### Plan directives

| Directive | Example | Notes |
| --- | --- | --- |
| `config` | `config today=2026-10-19 warn_days=30 min_clearance_cm=60` | Sets the reference date and thresholds |
| `flue` | `flue hob fuel=gas type=heater interval_months=12 hours_limit=none` | `fuel` is required: wood, coal, pellet, oil or gas |
| `sweep` | `sweep hob 2026-04-01 soot=4 by="Lee Park"` | `soot` is 0–10 and optional |
| `burn` | `burn hob 2026-05-01 hours=12` | Logs hours for a day or a period |
| `inspect` | `inspect hob 2026-08-30 result=fail note="cracked joint"` | `result` is `pass` or `fail` |

Today's date comes from `--today`, then the `config` line, then the system clock.

## Requirements

- Python 3.10 or newer (uses `X | None` annotations)
- pytest, only to run the test suite

## Testing

```bash
python3 -m pip install pytest
python3 -m pytest
```

The tests in `tests/test_flueline.py` cover parsing and line-numbered errors, month arithmetic, date and hours triggers, inspections, clearance and soot scoring, the calendar export, the JSON output, and CLI exit codes.

## Notes and limits

- The default hours limits (wood and coal 150 h, pellet 400 h) and the 60 cm minimum clearance are **placeholders, not safety standards**. Set `hours_limit=` and `min_clearance_cm=` to the figures in your appliance manual and local rules.
- The soot score is a simple heuristic to rank what to look at first. It does not replace a chimney sweep's inspection.
- Flueline is a record-keeping and planning aid. It does not inspect anything, and it does not replace a qualified inspector.
