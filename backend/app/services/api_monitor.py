"""接口监控领域服务：管理API监控指标，与 call_log、retry_queue、rate_limit、govern_report 联动。"""
from __future__ import annotations

from app.services.base import BaseModuleService
from app.store import store


class ApiMonitorService(BaseModuleService):
    MODULE = "api_monitor"
    ENTITY = "监控指标"
    OPERATION_TERM = "采集"
    LIST_FIELDS = ["指标编号", "目标端点", "监控类型", "采集间隔", "告警阈值", "告警渠道", "静默时段", "最近采样"]
    REQUIRED_FIELDS = ["指标编号", "目标端点", "监控类型"]
    STATUS_ORDER = ["待启用", "监控中", "已告警", "已静默"]
    ACTION_RULES = {"启用监控": "监控中", "静默告警": "已告警", "废弃监控": "已静默"}
    NEGATIVE_ACTIONS = []

    def run_action(self, entry_id: int, action: str, action_id: str | None = None):
        """启用监控时采集调用日志统计；已告警时联动重放队列和治理报告。"""
        entry, message = super().run_action(entry_id, action)
        if entry is None:
            return entry, message
        if action == "启用监控":
            self._collect_call_log_stats(entry)
            message += "，已采集调用日志统计"
        elif action == "静默告警":
            self._on_alert_trigger(entry)
            message += "，已创建重放任务和治理报告"
        return entry, message

    def _collect_call_log_stats(self, monitor: dict):
        """启用时从 call_log 读取关联端点统计数据。"""
        endpoint = monitor.get("目标端点", "")
        logs = [l for l in store.rows("call_log") if l.get("接口端点") == endpoint]
        total = len(logs)
        success = sum(1 for l in logs if l.get("status") == "成功")
        timeout = sum(1 for l in logs if l.get("status") == "超时")
        rate_limited = sum(1 for l in logs if l.get("status") == "限流拒绝")
        monitor["最近采样"] = "2026-11-17"
        monitor["stats"] = {
            "total": total, "success": success,
            "timeout": timeout, "rate_limited": rate_limited,
        }

    def _on_alert_trigger(self, monitor: dict):
        """告警触发时联动创建重放任务和治理报告。"""
        endpoint = monitor.get("目标端点", "")
        mid = str(monitor["id"]).zfill(4)
        # 创建重放任务
        retry_rows = store.rows("retry_queue")
        retry_rows.append({
            "id": max((int(r.get("id", 0)) for r in retry_rows), default=0) + 1,
            "任务编号": f"RETR-MON-{mid}",
            "失败端点": endpoint,
            "失败原因": f"监控告警:{monitor.get('告警阈值', '')}",
            "重试次数": 0,
            "最大重试": 3,
            "退避策略": "fixed:10s",
            "最后重试": "",
            "队列状态": "告警触发",
            "status": "待重试", "pending": True, "abnormal": True,
        })
        # 创建治理报告
        gov_rows = store.rows("govern_report")
        gov_rows.append({
            "id": max((int(r.get("id", 0)) for r in gov_rows), default=0) + 1,
            "报告编号": f"GOVE-MON-{mid}",
            "评估范围": f"监控告警:{endpoint}",
            "合规指标": "API可用性",
            "当前得分": 40.0,
            "改进事项": f"端点{endpoint}触发告警，阈值{monitor.get('告警阈值', '')}",
            "评估周期": "2026-Q4",
            "负责人": "值班SRE",
            "生成时间": "2026-11-17",
            "status": "待生成", "pending": True, "abnormal": True,
        })

    def analyze_chain_health(self) -> dict:
        """跨模块链路健康分析：汇总监控+日志+重试+限流+治理全景。"""
        monitors = store.rows("api_monitor")
        call_logs = store.rows("call_log")
        retries = store.rows("retry_queue")
        rate_limits = store.rows("rate_limit")
        gov_reports = store.rows("govern_report")
        return {
            "monitor_metrics": sum(1 for m in monitors if m.get("status") == "监控中"),
            "alert_count": sum(1 for m in monitors if m.get("status") == "已告警"),
            "total_calls": len(call_logs),
            "abnormal_calls": sum(1 for c in call_logs if c.get("abnormal")),
            "pending_retries": sum(1 for r in retries if r.get("status") == "待重试"),
            "circuit_broken": sum(1 for rl in rate_limits if rl.get("status") == "已熔断"),
            "govern_issues": sum(1 for g in gov_reports if g.get("status") == "待生成"),
            "overall_health": "healthy" if sum(1 for m in monitors if m.get("status") == "已告警") < 3 else "degraded",
        }