"""抽样校验领域服务：管理数据抽样校验，与 call_log、govern_report、data_version、cost_account 联动。"""
from __future__ import annotations

from app.services.base import BaseModuleService
from app.store import store


class SampleCheckService(BaseModuleService):
    MODULE = "sample_check"
    ENTITY = "抽样计划"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["计划编号", "目标端点", "抽样比例", "校验字段", "参考基线", "偏差阈值", "样本数量", "最近采样"]
    REQUIRED_FIELDS = ["计划编号", "目标端点", "抽样比例"]
    STATUS_ORDER = ["待采样", "采样中", "已通过", "偏差告警"]
    ACTION_RULES = {"启动采样": "采样中", "确认通过": "已通过", "标记告警": "偏差告警"}
    NEGATIVE_ACTIONS = []

    def run_action(self, entry_id: int, action: str, action_id: str | None = None):
        """采样中时从调用日志取样并创建数据版本；确认通过时更新用量；告警时联动治理和重试。"""
        entry, message = super().run_action(entry_id, action)
        if entry is None:
            return entry, message
        if action == "启动采样":
            self._sample_from_call_logs(entry)
            self._create_data_version(entry)
            message += "，已从调用日志取样并创建数据版本"
        elif action == "确认通过":
            self._update_cost_quota(entry)
            message += "，已更新用量配额"
        elif action == "标记告警":
            self._trigger_govern_and_retry(entry)
            message += "，已触发治理报告和重放任务"
        return entry, message

    def _sample_from_call_logs(self, check: dict):
        """从调用日志中抽取样本记录。"""
        endpoint = check.get("目标端点", "")
        ratio = float(str(check.get("抽样比例", "10")).replace("%", "")) / 100.0
        logs = [l for l in store.rows("call_log") if l.get("接口端点") == endpoint]
        sample_count = max(1, int(len(logs) * ratio))
        check["样本数量"] = sample_count
        check["最近采样"] = "2026-11-17"

    def _create_data_version(self, check: dict):
        ver_rows = store.rows("data_version")
        sid = str(check["id"]).zfill(4)
        ver_rows.append({
            "id": max((int(r.get("id", 0)) for r in ver_rows), default=0) + 1,
            "版本编号": f"DATA-SAMP-{sid}",
            "数据来源": check.get("目标端点", ""),
            "行数统计": check.get("样本数量", 0),
            "版本标签": "sample_check_baseline",
            "Schema摘要": f"抽样{check.get('抽样比例', '10')}%",
            "创建原因": f"抽样计划{sid}采样完成",
            "依赖版本": check.get("参考基线", ""),
            "创建时间": "2026-11-17",
            "status": "可用", "pending": True, "abnormal": False,
        })

    def _update_cost_quota(self, check: dict):
        """通过后更新用量账单中关联端点的消费预估。"""
        endpoint = check.get("目标端点", "")
        for cost in store.rows("cost_account"):
            if cost.get("提供方") and endpoint in str(cost.get("提供方", "")):
                cost["调用次数"] = int(cost.get("调用次数", 0)) + check.get("样本数量", 0)
                cost["费用预估"] = float(cost.get("费用预估", 0.0)) + check.get("样本数量", 0) * 0.01

    def _trigger_govern_and_retry(self, check: dict):
        """偏差告警时创建治理报告和重放任务。"""
        sid = str(check["id"]).zfill(4)
        endpoint = check.get("目标端点", "")
        # 治理报告
        gov_rows = store.rows("govern_report")
        gov_rows.append({
            "id": max((int(r.get("id", 0)) for r in gov_rows), default=0) + 1,
            "报告编号": f"GOVE-SAMP-{sid}",
            "评估范围": f"抽样偏差告警:{endpoint}",
            "合规指标": "数据质量一致性",
            "当前得分": 55.0,
            "改进事项": f"端点{endpoint}抽样偏差超出阈值{check.get('偏差阈值', '')}",
            "评估周期": "2026-Q4",
            "负责人": "数据质量组",
            "生成时间": "2026-11-17",
            "status": "待生成", "pending": True, "abnormal": True,
        })
        # 重放任务
        retry_rows = store.rows("retry_queue")
        retry_rows.append({
            "id": max((int(r.get("id", 0)) for r in retry_rows), default=0) + 1,
            "任务编号": f"RETR-SAMP-{sid}",
            "失败端点": endpoint,
            "失败原因": f"抽样偏差告警:{check.get('校验字段', '')}",
            "重试次数": 0,
            "最大重试": 3,
            "退避策略": "linear:5s",
            "最后重试": "",
            "队列状态": "sample_deviation",
            "status": "待重试", "pending": True, "abnormal": True,
        })

    def calc_sampling_coverage(self) -> dict:
        """跨模块抽样覆盖率统计。"""
        checks = store.rows("sample_check")
        versions = [v for v in store.rows("data_version") if "SAMP" in str(v.get("版本编号", ""))]
        govern = [g for g in store.rows("govern_report") if "SAMP" in str(g.get("报告编号", ""))]
        return {
            "total_plans": len(checks),
            "active_sampling": sum(1 for c in checks if c.get("status") == "采样中"),
            "deviations": sum(1 for c in checks if c.get("status") == "偏差告警"),
            "version_snapshots": len(versions),
            "govern_items": len(govern),
        }