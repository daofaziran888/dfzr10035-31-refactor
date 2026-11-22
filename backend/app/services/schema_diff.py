"""Schema差异检测领域服务：管理API Schema变更差异，与 endpoint_def、govern_report、data_version 联动。"""
from __future__ import annotations

import threading

from app.services.base import BaseModuleService
from app.store import store

# 建档口径共用的登记锁：保证同一动作标识的并发提交只登记一次。
_FILING_LOCK = threading.Lock()

# 升级前（无动作标识）记录在查核来源入口上的统一标注。
LEGACY_SOURCE = "升级前登记"


def file_schema_diff(
    *,
    number: str,
    endpoint: str,
    field: str,
    old: str,
    new: str,
    level: str,
    impact: str,
    detected_at: str,
    abnormal: bool,
    source: str,
    action_id: str | None = None,
) -> tuple[dict, bool]:
    """共同的Schema差异建档口径：投产发布/标记变更/启效规则统一经此写入待审差异清单。

    返回 (差异记录, 是否本次新登记)。携带动作标识的提交幂等：
    同一动作标识只登记一次，重复提交（含回执丢失后的重试）返回首次登记的那条，
    不新建记录，也绝不把已确认的记录改回已检测；匹配只认动作标识，
    不会把显示编号相同的旧记录误当成新动作的回执。
    """
    with _FILING_LOCK:
        rows = store.rows("schema_diff")
        if action_id:
            for row in rows:
                if row.get("动作标识") and row.get("动作标识") == action_id:
                    return row, False
        row = {
            "id": max((int(r.get("id", 0)) for r in rows), default=0) + 1,
            "记录编号": number,
            "关联端点": endpoint,
            "变更字段": field,
            "旧定义": old,
            "新定义": new,
            "差异等级": level,
            "影响评估": impact,
            "检测时间": detected_at,
            "status": "已检测", "pending": True, "abnormal": abnormal,
            "来源": source,
            "动作标识": action_id or "",
        }
        rows.append(row)
        return row, True


