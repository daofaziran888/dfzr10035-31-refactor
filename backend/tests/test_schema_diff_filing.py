"""Schema差异建档共用口径的恢复与核对测试（同一服务进程内执行）。

覆盖场景：
A. 投产登记完成、动作返回前中断（回执丢失）→ 同动作标识重试不重复登记、不改回已确认；
   并发提交同一动作标识只登记一次；新动作标识的启效生成新的待审记录。
B. 升级前缺少动作标识的同号记录：按编号回查分别列出，重试只返回带本次动作标识的记录。
C. 历史差异详情：已采用版本只返回可证实关联的那一条；依据被删后显示待核，不拿同号版本补位。

运行：cd backend && .venv/bin/python tests/test_schema_diff_filing.py
"""
from __future__ import annotations

import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.endpoint_def import EndpointDefService
from app.services.response_parse import ResponseParseService
from app.services.schema_diff import LEGACY_SOURCE, SchemaDiffService, file_schema_diff
from app.store import store

endpoint_svc = EndpointDefService()
parse_svc = ResponseParseService()
diff_svc = SchemaDiffService()

PASSED = 0


def check(label: str, condition: bool):
    global PASSED
    assert condition, f"FAILED: {label}"
    PASSED += 1
    print(f"  ok - {label}")


def diffs_of(**conditions) -> list[dict]:
    rows = []
    for row in store.rows("schema_diff"):
        if all(row.get(k) == v for k, v in conditions.items()):
            rows.append(row)
    return rows


def versions_of(**conditions) -> list[dict]:
    rows = []
    for row in store.rows("data_version"):
        if all(row.get(k) == v for k, v in conditions.items()):
            rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# 场景A：投产回执丢失 → 同标识重试；启效各有差异；确认只命中所选记录
