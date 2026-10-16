"""生成不包含密钥的本地 JSON 研究报告。"""

import json
from collections import Counter
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from sequoia_x.data.engine import DataAuditReport, SyncReport
from sequoia_x.data.research import ResearchSnapshot
from sequoia_x.strategy.base import StrategyResult


def write_local_report(
    *,
    audit: DataAuditReport,
    selections: dict[str, list[str]],
    sync: SyncReport | None,
    output_path: str = "reports/latest.json",
) -> Path:
    """以原子替换方式写入最新研究报告。"""
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "data_date": audit.latest_date,
        "disclaimer": "仅用于个人研究，不构成投资建议或收益保证。",
        "data_audit": audit.to_dict(),
        "sync": sync.to_dict() if sync else None,
        "strategies": selections,
    }
    temp_path = path.with_suffix(f"{path.suffix}.tmp")
    temp_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temp_path.replace(path)
    return path


def write_research_report(
    *,
    snapshot: ResearchSnapshot,
    results: tuple[StrategyResult, ...],
    output_path: str = "reports/latest.json",
) -> Path:
    """原子写入 V3 研究报告，不包含密钥或网络配置。"""
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    exclusion_counts = Counter(item.reason for item in snapshot.exclusions)
    payload = {
        "schema_version": 3,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "data_date": snapshot.as_of_date,
        "price_adjustment": snapshot.price_adjustment,
        "disclaimer": "仅用于个人研究，不构成投资建议、专业意见或收益保证。",
        "data_quality": {
            "requested_instruments": snapshot.requested_instruments,
            "eligible_instruments": snapshot.eligible_instruments,
            "coverage": snapshot.coverage,
            "exclusion_counts": dict(exclusion_counts),
            "base_audit": snapshot.audit.to_dict(),
        },
        "exclusions": [asdict(item) for item in snapshot.exclusions],
        "strategies": {
            result.strategy_key: {
                "selected_instruments": list(result.selected_instruments),
                "selected_count": len(result.selected_instruments),
                "rule_basis": list(result.rule_basis),
                "input_instruments": result.input_instruments,
                "eligible_instruments": result.eligible_instruments,
                "data_exclusions": result.data_exclusions,
                "calculation_errors": list(result.calculation_errors),
                "risk_note": result.risk_note,
            }
            for result in results
        },
    }
    temp_path = path.with_suffix(f"{path.suffix}.tmp")
    temp_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temp_path.replace(path)
    return path