class SchemaDiffService(BaseModuleService):
    MODULE = "schema_diff"
    ENTITY = "差异记录"
    OPERATION_TERM = "关联"
    LIST_FIELDS = ["记录编号", "关联端点", "变更字段", "旧定义", "新定义", "差异等级", "影响评估", "检测时间"]
    REQUIRED_FIELDS = ["记录编号", "关联端点", "变更字段"]
    STATUS_ORDER = ["已检测", "评估中", "已确认", "已忽略"]
    ACTION_RULES = {"发起评估": "评估中", "确认变更": "已确认", "忽略差异": "已忽略"}
    NEGATIVE_ACTIONS = ["忽略差异"]

    def run_action(self, entry_id: int, action: str, action_id: str | None = None):
        """评估中时更新端点状态和治理报告；确认变更时创建数据版本；忽略差异时记录治理。"""
        if action == "确认变更":
            # 确认只能使用完整、可定位的那条差异：按 id 定位，且关键字段齐全。
            target = store.find(self.MODULE, entry_id)
            if target is None:
                return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
            missing = [
                field for field in self.REQUIRED_FIELDS
                if not str(target.get(field) or "").strip()
            ]
            if missing:
                return None, f"差异记录缺少{'、'.join(missing)}，记录不完整，不能确认变更"
        entry, message = super().run_action(entry_id, action)
        if entry is None:
            return entry, message
        if action == "发起评估":
            self._update_endpoint_status(entry, "Schema变更")
            self._create_govern_watch(entry)
            message += "，已更新端点状态并创建治理观察"
        elif action == "确认变更":
            self._create_data_version(entry)
            self._update_endpoint_status(entry, "已投产")
            message += "，已创建数据版本并恢复端点状态"
        elif action == "忽略差异":
            self._log_ignored_diff(entry)
        return entry, message

    def get_entry(self, entry_id: int):
        """历史差异详情：除记录本身外，带回该条差异确认生成的数据版本和核对依据。

        新旧记录都保留查核来源的入口；升级前缺少动作标识的记录按实际编号、
        定义和已确认的版本核对，缺少可证实关联的明确标为待核。
        """
        row = store.find(self.MODULE, entry_id)
        if row is None:
            return None
        detail = dict(row)
        if not detail.get("来源"):
            detail["来源"] = LEGACY_SOURCE
        version, basis = self._locate_confirmation_version(row)
        detail["核对依据"] = basis
        if row.get("status") == "已确认":
            detail["确认版本"] = version
            detail["核验状态"] = "已核实" if version is not None else "待核"
        else:
            detail["确认版本"] = None
            detail["核验状态"] = "未确认"
        return detail

    def _locate_confirmation_version(self, diff: dict) -> tuple[dict | None, str]:
        """按可证实的关联定位该条差异确认生成的数据版本。

        只认两种依据，且都锚定记录的实际编号（id），不按显示编号凑数：
        1. 版本上登记了指向本记录的来源差异ID（升级后确认生成）；
        2. 升级前口径：版本编号/创建原因由本记录 id 派生，且Schema摘要与本记录新定义一致。
        """
        did = int(diff.get("id", 0))
        tag = str(did).zfill(4)
        who = f"差异{diff.get('记录编号', '')}(id={did})"
        for ver in store.rows("data_version"):
            if ver.get("来源差异ID") is not None and str(ver.get("来源差异ID")) == str(did):
                return dict(ver), (
                    f"版本{ver.get('版本编号', '')}登记来源差异ID={did}，"
                    f"可证实由{who}确认生成"
                )
        for ver in store.rows("data_version"):
            if (
                ver.get("版本编号") == f"DATA-SCHEMA-{tag}"
                and ver.get("创建原因") == f"Schema差异{tag}确认变更"
                and ver.get("Schema摘要") == diff.get("新定义", "")
            ):
                return dict(ver), (
                    f"版本{ver.get('版本编号', '')}的编号与创建原因均指向{who}，"
                    f"且Schema摘要与该差异新定义一致（升级前口径核对）"
                )
        return None, f"未找到可证实由{who}确认生成的数据版本"

    def _update_endpoint_status(self, diff: dict, new_status: str):
        endpoint_id = diff.get("关联端点", "")
        for ep in store.rows("endpoint_def"):
            if ep.get("端点编号") == endpoint_id:
                ep["status"] = new_status
                ep["pending"] = new_status == "Schema变更"
                break

    def _create_govern_watch(self, diff: dict):
        gov_rows = store.rows("govern_report")
        did = str(diff["id"]).zfill(4)
        gov_rows.append({
            "id": max((int(r.get("id", 0)) for r in gov_rows), default=0) + 1,
            "报告编号": f"GOVE-SCHEMA-{did}",
            "评估范围": f"Schema差异评估:{diff.get('关联端点', '')}",
            "合规指标": "Schema变更合规性",
            "当前得分": 70.0,
            "改进事项": f"端点{diff.get('关联端点', '')}发生{diff.get('差异等级', '')}级变更",
            "评估周期": "2026-Q4",
            "负责人": "数据架构组",
            "生成时间": "2026-11-17",
            "status": "待生成", "pending": True, "abnormal": True,
        })

    def _create_data_version(self, diff: dict):
        """按所选差异记录创建数据版本：保留该差异的新定义和可回查来源。"""
        ver_rows = store.rows("data_version")
        did = str(diff["id"]).zfill(4)
        ver_rows.append({
            "id": max((int(r.get("id", 0)) for r in ver_rows), default=0) + 1,
            "版本编号": f"DATA-SCHEMA-{did}",
            "数据来源": diff.get("关联端点", ""),
            "行数统计": 0,
            "版本标签": "schema_upgrade_v2",
            "Schema摘要": diff.get("新定义", ""),
            "创建原因": f"Schema差异{did}确认变更",
            "依赖版本": diff.get("旧定义", ""),
            "创建时间": "2026-11-17",
            "status": "可用", "pending": False, "abnormal": False,
            "来源差异ID": diff["id"],
            "差异动作标识": diff.get("动作标识", ""),
        })

    def _log_ignored_diff(self, diff: dict):
        log_rows = store.rows("call_log")
        did = str(diff["id"]).zfill(4)
        log_rows.append({
            "id": max((int(r.get("id", 0)) for r in log_rows), default=0) + 1,
            "记录编号": f"CALL-SCHEMA-{did}",
            "接口端点": diff.get("关联端点", ""),
            "请求参数": f"ignore_diff:{did}",
            "响应状态": "ignored",
            "响应耗时(ms)": 0,
            "返回行数": 0,
            "错误信息": f"Schema差异{did}已忽略",
            "调用时间": "2026-11-17",
            "status": "成功", "pending": False, "abnormal": False,
        })

    def calc_schema_health(self) -> dict:
        """跨模块Schema健康度。"""
        diffs = store.rows("schema_diff")
        endpoints = store.rows("endpoint_def")
        govern_reports = [g for g in store.rows("govern_report") if "SCHEMA" in str(g.get("报告编号", ""))]
        return {
            "total_diffs": len(diffs),
            "pending_assessment": sum(1 for d in diffs if d.get("status") == "已检测"),
            "confirmed_changes": sum(1 for d in diffs if d.get("status") == "已确认"),
            "endpoints_affected": sum(1 for ep in endpoints if ep.get("status") == "Schema变更"),
            "govern_items": len(govern_reports),
        }