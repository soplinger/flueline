import datetime as dt
import json
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import flueline as fl  # noqa: E402

TODAY = dt.date(2026, 10, 19)


def plan_of(text):
    return fl.parse_text(textwrap.dedent(text))


def status_of(text, name="stove", today=TODAY):
    plan = plan_of(text)
    _, statuses = fl.analyse(plan, today)
    return next(s for s in statuses if s.name == name)


def test_parse_reports_line_numbers_for_each_bad_line():
    plan = plan_of("""\
        flue a fuel=wood
        flue b fuel=peat
        bogus x
        """)
    found = {(f.line, f.message) for f in plan.findings}
    assert (2, "fuel must be one of: wood, coal, pellet, oil, gas") in found
    assert (3, "unknown directive 'bogus'") in found


def test_duplicate_flue_points_back_to_first_definition():
    plan = plan_of("""\
        flue a fuel=wood
        flue a fuel=gas
        """)
    assert [(f.line, f.message) for f in plan.findings] == [
        (2, "flue 'a' is already defined on line 1")]


def test_unbalanced_quotes_are_reported_not_raised():
    plan = plan_of('sweep a "2026-01-01\n')
    assert plan.findings[0].line == 1
    assert "unbalanced quotation marks" in plan.findings[0].message


def test_unknown_and_duplicate_options_are_rejected():
    plan = plan_of("flue a fuel=wood colour=red\nflue b fuel=wood fuel=gas\n")
    messages = [f.message for f in plan.findings]
    assert "unknown option 'colour'" in messages
    assert "option 'fuel' given twice" in messages


def test_add_months_clamps_to_month_end_and_wraps_years():
    assert fl.add_months(dt.date(2026, 1, 31), 1) == dt.date(2026, 2, 28)
    assert fl.add_months(dt.date(2024, 1, 31), 1) == dt.date(2024, 2, 29)
    assert fl.add_months(dt.date(2025, 12, 15), 1) == dt.date(2026, 1, 15)
    assert fl.add_months(dt.date(2025, 11, 5), 12) == dt.date(2026, 11, 5)


def test_overdue_when_sweep_date_has_passed():
    status = status_of("""\
        flue stove fuel=wood
        sweep stove 2025-09-01 soot=1
        """)
    assert status.state == "overdue"
    assert status.next_due == dt.date(2026, 9, 1)
    assert any("passed on 2026-09-01" in n for n in status.notes)


def test_due_soon_inside_warning_window():
    status = status_of("""\
        flue stove fuel=wood
        sweep stove 2025-11-05 soot=2
        """)
    assert status.state == "due-soon"
    assert status.next_due == dt.date(2026, 11, 5)


def test_hours_limit_makes_appliance_overdue_even_before_date():
    status = status_of("""\
        flue stove fuel=wood
        sweep stove 2026-01-01 soot=2
        burn stove 2026-02-01 hours=100
        burn stove 2026-03-01 hours=60
        """)
    assert status.state == "overdue"
    assert status.hours_since_sweep == 160
    assert any("burned 160 h of 150 h" in n for n in status.notes)


def test_hours_warning_at_ninety_percent_of_limit():
    status = status_of("""\
        flue stove fuel=wood
        sweep stove 2026-01-01 soot=2
        burn stove 2026-02-01 hours=140
        """)
    assert status.state == "due-soon"


def test_burns_before_the_last_sweep_do_not_count():
    status = status_of("""\
        flue stove fuel=wood
        burn stove 2025-12-01 hours=100
        sweep stove 2026-01-01 soot=2
        """)
    assert status.hours_since_sweep == 0
    assert status.state == "ok"


def test_failed_inspection_is_out_of_service_until_a_later_pass():
    failing = status_of("""\
        flue stove fuel=wood
        sweep stove 2026-01-01
        inspect stove 2026-02-01 result=fail note="cracked liner"
        """)
    assert failing.state == "out-of-service"
    assert any("cracked liner" in n for n in failing.notes)

    cleared = status_of("""\
        flue stove fuel=wood
        sweep stove 2026-01-01
        inspect stove 2026-02-01 result=fail
        inspect stove 2026-03-01 result=pass
        """)
    assert cleared.state == "ok"


