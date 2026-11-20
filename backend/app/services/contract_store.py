"""版本化实体对齐契约数据集与迁移清单的磁盘读写。

目录布局（以 backend/dev_env/ 为例）::

    dev_env/
    ├── legacy_rules.txt              # 存量对齐规则导出（逗号+箭头旧格式）
    ├── history/                      # 历史分发：交付文件 + 输入记录 + index.json
    ├── contracts/<rule_id>/v1.json   # 迁移生成的契约版本（v1=旧解析生效映射）
    ├── contracts/<rule_id>/v2.json   # v2=恢复被折叠字段定义后的新版契约
    ├── deliveries/<delivery_id>.csv  # 输出目标写出的实际分发数据
    └── migration_manifest.json       # 迁移清单：逐规则决策 + 版本使用回写
"""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ContractStore:
    """版本化契约数据集与迁移清单的文件存取。"""

    def __init__(self, env_dir: str | Path) -> None:
        self.env_dir = Path(env_dir)
        self.contracts_dir = self.env_dir / "contracts"
        self.deliveries_dir = self.env_dir / "deliveries"
        self.manifest_path = self.env_dir / "migration_manifest.json"

    # ---- 契约数据集 ----
    def contract_path(self, rule_id: str, version: int) -> Path:
        return self.contracts_dir / rule_id / f"v{int(version)}.json"

    def contract_exists(self, rule_id: str, version: int) -> bool:
        return self.contract_path(rule_id, version).exists()

    def write_contract(self, contract: dict[str, Any]) -> Path:
        path = self.contract_path(contract["rule_id"], contract["contract_version"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(contract, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return path

    def load_contract(self, rule_id: str, version: int) -> dict[str, Any] | None:
        path = self.contract_path(rule_id, version)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    # ---- 迁移清单 ----
    def load_manifest(self) -> dict[str, Any]:
        return json.loads(self.manifest_path.read_text(encoding="utf-8"))

    def save_manifest(self, manifest: dict[str, Any]) -> None:
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        self.manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def append_version_usage(self, entry: dict[str, Any]) -> None:
        """把一次版本使用结果（运行/切换/回滚）回写到迁移清单。"""
        manifest = self.load_manifest()
        manifest.setdefault("version_usage", []).append(entry)
        self.save_manifest(manifest)

    def update_rule_state(self, rule_id: str, **changes: Any) -> dict[str, Any]:
        """更新清单中某条规则的版本状态（活动版本、回滚栈等）。"""
        manifest = self.load_manifest()
        rule_state = manifest["rules"][rule_id]
        rule_state.update(changes)
        self.save_manifest(manifest)
        return rule_state


@dataclass
class Delivery:
    """一次历史分发交付：交付文件 + 产生它的输入记录。"""

    delivery_id: str
    rule_id: str
    delivered_at: str
    csv_path: Path
    input_path: Path

    def columns(self) -> list[str]:
        with self.csv_path.open(newline="", encoding="utf-8") as fh:
            return next(csv.reader(fh))

    def input_records(self) -> list[dict[str, Any]]:
        return json.loads(self.input_path.read_text(encoding="utf-8"))


class DeliveryHistory:
    """历史分发记录：迁移时用作被折叠映射是否有交付依据的判定来源。"""

    def __init__(self, deliveries: list[Delivery]) -> None:
        self.deliveries = deliveries

    @classmethod
    def load(cls, history_dir: str | Path) -> "DeliveryHistory":
        history_dir = Path(history_dir)
        index = json.loads((history_dir / "index.json").read_text(encoding="utf-8"))
        deliveries = [
            Delivery(
                delivery_id=item["delivery_id"],
                rule_id=item["rule_id"],
                delivered_at=item["delivered_at"],
                csv_path=history_dir / item["file"],
                input_path=history_dir / item["input_file"],
            )
            for item in index["deliveries"]
        ]
        return cls(deliveries)

    def deliveries_for(self, rule_id: str) -> list[Delivery]:
        return sorted(
            (d for d in self.deliveries if d.rule_id == rule_id),
            key=lambda d: d.delivered_at,
        )

    def evidence_for_target(self, rule_id: str, target: str) -> list[str]:
        """返回历史交付中包含该目标列的 delivery_id 列表（交付依据）。"""
        return [
            d.delivery_id for d in self.deliveries_for(rule_id)
            if target in d.columns()
        ]

    def latest(self, rule_id: str) -> Delivery | None:
        deliveries = self.deliveries_for(rule_id)
        return deliveries[-1] if deliveries else None
