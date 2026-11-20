"""输出目标领域服务：管理结构化数据的输出分发配置。"""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from app.services.base import BaseModuleService
from app.services.contract_store import ContractStore, utc_now
from app.store import store


OUTPUT_FORMATS = {"json": "application/json", "csv": "text/csv",
                   "parquet": "application/x-parquet", "avro": "application/avro"}


class OutputTargetService(BaseModuleService):
    MODULE = "output_target"
    ENTITY = "输出配置"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["配置名称", "输出类型", "目标地址", "输出格式", "分区策略", "刷新频率", "校验方式"]
    REQUIRED_FIELDS = ["配置名称", "输出类型", "目标地址"]
    STATUS_ORDER = ["草稿", "已启用", "已暂停"]
    ACTION_RULES = {"启用配置": "草稿", "暂停配置": "已启用"}
    NEGATIVE_ACTIONS = []

    def format_entity_output(self, entities: list[dict[str, Any]],
                             config_id: int) -> dict[str, Any]:
        """将实体对齐后的数据按输出配置格式化。消费 schema_align 输出。"""
        config = store.find(self.MODULE, config_id)
        if config is None:
            return {"ok": False, "reason": "输出配置不存在"}
        output_type = config.get("输出类型", "json")
        output_format = config.get("输出格式", "json")
        target = config.get("目标地址", "")

        if output_type == "json":
            payload = json.dumps(entities, ensure_ascii=False, indent=2)
            mime = OUTPUT_FORMATS.get(output_format, "application/json")
        elif output_type == "csv":
            if entities:
                keys = list(entities[0].keys())
                lines = [",".join(keys)]
                for e in entities:
                    lines.append(",".join(str(e.get(k, "")) for k in keys))
                payload = "\n".join(lines)
            else:
                payload = ""
            mime = OUTPUT_FORMATS.get("csv", "text/csv")
        else:
            payload = json.dumps(entities, ensure_ascii=False)
            mime = OUTPUT_FORMATS.get(output_format, "application/json")

        return {
            "ok": True,
            "config_name": config.get("配置名称"),
            "output_type": output_type,
            "target": target,
            "mime_type": mime,
            "record_count": len(entities),
            "payload_size": len(payload.encode("utf-8")),
            "payload_sample": payload[:200] if len(payload) > 200 else payload,
        }

    def build_output_chain(self, entity_extract_count: int,
                           schema_align_rule_id: int) -> dict[str, Any]:
        """构建从实体抽取到输出的完整链路的摘要信息。"""
        rule = store.find("schema_align", schema_align_rule_id)
        rows = store.rows(self.MODULE)
        active_outputs = [r for r in rows if r.get("status") == "已启用"]
        return {
            "entity_count": entity_extract_count,
            "align_rule": rule.get("规则名称") if rule else "未知",
            "active_output_targets": len(active_outputs),
            "target_types": [r.get("输出类型") for r in active_outputs],
            "output_chain_ready": len(active_outputs) > 0,
        }

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        rows = store.rows(self.MODULE)
        base["output_stats"] = {
            "total_configs": len(rows),
            "formats": {r.get("输出格式"): 0 for r in rows},
            "active_targets": sum(1 for r in rows if r.get("status") == "已启用"),
        }
        return base


class VersionSwitchError(Exception):
    """契约版本切换/回滚不合法（版本不存在、无回滚历史、规则未知等）。"""


class DistributionRun:
    """一次正在运行的分发：从创建起只消费一个契约版本。

    版本在 begin_run 时钉住；此后活动版本无论如何切换/回滚，本次分发
    都只按钉住的契约写出。切换时尚未验证的字段一律不写入交付文件。
    """

    def __init__(self, service: "ContractDistributionService", rule_id: str,
                 version: int, contract: dict[str, Any]) -> None:
        self._service = service
        self.rule_id = rule_id
        self.version = version
        self._contract = contract

    @property
    def writable_mappings(self) -> list[dict[str, Any]]:
        """确认的一整套映射：钉住版本中已验证的字段。"""
        return [m for m in self._contract["mappings"] if m.get("validated")]

    @property
    def suppressed_mappings(self) -> list[dict[str, Any]]:
        return [m for m in self._contract["mappings"] if not m.get("validated")]

    def deliver(self, records: list[dict[str, Any]], delivery_id: str) -> dict[str, Any]:
        store = self._service.store
        store.deliveries_dir.mkdir(parents=True, exist_ok=True)
        path = store.deliveries_dir / f"{delivery_id}.csv"
        writable = self.writable_mappings
        columns = [m["target"] for m in writable]
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh, lineterminator="\n")
            writer.writerow(columns)
            for record in records:
                writer.writerow([record.get(m["source"], "") for m in writable])
        suppressed = [m["target"] for m in self.suppressed_mappings]
        store.append_version_usage({
            "at": utc_now(),
            "rule_id": self.rule_id,
            "action": "run",
            "version": self.version,
            "delivery_id": delivery_id,
            "delivery_file": str(path.relative_to(store.env_dir)),
            "rows": len(records),
            "fields_written": columns,
            "fields_suppressed": suppressed,
        })
        return {
            "ok": True,
            "rule_id": self.rule_id,
            "version": self.version,
            "delivery_id": delivery_id,
            "delivery_file": path,
            "columns": columns,
            "suppressed_fields": suppressed,
            "rows": len(records),
        }


