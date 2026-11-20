#!/usr/bin/env python3
"""迁移场景：存量逗号/箭头字段映射 → 版本化实体对齐契约数据集。

用无冲突（R-USER）和重复源字段（R-ORDER / R-LOG）两种历史输入跑一次迁移，
随后通过输出服务接口复跑历史分发、切换契约版本、回滚，并核对本地开发
环境中的数据文件（contracts/、deliveries/）与迁移清单（migration_manifest.json）。

用法：cd backend && python3 scripts/run_contract_migration.py
"""
from __future__ import annotations

import csv
import json
import shutil
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from app.services.contract_store import ContractStore, DeliveryHistory  # noqa: E402
from app.services.output_target import (  # noqa: E402
    ContractDistributionService,
    VersionSwitchError,
)
from app.services.schema_align import ContractMigrationService  # noqa: E402

ENV_DIR = BACKEND_DIR / "dev_env"
RULES_FILE = ENV_DIR / "legacy_rules.txt"
HISTORY_DIR = ENV_DIR / "history"

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, ok, detail))
    suffix = f" — {detail}" if detail else ""
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{suffix}")


def read_csv(path: Path) -> tuple[list[str], list[list[str]]]:
    with path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.reader(fh))
    return rows[0], rows[1:]


def reset_generated_artifacts() -> None:
    """清理上次运行生成的契约、交付文件和迁移清单，保证场景可重复。"""
    shutil.rmtree(ENV_DIR / "contracts", ignore_errors=True)
    shutil.rmtree(ENV_DIR / "deliveries", ignore_errors=True)
    (ENV_DIR / "migration_manifest.json").unlink(missing_ok=True)