def test_clearance_below_minimum_is_noted():
    status = status_of("""\
        flue stove fuel=wood clearance_cm=45
        sweep stove 2026-01-01
        """)
    assert any("clearance 45 cm is below 60 cm" in n for n in status.notes)


def test_config_threshold_changes_due_soon_window():
    text = """\
        config warn_days=5
        flue stove fuel=wood
        sweep stove 2025-11-05
        """
    assert status_of(text).state == "ok"


def test_soot_bands_and_high_soot_note():
    assert fl.soot_label(3.9) == "low"
    assert fl.soot_label(4) == "moderate"
    assert fl.soot_label(6) == "high"
    assert fl.soot_label(8) == "critical"
    status = status_of("""\
        flue stove fuel=wood
        sweep stove 2026-01-01 soot=9
        """)
    assert status.soot_label == "critical"
    assert any("soot risk critical" in n for n in status.notes)


def test_unknown_flue_reference_is_an_error_on_its_line():
    plan = plan_of("sweep ghost 2026-01-01\n")
    findings, _ = fl.analyse(plan, TODAY)
    assert (findings[0].line, findings[0].level) == (1, "error")
    assert "unknown flue 'ghost'" in findings[0].message


def test_future_dated_event_is_warned_and_ignored():
    text = """\
        flue stove fuel=wood
        sweep stove 2026-11-01 soot=2
        """
    plan = plan_of(text)
    findings, statuses = fl.analyse(plan, TODAY)
    assert [f.level for f in findings] == ["warning"]
    assert statuses[0].state == "overdue"
    assert "never swept" in statuses[0].notes


def test_ics_escapes_special_characters_and_folds_long_lines():
    assert fl.ics_escape("a,b;c\\d") == "a\\,b\\;c\\\\d"
    folded = fl.ics_fold("x" * 200)
    assert all(len(part.lstrip(" ")) <= 75 for part in folded.split("\r\n"))
    assert "".join(part.lstrip(" ") for part in folded.split("\r\n")) == "x" * 200


def test_ics_has_one_event_per_flue_and_never_swept_is_due_today():
    plan = plan_of("""\
        flue stove fuel=wood
        flue boiler fuel=oil
        sweep boiler 2026-01-01
        """)
    _, statuses = fl.analyse(plan, TODAY)
    ics = fl.build_ics(statuses, TODAY)
    assert ics.count("BEGIN:VEVENT") == 2
    assert "DTSTART;VALUE=DATE:20261019" in ics


def write(tmp_path, text):
    path = tmp_path / "plan.flue"
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return str(path)


def test_check_exit_codes_for_overdue_errors_and_gate_off(tmp_path, capsys):
    overdue = write(tmp_path, """\
        flue stove fuel=wood
        sweep stove 2025-09-01
        """)
    assert fl.main(["check", overdue, "--today", "2026-10-19"]) == 1
    assert fl.main(["check", overdue, "--today", "2026-10-19", "--fail-on", "none"]) == 0

    broken = write(tmp_path, "flue stove fuel=peat\n")
    assert fl.main(["check", broken, "--today", "2026-10-19"]) == 2
    assert fl.main(["check", str(tmp_path / "missing.flue")]) == 2


def test_check_json_lists_every_appliance(tmp_path, capsys):
    path = write(tmp_path, """\
        flue stove fuel=wood
        flue boiler fuel=oil
        sweep boiler 2026-01-01
        """)
    fl.main(["check", path, "--today", "2026-10-19", "--json", "--fail-on", "none"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["today"] == "2026-10-19"
    assert [a["name"] for a in payload["appliances"]] == ["stove", "boiler"]
    assert payload["appliances"][1]["next_due"] == "2027-01-01"


def test_example_plan_parses_without_errors():
    plan = fl.parse_text(fl.EXAMPLE)
    findings, statuses = fl.analyse(plan, fl.parse_text(fl.EXAMPLE).config["today"])
    assert [f for f in findings if f.level == "error"] == []
    states = {s.name: s.state for s in statuses}
    assert states == {"stove-lounge": "ok", "insert-bedroom": "due-soon",
                      "boiler-garage": "overdue", "hob-kitchen": "out-of-service"}
