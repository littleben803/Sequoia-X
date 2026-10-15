"""本地研究报告测试。"""

import json

from sequoia_x.data.engine import DataAuditReport, SyncReport
from sequoia_x.reporting.local import write_local_report


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
