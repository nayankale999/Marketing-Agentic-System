"""W38 — CSV export of the campaign report (E10-S04 AC #2 partial).

Pure-function tests of `_report_to_csv`. We don't exercise the full
endpoint here — that's in test_report_endpoints.py.
"""

from __future__ import annotations

import csv
import io

from app.api.reports import _report_to_csv


def _parse(csv_text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(csv_text)))


def test_csv_covers_every_section_in_stable_order() -> None:
    """Populated sections appear; empty list sections are skipped to keep
    the export tight. Among sections that do appear, ordering matches the
    UI render order."""
    data = {
        "objectives": {"objective": "Drive MQLs", "budget_total": "1000.00"},
        "kpis_vs_target": [{"name": "click", "target": 100, "observed": 120}],
        "channel_breakdown": [{"name": "Email", "clicks": 100}],
        "ab_tests": [{"name": "Subject test", "status": "significant"}],
        "anomalies": [{"metric": "click", "severity": "warning"}],
        "recommendations_applied": [{"kind": "budget_shift"}],
        "recommendations_rejected": [{"kind": "budget_shift"}],
        "spend_total": "300.00",
    }
    rows = _parse(_report_to_csv(data))
    sections = [r[0] for r in rows if r[0] != "section"]
    seen: list[str] = []
    for s in sections:
        if s not in seen:
            seen.append(s)
    assert seen == [
        "objectives",
        "kpis_vs_target",
        "channel_breakdown",
        "ab_tests",
        "anomalies",
        "recommendations_applied",
        "recommendations_rejected",
        "spend_total",
    ]


def test_csv_writes_no_data_marker_for_null_sections() -> None:
    data = {
        "objectives": {"objective": "Drive MQLs"},
        "kpis_vs_target": [{"name": "click", "target": 100, "observed": None}],
        "channel_breakdown": [],
        "ab_tests": [],
        "anomalies": [],
        "recommendations_applied": [],
        "recommendations_rejected": [],
        "spend_total": None,
    }
    rows = _parse(_report_to_csv(data))
    # spend_total with None becomes a "(no data)" marker.
    spend_rows = [r for r in rows if r[0] == "spend_total"]
    assert spend_rows
    assert spend_rows[0][2] == "(no data)"


def test_csv_handles_nested_dicts_via_json() -> None:
    data = {
        "objectives": {},
        "kpis_vs_target": [],
        "channel_breakdown": [],
        "ab_tests": [],
        "anomalies": [],
        "recommendations_applied": [
            {"kind": "budget_shift", "proposal": {"from": {"channel": "LinkedIn"}}}
        ],
        "recommendations_rejected": [],
        "spend_total": "0",
    }
    rows = _parse(_report_to_csv(data))
    proposal_row = next(r for r in rows if r[0] == "recommendations_applied" and r[1].endswith(".proposal"))
    # The csv module unescapes the doubled quotes from CSV's escape rule,
    # leaving the original JSON string.
    assert proposal_row[2].startswith("{")
    assert '"from"' in proposal_row[2]
    assert '"channel"' in proposal_row[2]


def test_csv_starts_with_header_row() -> None:
    data = {"objectives": {}, "spend_total": "0"}
    rows = _parse(_report_to_csv(data))
    assert rows[0] == ["section", "key", "value"]
