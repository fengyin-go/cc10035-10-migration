"""Schema对齐领域服务：统一不同数据源的Schema到标准数据模型。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.services.base import BaseModuleService
from app.services.contract_store import ContractStore, DeliveryHistory, utc_now
from app.store import store


class SchemaAlignService(BaseModuleService):
    MODULE = "schema_align"
    ENTITY = "对齐规则"
    OPERATION_TERM = "转换"
    LIST_FIELDS = ["规则名称", "源Schema", "目标Schema", "字段映射", "值转换", "默认填充", "冲突处理"]
    REQUIRED_FIELDS = ["规则名称", "源Schema", "目标Schema"]
    STATUS_ORDER = ["草稿", "已启用", "已停用"]
    ACTION_RULES = {"启用规则": "草稿", "停用规则": "已启用"}
    NEGATIVE_ACTIONS = ["停用规则"]

    def parse_field_mapping_detailed(self, mapping_str: str) -> dict[str, Any]:
        """解析旧格式字段映射，并记录被折叠的重复源字段映射。

        旧格式用逗号和箭头表达映射：'src1->tgt1, src2->tgt2'。
        旧解析把表达式折叠成 dict：同一源字段在一条规则里映射到两个不同
        目标字段时，后出现的映射生效，先出现的那一项被折叠掉。
        """
        items: list[dict[str, str]] = []
        effective: dict[str, str] = {}
        collapsed: list[dict[str, str]] = []
        pairs = [p.strip() for p in mapping_str.split(",") if "->" in p]
        for pair in pairs:
            parts = pair.split("->", 1)
            if len(parts) != 2:
                continue
            source, target = parts[0].strip(), parts[1].strip()
            items.append({"source": source, "target": target})
            if source in effective:
                collapsed.append({
                    "source": source,
                    "dropped_target": effective[source],
                    "surviving_target": target,
                })
            effective[source] = target
        return {"items": items, "effective": effective, "collapsed": collapsed}

    def parse_field_mapping(self, mapping_str: str) -> dict[str, str]:
        """解析字段映射字符串 'src1->tgt1, src2->tgt2' 为字典（旧行为：重复源字段折叠）。"""
        return dict(self.parse_field_mapping_detailed(mapping_str)["effective"])

    def align_entities(self, entity_extract_records: list[dict[str, Any]],
                       rule_id: int) -> dict[str, Any]:
        """将实体抽取结果按对齐规则映射到目标Schema，被输出目标模块消费。"""
        rule = store.find(self.MODULE, rule_id)
        if rule is None:
            return {"ok": False, "reason": "对齐规则不存在"}
        mapping = self.parse_field_mapping(rule.get("字段映射", ""))
        source_schema = [s.strip() for s in rule.get("源Schema", "").split(",") if s.strip()]
        target_schema = [s.strip() for s in rule.get("目标Schema", "").split(",") if s.strip()]
        defaults = rule.get("默认填充", "")
        default_map = {}
        if defaults:
            for item in defaults.split(","):
                if ":" in item:
                    k, v = item.split(":", 1)
                    default_map[k.strip()] = v.strip()

        aligned = []
        for rec in entity_extract_records:
            target: dict[str, Any] = {}
            for t_field in target_schema:
                src_field = None
                for s, t in mapping.items():
                    if t == t_field:
                        src_field = s
                        break
                if src_field and src_field in rec:
                    target[t_field] = rec[src_field]
                elif t_field in default_map:
                    target[t_field] = default_map[t_field]
                else:
                    target[t_field] = ""
            aligned.append(target)

        return {
            "ok": True,
            "rule_name": rule.get("规则名称"),
            "source_schema": source_schema,
            "target_schema": target_schema,
            "input_count": len(entity_extract_records),
            "output_count": len(aligned),
            "aligned_records": aligned,
        }

    def build_output_schema(self, rule_id: int) -> dict[str, Any]:
        """构建输出目标的Schema定义，供 output_target 模块消费。"""
        rule = store.find(self.MODULE, rule_id)
        if rule is None:
            return {"ok": False, "reason": "规则不存在"}
        target_fields = [s.strip() for s in rule.get("目标Schema", "").split(",") if s.strip()]
        mapping = self.parse_field_mapping(rule.get("字段映射", ""))
        return {
            "ok": True,
            "rule_id": rule_id,
            "target_fields": target_fields,
            "field_count": len(target_fields),
            "mapping_rules": mapping,
            "value_transform": rule.get("值转换", ""),
            "conflict_handler": rule.get("冲突处理", "忽略"),
        }

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        rows = store.rows(self.MODULE)
        base["alignment_stats"] = {
            "total_rules": len(rows),
            "active_rules": sum(1 for r in rows if r.get("status") == "已启用"),
            "with_mapping": sum(1 for r in rows if r.get("字段映射", "").strip()),
        }
        return base


@dataclass
class LegacyRule:
    """一条存量对齐规则：规则ID、目标实体、逗号+箭头字段映射表达式。"""

    rule_id: str
    entity: str
    expression: str


def load_legacy_rules(rules_file: str | Path) -> list[LegacyRule]:
    """读取存量规则导出文件，每行：规则ID | 实体 | 字段映射表达式。"""
    rules: list[LegacyRule] = []
    for line in Path(rules_file).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) != 3:
            raise ValueError(f"无法解析的存量规则行：{line}")
        rules.append(LegacyRule(rule_id=parts[0], entity=parts[1], expression=parts[2]))
    return rules


class ContractMigrationService:
    """把存量逗号/箭头字段映射迁移为版本化实体对齐契约数据集。

    每条存量规则生成两个契约版本：
    - v1：旧解析折叠后实际生效的映射，保证与历史分发已使用的映射和列值兼容；
    - v2：恢复被折叠字段定义后的新版契约。被恢复的字段是否有历史交付依据
      决定它标记为已验证（可切换启用）还是未验证（留在旧路径/带切换条件）。

    迁移后所有规则先钉在 v1，启用新版本走输出目标的切换接口。
    """

    def __init__(self, contract_store: ContractStore,
                 align_service: SchemaAlignService | None = None) -> None:
        self.store = contract_store
        self.align = align_service or SchemaAlignService()

    def migrate(self, rules_file: str | Path, history_dir: str | Path) -> dict[str, Any]:
        rules = load_legacy_rules(rules_file)
        history = DeliveryHistory.load(history_dir)
        manifest: dict[str, Any] = {
            "migration_id": f"MIG-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}",
            "generated_at": utc_now(),
            "legacy_rules_file": Path(rules_file).name,
            "rules": {},
            "version_usage": [],
        }
        for rule in rules:
            v1, v2, state = self._migrate_rule(rule, history)
            self.store.write_contract(v1)
            self.store.write_contract(v2)
            manifest["rules"][rule.rule_id] = state
        self.store.save_manifest(manifest)
        return manifest

    def _migrate_rule(
        self, rule: LegacyRule, history: DeliveryHistory
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        parsed = self.align.parse_field_mapping_detailed(rule.expression)
        effective: dict[str, str] = parsed["effective"]
        collapsed: list[dict[str, str]] = parsed["collapsed"]
        dup_sources = {c["source"] for c in collapsed}
        dropped = {(c["source"], c["dropped_target"]) for c in collapsed}

        def evidence(target: str) -> list[str]:
            return history.evidence_for_target(rule.rule_id, target)

        def origin_of(source: str) -> str:
            return "legacy_collapsed" if source in dup_sources else "legacy"

        # v1：旧解析生效映射，全部已验证（历史分发就是按它们交付的）。
        v1_mappings = [
            {
                "source": source,
                "target": target,
                "origin": origin_of(source),
                "validated": True,
                "delivery_evidence": evidence(target),
            }
            for source, target in effective.items()
        ]
        # v2：按书写顺序恢复完整映射；被折叠项只有存在交付依据时才标记已验证。
        v2_mappings = []
        for item in parsed["items"]:
            source, target = item["source"], item["target"]
            if (source, target) in dropped:
                ev = evidence(target)
                v2_mappings.append({
                    "source": source,
                    "target": target,
                    "origin": "restored",
                    "validated": bool(ev),
                    "delivery_evidence": ev,
                })
            else:
                v2_mappings.append({
                    "source": source,
                    "target": target,
                    "origin": origin_of(source),
                    "validated": True,
                    "delivery_evidence": evidence(target),
                })

        collapsed_entries = [
            {**c, "delivery_evidence": evidence(c["dropped_target"])} for c in collapsed
        ]
        decision, reason, switch_conditions = self._decide(collapsed_entries)

        base = {
            "rule_id": rule.rule_id,
            "entity": rule.entity,
            "source_expression": rule.expression,
            "generated_at": utc_now(),
            "created_by": "contract-migration",
        }
        v1 = {**base, "contract_version": 1, "supersedes": None, "mappings": v1_mappings}
        v2 = {**base, "contract_version": 2, "supersedes": 1, "mappings": v2_mappings}
        state = {
            "entity": rule.entity,
            "decision": decision,
            "reason": reason,
            "legacy_expression": rule.expression,
            "collapsed": collapsed_entries,
            "switch_conditions": switch_conditions,
            "active_version": 1,
            "version_stack": [],
            "contracts": {
                "v1": f"contracts/{rule.rule_id}/v1.json",
                "v2": f"contracts/{rule.rule_id}/v2.json",
            },
        }
        return v1, v2, state

    @staticmethod
    def _decide(collapsed_entries: list[dict[str, Any]]) -> tuple[str, str, list[str]]:
        """按折叠项是否有历史交付依据，给出迁移决策和解释。"""
        if not collapsed_entries:
            return (
                "migrated",
                "无重复源字段冲突，v2 与旧解析生效映射一致，可直接切换",
                [],
            )
        missing = [c for c in collapsed_entries if not c["delivery_evidence"]]
        if not missing:
            refs = "、".join(
                f"{c['source']}->{c['dropped_target']}（交付依据：{'、'.join(c['delivery_evidence'])}）"
                for c in collapsed_entries
            )
            return (
                "switch_ready",
                f"旧解析折叠了 {refs}；折叠有历史交付依据，v2 恢复的新字段定义已验证，可切换启用",
                [],
            )
        refs = "、".join(f"{c['source']}->{c['dropped_target']}" for c in missing)
        conditions = [
            f"字段 {c['dropped_target']}（{c['source']}->{c['dropped_target']}）"
            "需先完成一次验证交付作为交付依据，才允许写入交付文件"
            for c in missing
        ]
        return (
            "stays_legacy",
            f"旧解析折叠了 {refs}；被折叠的映射没有历史交付依据，"
            "新字段定义不能启用，规则留在旧路径（v1）",
            conditions,
        )