def main() -> int:
    reset_generated_artifacts()
    store = ContractStore(ENV_DIR)
    output = ContractDistributionService(ENV_DIR)
    history = DeliveryHistory.load(HISTORY_DIR)

    print("== 1. 迁移：存量规则 → 版本化契约数据集（无冲突 + 重复源字段两种输入） ==")
    manifest = ContractMigrationService(store).migrate(RULES_FILE, HISTORY_DIR)
    for rule_id, state in manifest["rules"].items():
        print(f"  {rule_id}: {state['decision']}（活动版本 v{state['active_version']}）")
        print(f"    原因：{state['reason']}")
        for condition in state["switch_conditions"]:
            print(f"    切换条件：{condition}")
    rules = manifest["rules"]
    check("无冲突规则 R-USER 决策为 migrated",
          rules["R-USER"]["decision"] == "migrated")
    check("重复源字段且有交付依据的 R-ORDER 决策为 switch_ready",
          rules["R-ORDER"]["decision"] == "switch_ready",
          rules["R-ORDER"]["collapsed"][0]["delivery_evidence"][0])
    check("重复源字段且无交付依据的 R-LOG 决策为 stays_legacy 且带切换条件",
          rules["R-LOG"]["decision"] == "stays_legacy"
          and len(rules["R-LOG"]["switch_conditions"]) == 1)
    check("三条规则均生成 v1/v2 契约文件",
          all(store.contract_exists(r, v)
              for r in ("R-USER", "R-ORDER", "R-LOG") for v in (1, 2)))
    check("迁移后所有规则先钉在 v1",
          all(s["active_version"] == 1 for s in rules.values()))

    print("== 2. 按契约版本复跑历史分发：v1 复跑结果须与历史交付的映射和列值一致 ==")
    for rule_id in ("R-USER", "R-ORDER", "R-LOG"):
        delivery = history.latest(rule_id)
        result = output.run_distribution(
            rule_id, delivery.input_records(), f"replay-{delivery.delivery_id}")
        hist = read_csv(HISTORY_DIR / delivery.csv_path.name)
        replay = read_csv(result["delivery_file"])
        check(f"{rule_id} 用 v{result['version']} 复跑 {delivery.delivery_id}：列与列值一致",
              hist == replay, f"columns={replay[0]}")

    print("== 3. 切换（输出服务接口）：R-ORDER → v2，折叠有交付依据，新字段定义启用 ==")
    report = output.switch_version("R-ORDER", 2)
    print(f"  {report['message']}")
    check("切换后 R-ORDER 活动版本为 v2", report["active_version"] == 2)
    records = [{"order_no": "SO-9201", "amount": "88.00"}]
    result = output.run_distribution("R-ORDER", records, "D-2026-901")
    header, rows = read_csv(result["delivery_file"])
    check("v2 交付文件包含恢复的新字段 order_id",
          header == ["order_id", "order_code", "order_amount"], str(header))
    check("恢复字段的列值来自同一源字段 order_no",
          rows[0][0] == "SO-9201" and rows[0][1] == "SO-9201", str(rows[0]))

    print("== 4. 回滚（输出服务接口）：正在运行的分发只消费一个版本 ==")
    inflight_run = output.begin_run("R-ORDER")  # 钉住 v2
    back = output.rollback("R-ORDER")
    print(f"  {back['message']}")
    check("回滚后 R-ORDER 活动版本为 v1", back["active_version"] == 1)
    inflight = inflight_run.deliver(records, "D-2026-902")
    header, _ = read_csv(inflight["delivery_file"])
    check("回滚前已开始的分发仍按钉住的 v2 写出",
          inflight["version"] == 2
          and header == ["order_id", "order_code", "order_amount"], str(header))
    fresh = output.run_distribution("R-ORDER", records, "D-2026-903")
    header, _ = read_csv(fresh["delivery_file"])
    check("回滚后新发起的分发按 v1 写出（不含 order_id）",
          fresh["version"] == 1 and header == ["order_code", "order_amount"], str(header))

    print("== 5. 切换时尚未验证的字段不得写入交付文件 ==")
    report = output.switch_version("R-LOG", 2)
    print(f"  {report['message']}")
    check("R-LOG 切到 v2 时报告未验证字段 device_id",
          report["suppressed_fields"] == ["device_id"])
    result = output.run_distribution(
        "R-LOG", [{"dev_id": "DVC-09", "ts": "2026-11-20T01:00:00Z"}], "D-2026-904")
    header, rows = read_csv(result["delivery_file"])
    check("v2 运行但 device_id 未写入交付文件",
          header == ["dev_uuid", "event_ts"], str(header))
    check("已验证字段的列值照常交付",
          rows[0] == ["DVC-09", "2026-11-20T01:00:00Z"], str(rows[0]))
    output.rollback("R-LOG")

    print("== 6. 无冲突规则的切换与回滚 ==")
    output.switch_version("R-USER", 2)
    result = output.run_distribution(
        "R-USER",
        [{"crm_id": "C-2001", "full_name": "Grace Hopper", "email": "grace@example.com"}],
        "D-2026-905")
    header, _ = read_csv(result["delivery_file"])
    check("R-USER v2 交付列与 v1 一致",
          header == ["user_id", "user_name", "contact_email"], str(header))
    output.rollback("R-USER")

    print("== 7. 非法切换被拒绝 ==")
    try:
        output.switch_version("R-USER", 99)
        check("切换到不存在的契约版本被拒绝", False)
    except VersionSwitchError as exc:
        check("切换到不存在的契约版本被拒绝", True, str(exc))

    print("== 8. 核对迁移清单回写 ==")
    final = json.loads((ENV_DIR / "migration_manifest.json").read_text(encoding="utf-8"))
    usage = final["version_usage"]
    switches = [u for u in usage if u["action"] == "switch"]
    rollbacks = [u for u in usage if u["action"] == "rollback"]
    runs = [u for u in usage if u["action"] == "run"]
    check("版本使用结果已回写迁移清单（switch/rollback/run）",
          len(switches) == 3 and len(rollbacks) == 3 and len(runs) == 8,
          f"switch={len(switches)} rollback={len(rollbacks)} run={len(runs)}")
    check("所有规则最终回到 v1",
          all(s["active_version"] == 1 for s in final["rules"].values()))
    check("R-LOG 的 v2 运行在清单中记录了被抑制的字段",
          any(u["rule_id"] == "R-LOG" and u["version"] == 2
              and u["fields_suppressed"] == ["device_id"] for u in runs))
    check("清单仍解释留在旧路径的规则及其切换条件",
          final["rules"]["R-LOG"]["decision"] == "stays_legacy"
          and bool(final["rules"]["R-LOG"]["switch_conditions"]))

    failed = [name for name, ok, _ in CHECKS if not ok]
    print(f"\n数据文件：{ENV_DIR}/contracts、{ENV_DIR}/deliveries")
    print(f"迁移清单：{ENV_DIR}/migration_manifest.json")
    print(f"结果：{len(CHECKS) - len(failed)}/{len(CHECKS)} 项通过")
    if failed:
        print("失败项：")
        for name in failed:
            print(f"  - {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
