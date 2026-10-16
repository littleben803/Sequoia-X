"""本地研究报告测试。"""

import json

from sequoia_x.data.engine import DataAuditReport, SyncReport
from sequoia_x.data.models import V3AuditReport
from sequoia_x.data.research import ResearchBar, ResearchExclusion, ResearchSnapshot
from sequoia_x.reporting.local import write_local_report, write_research_report
from sequoia_x.strategy.base import StrategyResult


def test_write_local_report(tmp_path) -> None:
    audit = DataAuditReport(
        ready=True,
        latest_date="2026-10-13",
        total_symbols=2,
        stored_symbols=2,
        latest_symbols=2,
        stale_symbols=0,
        latest_coverage=1.0,
        invalid_rows=0,
        future_rows=0,
        data_age_days=0,
        problems=(),
    )
    sync = SyncReport(2, 2, 0, 2)
    output = tmp_path / "latest.json"

    write_local_report(
        audit=audit,
        selections={"MaVolumeStrategy": ["000001"]},
        sync=sync,
        output_path=str(output),
    )

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["data_date"] == "2026-10-13"
    assert payload["data_audit"]["ready"] is True
    assert payload["strategies"]["MaVolumeStrategy"] == ["000001"]


def test_write_v3_research_report_contains_date_rules_and_risk_boundary(tmp_path) -> None:
    audit = V3AuditReport(
        True,
        "2026-10-13",
        2,
        20,
        1,
        1,
        0,
        0,
        0,
        0,
        (),
    )
    snapshot = ResearchSnapshot(
        as_of_date="2026-10-13",
        bars={
            "SSE:600001": (
                ResearchBar(
                    "SSE:600001",
                    "2026-10-13",
                    10,
                    11,
                    9,
                    10.5,
                    10,
                    100,
                    1_000,
                    1,
                ),
            )
        },
        exclusions=(ResearchExclusion("BSE:920001", "MISSING_QFQ_FACTORS"),),
        requested_instruments=2,
        eligible_instruments=1,
        coverage=0.5,
        audit=audit,
    )
    result = StrategyResult(
        "shakeout",
        "2026-10-13",
        ("SSE:600001",),
        ("测试规则",),
        2,
        1,
        1,
        risk_note="默认禁用",
    )
    output = tmp_path / "latest.json"

    write_research_report(snapshot=snapshot, results=(result,), output_path=str(output))

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 3
    assert payload["data_date"] == "2026-10-13"
    assert payload["price_adjustment"] == "QFQ"
    assert payload["data_quality"]["exclusion_counts"] == {"MISSING_QFQ_FACTORS": 1}
    assert payload["strategies"]["shakeout"]["rule_basis"] == ["测试规则"]
    assert payload["strategies"]["shakeout"]["risk_note"] == "默认禁用"
