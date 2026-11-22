"""响应解析领域服务：管理响应解析规则，与Schema差异检测、数据缓存、数据投递联动。"""
from __future__ import annotations

from app.services.base import BaseModuleService
from app.services.schema_diff import file_schema_diff
from app.store import store


class ResponseParseService(BaseModuleService):
    MODULE = "response_parse"
    ENTITY = "解析规则"
    OPERATION_TERM = "解析"
    LIST_FIELDS = ["规则编号", "目标端点", "JSON路径", "字段别名", "数据类型", "默认值", "解析顺序", "映射描述"]
    REQUIRED_FIELDS = ["规则编号", "目标端点", "JSON路径"]
    STATUS_ORDER = ["草稿", "生效中", "解析异常", "已废弃"]
    ACTION_RULES = {"启效规则": "生效中", "标记异常": "解析异常", "废弃规则": "已废弃"}
    NEGATIVE_ACTIONS = []

    def run_action(self, entry_id: int, action: str, action_id: str | None = None):
        """启效规则时同步创建Schema差异检测记录；标记异常时写调用日志。"""
        entry, message = super().run_action(entry_id, action)
        if entry is None:
            return entry, message
        if action == "启效规则":
            self._trigger_schema_diff(entry, action_id=action_id)
            self._sync_cache_policy(entry)
            message += "，已触发Schema差异检测并刷新缓存策略"
        elif action == "标记异常":
            self._log_parse_error(entry)
        elif action == "废弃规则":
            self._trigger_data_delivery_retry(entry)
        return entry, message

    def _trigger_schema_diff(self, rule: dict, action_id: str | None = None):
        """字段映射事件转Schema差异：与端点投产复用共同的建档口径，动作标识幂等。"""
        rid = str(rule["id"]).zfill(4)
        file_schema_diff(
            number=f"SCHE-PARSE-{rid}",
            endpoint=rule.get("目标端点", ""),
            field=rule.get("JSON路径", ""),
            old="pre_parse",
            new=rule.get("映射描述", ""),
            level="INFO",
            impact=f"解析规则{rid}生效触发",
            detected_at="2026-11-17",
            abnormal=False,
            source="response_parse:启效规则",
            action_id=action_id,
        )

    def _sync_cache_policy(self, rule: dict):
        """解析规则变更后更新关联缓存策略的命中标记。"""
        endpoint = rule.get("目标端点", "")
        for cache in store.rows("data_cache"):
            if cache.get("目标端点") == endpoint:
                cache["最后刷新"] = "2026-11-17"
                cache["命中次数"] = str(int(cache.get("命中次数", 0)) + 1)

    def _log_parse_error(self, rule: dict):
        """解析异常时写入调用日志。"""
        log_rows = store.rows("call_log")
        rid = str(rule["id"]).zfill(4)
        log_rows.append({
            "id": max((int(r.get("id", 0)) for r in log_rows), default=0) + 1,
            "记录编号": f"CALL-PARSE-{rid}",
            "接口端点": rule.get("目标端点", ""),
            "请求参数": rule.get("JSON路径", ""),
            "响应状态": "parse_error",
            "响应耗时(ms)": 0,
            "返回行数": 0,
            "错误信息": f"解析规则{rid}异常",
            "调用时间": "2026-11-17",
            "status": "超时", "pending": True, "abnormal": True,
        })

    def _trigger_data_delivery_retry(self, rule: dict):
        """废弃规则时通知数据投递管道重新采集。"""
        endpoint = rule.get("目标端点", "")
        for delivery in store.rows("data_delivery"):
            if delivery.get("数据来源") == endpoint:
                delivery["status"] = "投递中"
                delivery["pending"] = True
                delivery["最后投递"] = "待重采"