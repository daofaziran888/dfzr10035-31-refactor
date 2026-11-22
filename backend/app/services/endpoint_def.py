"""接口端点定义领域服务：管理API端点，与 api_provider、response_parse、rate_limit、schema_diff 联动。"""
from __future__ import annotations

from app.services.base import BaseModuleService
from app.services.schema_diff import file_schema_diff
from app.store import store


class EndpointDefService(BaseModuleService):
    MODULE = "endpoint_def"
    ENTITY = "接口端点"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["端点编号", "提供方", "请求路径", "请求方法", "参数Schema", "响应Schema", "分页策略", "描述文档"]
    REQUIRED_FIELDS = ["端点编号", "提供方", "请求路径"]
    STATUS_ORDER = ["草稿", "联调中", "已投产", "Schema变更"]
    ACTION_RULES = {"提交联调": "联调中", "投产发布": "已投产", "标记变更": "Schema变更"}
    NEGATIVE_ACTIONS = []

    def run_action(self, entry_id: int, action: str, action_id: str | None = None):
        """投产发布时联动创建响应解析规则和Schema差异记录；Schema变更时通知缓存刷新。"""
        entry, message = super().run_action(entry_id, action)
        if entry is None:
            return entry, message
        if action == "投产发布":
            self._create_parse_rule(entry)
            self._create_schema_diff(entry, "initial", action_id=action_id)
            self._sync_rate_limit_creation(entry)
            message += "，已联动创建解析规则/Schema记录/限流策略"
        elif action == "标记变更":
            self._create_schema_diff(entry, "upgrade", action_id=action_id)
            self._notify_cache_refresh(entry)
            message += "，已记录Schema差异并通知缓存刷新"
        return entry, message

    def _create_parse_rule(self, endpoint: dict):
        """投产时自动创建响应解析规则。"""
        parse_rows = store.rows("response_parse")
        eid = str(endpoint["id"]).zfill(4)
        parse_rows.append({
            "id": max((int(r.get("id", 0)) for r in parse_rows), default=0) + 1,
            "规则编号": f"RESP-{endpoint['端点编号']}",
            "目标端点": endpoint["端点编号"],
            "JSON路径": "$.data[*]",
            "字段别名": "source_field",
            "数据类型": "string",
            "默认值": "",
            "解析顺序": "1",
            "映射描述": f"{endpoint.get('请求路径', '')}响应解析",
            "status": "草稿", "pending": True, "abnormal": False,
        })

    def _create_schema_diff(self, endpoint: dict, reason: str, action_id: str | None = None):
        """端点变化转Schema差异：与启效规则复用共同的建档口径，动作标识幂等。"""
        eid = str(endpoint["id"]).zfill(4)
        file_schema_diff(
            number=f"SCHE-{endpoint['端点编号']}",
            endpoint=endpoint["端点编号"],
            field="responseSchema" if reason == "upgrade" else "initialSchema",
            old=endpoint.get("响应Schema", "") if reason == "upgrade" else "",
            new=endpoint.get("响应Schema", "") if reason == "initial" else "upgraded",
            level="MAJOR" if reason == "upgrade" else "INFO",
            impact=f"端点{eid}投产{'变更' if reason == 'upgrade' else '初始化'}",
            detected_at="2026-11-17",
            abnormal=reason == "upgrade",
            source=f"endpoint_def:{'投产发布' if reason == 'initial' else '标记变更'}",
            action_id=action_id,
        )

    def _sync_rate_limit_creation(self, endpoint: dict):
        """确保投产端点有关联限流策略。"""
        endpoint_id = endpoint["端点编号"]
        exist = any(
            rl.get("目标端点") == endpoint_id
            for rl in store.rows("rate_limit")
        )
        if not exist:
            rl_rows = store.rows("rate_limit")
            eid = str(endpoint["id"]).zfill(4)
            rl_rows.append({
                "id": max((int(r.get("id", 0)) for r in rl_rows), default=0) + 1,
                "策略编号": f"RATE-{endpoint_id}",
                "目标端点": endpoint_id,
                "QPS上限": "100",
                "滑动窗口": "60s",
                "熔断阈值(fail%)": 30.0,
                "恢复时长(s)": 60,
                "排队队列": "FIFO-1000",
                "当前状态": "草稿",
                "status": "草稿", "pending": True, "abnormal": False,
            })

    def _notify_cache_refresh(self, endpoint: dict):
        """Schema变更通知关联缓存刷新。"""
        endpoint_id = endpoint["端点编号"]
        for cache in store.rows("data_cache"):
            if cache.get("目标端点") == endpoint_id:
                cache["最后刷新"] = "2026-11-17"
                cache["status"] = "容量告警"
                cache["pending"] = True

    def check_endpoint_chain(self, endpoint_id: str) -> dict:
        """跨模块端点链路检查：端点→提供方→限流→解析规则→Schema差异。"""
        endpoints = [e for e in store.rows("endpoint_def") if e.get("端点编号") == endpoint_id]
        if not endpoints:
            return {"ok": False, "message": "端点不存在"}
        ep = endpoints[0]
        provider = ep.get("提供方", "")
        rate_limits = [rl for rl in store.rows("rate_limit") if rl.get("目标端点") == endpoint_id]
        parse_rules = [p for p in store.rows("response_parse") if p.get("目标端点") == endpoint_id]
        schema_diffs = [sd for sd in store.rows("schema_diff") if sd.get("关联端点") == endpoint_id]
        return {
            "endpoint": endpoint_id,
            "provider": provider,
            "rate_limits": len(rate_limits),
            "parse_rules": len(parse_rules),
            "schema_diffs": len(schema_diffs),
            "production_ready": len(rate_limits) > 0 and len(parse_rules) > 0,
        }