# ---------------------------------------------------------------------------
def scenario_a():
    print("场景A：回执丢失后的恢复（同一服务进程）")
    ep, missing = endpoint_svc.create_entry({
        "端点编号": "ENDP-E2E-0006", "提供方": "恢复测试源", "请求路径": "/api/e2e/recover",
        "请求方法": "GET", "参数Schema": "{}", "响应Schema": '{"code":"int","data":"array"}',
        "分页策略": "none", "描述文档": "回执丢失恢复测试端点",
    })
    assert not missing
    ep_id = ep["id"]

    # 1) 登记 E 的 initialSchema：登记完成、动作返回前中断（回执未返回）。
    endpoint_svc._create_schema_diff(ep, "initial", action_id="ACT-A-PROD")
    prod_diffs = diffs_of(关联端点="ENDP-E2E-0006", 变更字段="initialSchema")
    check("投产差异已登记且仅一条", len(prod_diffs) == 1)
    prod_diff = prod_diffs[0]
    check("投产差异保留原编号", prod_diff["记录编号"] == "SCHE-ENDP-E2E-0006")
    check("投产差异保留来源", prod_diff["来源"] == "endpoint_def:投产发布")
    check("投产差异保留动作标识", prod_diff["动作标识"] == "ACT-A-PROD")
    check("投产差异初始状态为已检测", prod_diff["status"] == "已检测")
    check("投产差异保留定义", prod_diff["新定义"] == '{"code":"int","data":"array"}' and prod_diff["旧定义"] == "")

    # 2) 回执丢失期间，启效 E 的 $.data[*] 规则，提交自己的差异。
    rule, missing = parse_svc.create_entry({
        "规则编号": "RESP-E2E-0006", "目标端点": "ENDP-E2E-0006", "JSON路径": "$.data[*]",
        "字段别名": "source_field", "数据类型": "string", "默认值": "",
        "解析顺序": "1", "映射描述": "E2E响应解析映射",
    })
    assert not missing
    rule_id = rule["id"]
    _, msg = parse_svc.run_action(rule_id, "启效规则", action_id="ACT-A-PARSE")
    check("启效动作回执文案不变", msg == "解析规则已启效规则，已触发Schema差异检测并刷新缓存策略")
    parse_diffs = diffs_of(关联端点="ENDP-E2E-0006", 变更字段="$.data[*]")
    check("启效差异独立登记", len(parse_diffs) == 1)
    parse_diff = parse_diffs[0]
    check("启效差异保留原编号", parse_diff["记录编号"] == f"SCHE-PARSE-{str(rule_id).zfill(4)}")
    check("启效差异保留来源", parse_diff["来源"] == "response_parse:启效规则")
    check("启效差异保留定义", parse_diff["旧定义"] == "pre_parse" and parse_diff["新定义"] == "E2E响应解析映射")
    check("两条差异各自独立", parse_diff["id"] != prod_diff["id"])

    # 3) 并发提交同一动作标识（含一个全新标识）只登记一次。
    results: list[tuple[int, bool]] = []
    barrier = threading.Barrier(8)

    def submit():
        barrier.wait()
        row, created = file_schema_diff(
            number="SCHE-ENDP-E2E-0006", endpoint="ENDP-E2E-0006", field="initialSchema",
            old="", new='{"code":"int","data":"array"}', level="INFO",
            impact="端点0006投产初始化", detected_at="2026-11-17", abnormal=False,
            source="endpoint_def:投产发布", action_id="ACT-A-CONC",
        )
        results.append((row["id"], created))

    threads = [threading.Thread(target=submit) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check("并发提交只登记一次", sum(1 for _, created in results if created) == 1)
    check("并发回执指向同一条记录", len({rid for rid, _ in results}) == 1)
    check("并发标识只产生一条差异", len(diffs_of(动作标识="ACT-A-CONC")) == 1)

    # 4) 值班员打开待审差异清单，确认投产记录。
    pending = [d for d in store.rows("schema_diff") if d.get("status") == "已检测"]
    check("待审清单能同时看到投产与启效差异",
          any(d["id"] == prod_diff["id"] for d in pending) and any(d["id"] == parse_diff["id"] for d in pending))
    entry, msg = diff_svc.run_action(prod_diff["id"], "确认变更")
    check("确认投产差异成功", entry is not None and prod_diff["status"] == "已确认")
    v1s = versions_of(来源差异ID=prod_diff["id"])
    check("投产确认生成数据版本", len(v1s) == 1)
    check("投产版本保留新定义与可回查来源",
          v1s[0]["Schema摘要"] == '{"code":"int","data":"array"}' and v1s[0]["版本编号"] == f"DATA-SCHEMA-{str(prod_diff['id']).zfill(4)}")
    check("启效差异未被投产确认波及", parse_diff["status"] == "已检测")

    # 5) 用同一动作标识重试投产：不重复登记，已确认记录不被改回已检测。
    diff_count_before = len(store.rows("schema_diff"))
    version_count_before = len(store.rows("data_version"))
    entry, msg = endpoint_svc.run_action(ep_id, "投产发布", action_id="ACT-A-PROD")
    check("重试投产动作正常返回", entry is not None and msg == "接口端点已投产发布，已联动创建解析规则/Schema记录/限流策略")
    check("重试不重复登记差异", len(store.rows("schema_diff")) == diff_count_before)
    check("重试不重复生成版本", len(store.rows("data_version")) == version_count_before)
    check("重试不把已确认改回已检测", prod_diff["status"] == "已确认")
    check("重试不新增同标识记录", len(diffs_of(动作标识="ACT-A-PROD")) == 1)

    # 6) 确认启效记录。
    entry, _ = diff_svc.run_action(parse_diff["id"], "确认变更")
    check("确认启效差异成功", entry is not None and parse_diff["status"] == "已确认")
    v2s = versions_of(来源差异ID=parse_diff["id"])
    check("启效确认生成数据版本", len(v2s) == 1)
    check("启效版本保留各自新定义与来源",
          v2s[0]["Schema摘要"] == "E2E响应解析映射" and v2s[0]["差异动作标识"] == "ACT-A-PARSE")
    check("两个版本各自可回查", v1s[0]["id"] != v2s[0]["id"])

    # 7) 按新动作标识启效同一规则：新的待审记录，先前记录仍已确认。
    parse_svc.run_action(rule_id, "启效规则", action_id="ACT-A-PARSE-2")
    new_diffs = diffs_of(动作标识="ACT-A-PARSE-2")
    check("新动作标识登记新的待审记录", len(new_diffs) == 1 and new_diffs[0]["status"] == "已检测")
    check("新记录沿用同一显示编号", new_diffs[0]["记录编号"] == parse_diff["记录编号"])
    check("新记录不沿用旧动作的来源标识", new_diffs[0]["动作标识"] != parse_diff["动作标识"])
    check("先前启效记录仍已确认", parse_diff["status"] == "已确认")

    # 8) 确认只能使用完整、可定位的那条差异。
    bad = {"id": max(int(r["id"]) for r in store.rows("schema_diff")) + 1,
           "记录编号": "SCHE-BROKEN", "关联端点": "ENDP-E2E-0006", "变更字段": "",
           "旧定义": "", "新定义": "x", "差异等级": "INFO", "影响评估": "", "检测时间": "2026-11-17",
           "status": "已检测", "pending": True, "abnormal": False, "来源": "测试", "动作标识": ""}
    store.rows("schema_diff").append(bad)
    entry, msg = diff_svc.run_action(bad["id"], "确认变更")
    check("不完整差异不能确认", entry is None and "不完整" in msg and bad["status"] == "已检测")
    entry, msg = diff_svc.run_action(999999, "确认变更")
    check("不可定位的差异不能确认", entry is None and "不存在" in msg)


# ---------------------------------------------------------------------------
# 场景B：升级前的同号记录 —— 分别列出，重试只认本次动作标识
# ---------------------------------------------------------------------------
def scenario_b():
    print("场景B：升级前同号记录的回查与重试")
    legacy = {"id": max(int(r["id"]) for r in store.rows("schema_diff")) + 1,
              "记录编号": "SCHE-PARSE-0001", "关联端点": "ENDP-LEG-0001", "变更字段": "$.data[*]",
              "旧定义": "pre_parse", "新定义": "legacy映射", "差异等级": "INFO",
              "影响评估": "解析规则0001生效触发", "检测时间": "2026-09-01",
              "status": "已确认", "pending": False, "abnormal": False}
    store.rows("schema_diff").append(legacy)

    # 新启动动作写出相同显示编号。
    parse_svc.run_action(1, "启效规则", action_id="ACT-B-1")
    fresh = diffs_of(动作标识="ACT-B-1")
    check("新动作写出相同显示编号", len(fresh) == 1 and fresh[0]["记录编号"] == "SCHE-PARSE-0001")
    fresh = fresh[0]

    # 按编号回查：同一显示编号分别列出。
    items, total = diff_svc.list_entries(filters={"记录编号": "SCHE-PARSE-0001"}, size=50)
    check("同号记录分别列出", total == 2 and {i["id"] for i in items} == {legacy["id"], fresh["id"]})
    old_item = next(i for i in items if i["id"] == legacy["id"])
    new_item = next(i for i in items if i["id"] == fresh["id"])
    check("旧记录保持原确认状态", old_item["status"] == "已确认")
    check("旧记录缺少动作标识", not old_item.get("动作标识"))
    check("新记录带来源与动作标识",
          new_item["来源"] == "response_parse:启效规则" and new_item["动作标识"] == "ACT-B-1" and new_item["status"] == "已检测")
    detail = diff_svc.get_entry(legacy["id"])
    check("旧记录保留查核来源入口", detail["来源"] == LEGACY_SOURCE)

    # 重试只返回带本次动作标识的记录，不把旧记录认成回执。
    row, created = file_schema_diff(
        number="SCHE-PARSE-0001", endpoint="响应解析样例1", field="响应解析样例1",
        old="pre_parse", new="响应解析样例1", level="INFO", impact="解析规则0001生效触发",
        detected_at="2026-11-17", abnormal=False, source="response_parse:启效规则",
        action_id="ACT-B-1",
    )
    check("重试不再登记", not created)
    check("重试只返回带本次动作标识的记录", row["id"] == fresh["id"] and row["动作标识"] == "ACT-B-1")
    parse_svc.run_action(1, "启效规则", action_id="ACT-B-1")
    items, total = diff_svc.list_entries(filters={"记录编号": "SCHE-PARSE-0001"}, size=50)
    check("服务层重试后同号仍只有两条", total == 2)
    check("旧记录未被重试改动", legacy["status"] == "已确认")
    return legacy, fresh


# ---------------------------------------------------------------------------
# 场景C：历史详情只返回可证实关联的版本；依据缺失则待核，不拿同号版本补位
# ---------------------------------------------------------------------------
def scenario_c(legacy: dict, fresh: dict):
    print("场景C：历史差异详情的版本核对")
    # 为旧差异准备它确实生成的 V1（升级前口径：编号/创建原因派生自记录 id，定义一致）。
    tag = str(legacy["id"]).zfill(4)
    v1 = {"id": max(int(r["id"]) for r in store.rows("data_version")) + 1,
          "版本编号": f"DATA-SCHEMA-{tag}", "数据来源": legacy["关联端点"], "行数统计": 0,
          "版本标签": "schema_upgrade_v2", "Schema摘要": legacy["新定义"],
          "创建原因": f"Schema差异{tag}确认变更", "依赖版本": legacy["旧定义"],
          "创建时间": "2026-09-02", "status": "可用", "pending": False, "abnormal": False}
    store.rows("data_version").append(v1)

    # 另一条同号差异确认生成 V2（升级后口径：带来源差异ID）。
    diff_svc.run_action(fresh["id"], "确认变更")
    v2s = versions_of(来源差异ID=fresh["id"])
    check("同号新差异确认生成V2", len(v2s) == 1 and v2s[0]["版本编号"] != v1["版本编号"])

    # 打开旧差异的历史详情：已采用版本只返回 V1，并说明采用了哪条差异。
    detail = diff_svc.get_entry(legacy["id"])
    check("旧差异核验状态为已核实", detail["核验状态"] == "已核实")
    check("已采用版本只返回V1", detail["确认版本"] is not None and detail["确认版本"]["版本编号"] == v1["版本编号"])
    check("核对依据说明采用了哪条差异",
          f"id={legacy['id']}" in detail["核对依据"] and legacy["记录编号"] in detail["核对依据"])
    check("V2没有补位到旧差异", detail["确认版本"]["版本编号"] != v2s[0]["版本编号"])

    new_detail = diff_svc.get_entry(fresh["id"])
    check("新差异详情按来源差异ID核到V2",
          new_detail["核验状态"] == "已核实" and new_detail["确认版本"]["版本编号"] == v2s[0]["版本编号"])

    # 删去能证明 V1 来源的依据：该项显示待核，且没有拿 V2 补位。
    del v1["创建原因"]
    detail = diff_svc.get_entry(legacy["id"])
    check("依据缺失后显示待核", detail["核验状态"] == "待核")
    check("待核时不返回任何确认版本", detail["确认版本"] is None)
    check("V2仍在但不被拿来补位", store.find("data_version", v2s[0]["id"]) is not None)


def main():
    scenario_a()
    legacy, fresh = scenario_b()
    scenario_c(legacy, fresh)
    print(f"\n全部通过：{PASSED} 项检查")


if __name__ == "__main__":
    main()
