"""生成不包含密钥的本地 JSON 研究报告。"""

import json
from datetime import datetime
from pathlib import Path

from sequoia_x.data.engine import DataAuditReport, SyncReport


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