class ContractDistributionService:
    """输出目标的版本化分发接口：消费版本化实体对齐契约数据集。

    - 每次分发只消费一个契约版本（begin_run 钉住）；
    - 切换/回滚只改活动版本，不影响已在运行的分发；
    - 所有版本使用结果回写到迁移清单。
    """

    def __init__(self, env_dir: str | Path) -> None:
        self.store = ContractStore(env_dir)

    @staticmethod
    def _rule_state(manifest: dict[str, Any], rule_id: str) -> dict[str, Any]:
        try:
            return manifest["rules"][rule_id]
        except KeyError:
            raise VersionSwitchError(f"迁移清单中不存在规则：{rule_id}") from None

    def active_version(self, rule_id: str) -> int:
        return int(self._rule_state(self.store.load_manifest(), rule_id)["active_version"])

    def begin_run(self, rule_id: str) -> DistributionRun:
        """发起一次分发：钉住当前活动版本，整个运行只消费该版本。"""
        version = self.active_version(rule_id)
        contract = self.store.load_contract(rule_id, version)
        if contract is None:
            raise VersionSwitchError(f"契约版本不存在：{rule_id} v{version}")
        return DistributionRun(self, rule_id, version, contract)

    def run_distribution(self, rule_id: str, records: list[dict[str, Any]],
                         delivery_id: str) -> dict[str, Any]:
        """按当前活动版本生成实际分发数据（等价于 begin_run + deliver）。"""
        return self.begin_run(rule_id).deliver(records, delivery_id)

    def switch_version(self, rule_id: str, target_version: int) -> dict[str, Any]:
        """把规则切换到指定契约版本；未验证字段切换后仍不写入交付文件。"""
        manifest = self.store.load_manifest()
        state = self._rule_state(manifest, rule_id)
        current = int(state["active_version"])
        target_version = int(target_version)
        if target_version == current:
            raise VersionSwitchError(f"{rule_id} 已运行在 v{current}")
        contract = self.store.load_contract(rule_id, target_version)
        if contract is None:
            raise VersionSwitchError(f"契约版本不存在：{rule_id} v{target_version}")
        suppressed = [m["target"] for m in contract["mappings"] if not m.get("validated")]
        self.store.update_rule_state(
            rule_id,
            active_version=target_version,
            version_stack=[*state.get("version_stack", []), current],
        )
        self.store.append_version_usage({
            "at": utc_now(),
            "rule_id": rule_id,
            "action": "switch",
            "from_version": current,
            "to_version": target_version,
            "suppressed_fields": suppressed,
        })
        message = f"{rule_id} 已切换到 v{target_version}"
        if suppressed:
            message += f"；未验证字段不写入交付文件：{'、'.join(suppressed)}"
        return {
            "ok": True,
            "rule_id": rule_id,
            "active_version": target_version,
            "suppressed_fields": suppressed,
            "message": message,
        }

    def rollback(self, rule_id: str) -> dict[str, Any]:
        """回滚到上一次切换前的契约版本。"""
        manifest = self.store.load_manifest()
        state = self._rule_state(manifest, rule_id)
        stack = list(state.get("version_stack", []))
        if not stack:
            raise VersionSwitchError(f"{rule_id} 没有可回滚的历史版本")
        current = int(state["active_version"])
        previous = stack.pop()
        self.store.update_rule_state(
            rule_id, active_version=previous, version_stack=stack)
        self.store.append_version_usage({
            "at": utc_now(),
            "rule_id": rule_id,
            "action": "rollback",
            "from_version": current,
            "to_version": previous,
        })
        return {
            "ok": True,
            "rule_id": rule_id,
            "active_version": previous,
            "message": f"{rule_id} 已回滚到 v{previous}",
        }