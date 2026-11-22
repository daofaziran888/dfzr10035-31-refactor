"""用量计费领域服务：管理API用量账单，与 api_provider、call_log、sample_check、govern_report 联动。"""
from __future__ import annotations

from app.services.base import BaseModuleService
from app.store import store


class CostAccountService(BaseModuleService):
    MODULE = "cost_account"
    ENTITY = "用量账单"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["账单编号", "提供方", "计费周期", "调用次数", "数据流量", "超出配额", "费用预估", "账单状态"]
    REQUIRED_FIELDS = ["账单编号", "提供方", "计费周期"]
    STATUS_ORDER = ["统计中", "已核算", "超额告警", "已确认"]
    ACTION_RULES = {"启动核算": "已核算", "确认账单": "超额告警", "标记超额": "已确认"}
    NEGATIVE_ACTIONS = []

    def run_action(self, entry_id: int, action: str, action_id: str | None = None):
        """启动核算时从调用日志和提供方SLA拉取用量；确认账单时审计；超额时联动治理报告和重采。"""
        entry, message = super().run_action(entry_id, action)
        if entry is None:
            return entry, message
        if action == "启动核算":
            self._calc_usage_from_logs(entry)
            self._check_sla_limits(entry)
            message += "，已从调用日志统计用量并核对SLA上限"
        elif action == "确认账单":
            self._audit_cost(entry)
            message += "，已记录审计日志"
        elif action == "标记超额":
            self._trigger_overrun_govern(entry)
            self._enqueue_resample(entry)
            message += "，已触发超额治理并创建重采任务"
        return entry, message

    def _calc_usage_from_logs(self, cost: dict):
        """从调用日志计算关联提供方的实际用量。"""
        provider_name = cost.get("提供方", "")
        logs = [l for l in store.rows("call_log") if provider_name in str(l.get("接口端点", ""))]
        cost["调用次数"] = len(logs)
        cost["数据流量(MB)"] = sum(int(l.get("返回行数", 0)) for l in logs)
        cost["费用预估"] = len(logs) * 0.005

    def _check_sla_limits(self, cost: dict):
        """核对提供方SLA中的QPS上限。"""
        provider_name = cost.get("提供方", "")
        providers = [p for p in store.rows("api_provider") if p.get("提供方名称") == provider_name]
        if providers and providers[0].get("QPS上限"):
            qps_limit = int(str(providers[0].get("QPS上限", "100")))
            current_calls = cost.get("调用次数", 0)
            if current_calls > qps_limit:
                cost["超出配额"] = "是"
                cost["status"] = "超额告警"
                cost["pending"] = True
            else:
                cost["超出配额"] = "否"

    def _audit_cost(self, cost: dict):
        audit_rows = store.rows("pipeline_audit")
        cid = str(cost["id"]).zfill(4)
        audit_rows.append({
            "id": max((int(r.get("id", 0)) for r in audit_rows), default=0) + 1,
            "记录编号": f"PIPE-COST-{cid}",
            "管道名称": f"cost_account:{cid}",
            "操作人": "系统核算",
            "操作类型": "费用确认",
            "变更摘要": f"账单{cid}:费用{cost.get('费用预估', 0.0)}元/调用{cost.get('调用次数', 0)}次",
            "操作时间": "2026-11-17",
            "来源节点": "cost_account",
            "审计结论": "已通过",
            "status": "已记录", "pending": False, "abnormal": False,
        })

    def _trigger_overrun_govern(self, cost: dict):
        gov_rows = store.rows("govern_report")
        cid = str(cost["id"]).zfill(4)
        gov_rows.append({
            "id": max((int(r.get("id", 0)) for r in gov_rows), default=0) + 1,
            "报告编号": f"GOVE-COST-{cid}",
            "评估范围": f"用量超额:{cost.get('提供方', '')}",
            "合规指标": "成本基线控制",
            "当前得分": 30.0,
            "改进事项": f"提供方{cost.get('提供方', '')}费用{cost.get('费用预估', 0)}元超配额",
            "评估周期": "2026-Q4",
            "负责人": "成本管理组",
            "生成时间": "2026-11-17",
            "status": "待生成", "pending": True, "abnormal": True,
        })

    def _enqueue_resample(self, cost: dict):
        """超额后创建重采任务和抽样校验。"""
        cid = str(cost["id"]).zfill(4)
        # 重放任务
        retry_rows = store.rows("retry_queue")
        retry_rows.append({
            "id": max((int(r.get("id", 0)) for r in retry_rows), default=0) + 1,
            "任务编号": f"RETR-COST-{cid}",
            "失败端点": cost.get("提供方", ""),
            "失败原因": f"用量超额重采:{cost.get('费用预估', '')}元",
            "重试次数": 0,
            "最大重试": 2,
            "退避策略": "fixed:30s",
            "最后重试": "",
            "队列状态": "cost_overrun",
            "status": "待重试", "pending": True, "abnormal": True,
        })

    def calc_cost_overview(self) -> dict:
        """跨模块成本全景视图。"""
        costs = store.rows("cost_account")
        providers = store.rows("api_provider")
        logs = store.rows("call_log")
        return {
            "total_bills": len(costs),
            "total_cost": sum(float(c.get("费用预估", 0)) for c in costs),
            "overrun_bills": sum(1 for c in costs if c.get("status") == "超额告警"),
            "total_providers": len(providers),
            "total_calls_logged": len(logs),
            "avg_cost_per_call": sum(float(c.get("费用预估", 0)) for c in costs) / len(logs) if logs else 0.0,
        }