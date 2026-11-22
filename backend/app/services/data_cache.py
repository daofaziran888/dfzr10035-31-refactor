"""数据缓存领域服务：管理结果缓存策略，与 data_delivery、subscriber、schema_diff 联动。"""
from __future__ import annotations

from app.services.base import BaseModuleService
from app.store import store


class DataCacheService(BaseModuleService):
    MODULE = "data_cache"
    ENTITY = "缓存策略"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["策略编号", "目标端点", "缓存TTL", "缓存键", "最大条目", "淘汰算法", "命中次数", "最后刷新"]
    REQUIRED_FIELDS = ["策略编号", "目标端点", "缓存TTL"]
    STATUS_ORDER = ["草稿", "生效中", "容量告警", "已停用"]
    ACTION_RULES = {"启效缓存": "生效中", "强制刷新": "容量告警", "停用缓存": "已停用"}
    NEGATIVE_ACTIONS = ["停用缓存"]

    def run_action(self, entry_id: int, action: str, action_id: str | None = None):
        """启效缓存时同步投递管道；强制刷新时通知订阅方；停用缓存时更新Schema差异。"""
        entry, message = super().run_action(entry_id, action)
        if entry is None:
            return entry, message
        if action == "启效缓存":
            self._sync_delivery_pipeline(entry)
            message += "，已同步数据投递管道"
        elif action == "强制刷新":
            self._notify_subscribers(entry)
            message += "，已通知所有订阅方"
        elif action == "停用缓存":
            self._record_schema_diff(entry)
            message += "，已记录Schema差异"
        return entry, message

    def _sync_delivery_pipeline(self, cache: dict):
        """缓存生效后检查投递管道是否存在，不存在则创建。"""
        endpoint = cache.get("目标端点", "")
        exist = any(
            d.get("数据来源") == endpoint
            for d in store.rows("data_delivery")
        )
        if not exist:
            del_rows = store.rows("data_delivery")
            cid = str(cache["id"]).zfill(4)
            del_rows.append({
                "id": max((int(r.get("id", 0)) for r in del_rows), default=0) + 1,
                "管道编号": f"DATA-CACHE-{cid}",
                "数据来源": endpoint,
                "目标地址": f"cache://{endpoint}/v1",
                "投递格式": "JSON",
                "分区策略": f"ttl={cache.get('缓存TTL', '3600')}s",
                "投递频率": "on_cache_evict",
                "压缩方式": "snappy",
                "最后投递": "",
                "status": "待配置", "pending": True, "abnormal": False,
            })

    def _notify_subscribers(self, cache: dict):
        """缓存刷新通知所有订阅方和webhook路由。"""
        endpoint = cache.get("目标端点", "")
        for sub in store.rows("subscriber"):
            if sub.get("数据主题") == endpoint:
                sub["最后推送"] = "2026-11-17"
                sub["消费状态"] = "待消费"
        for wh in store.rows("webhook_router"):
            if wh.get("触发事件") and endpoint in str(wh.get("触发事件", "")):
                wh["最后触发"] = "2026-11-17"
                wh["投递状态"] = "待投递"

    def _record_schema_diff(self, cache: dict):
        """停用缓存时创建Schema差异记录。"""
        diff_rows = store.rows("schema_diff")
        cid = str(cache["id"]).zfill(4)
        diff_rows.append({
            "id": max((int(r.get("id", 0)) for r in diff_rows), default=0) + 1,
            "记录编号": f"SCHE-CACHE-{cid}",
            "关联端点": cache.get("目标端点", ""),
            "变更字段": "cache_policy",
            "旧定义": f"ttl={cache.get('缓存TTL', '')}",
            "新定义": "cached_disabled",
            "差异等级": "WARNING",
            "影响评估": f"缓存策略{cid}停用，下游需重新评估",
            "检测时间": "2026-11-17",
            "status": "已检测", "pending": True, "abnormal": False,
        })

    def calc_cache_hit_rate(self) -> dict:
        """跨模块缓存命中率统计：综合 data_cache + data_delivery + subscriber。"""
        caches = store.rows("data_cache")
        deliveries = store.rows("data_delivery")
        subscribers = store.rows("subscriber")
        total_hits = sum(int(c.get("命中次数", 0)) for c in caches)
        active = sum(1 for c in caches if c.get("status") == "生效中")
        return {
            "cache_count": len(caches),
            "active_caches": active,
            "total_hits": total_hits,
            "delivery_pipelines": len(deliveries),
            "active_subscribers": sum(1 for s in subscribers if s.get("status") == "推送中"),
            "hit_rate_ok": active > 0,
        }