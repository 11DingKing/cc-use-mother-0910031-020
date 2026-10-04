"""接口逐项原因码。

每个写操作返回的 item 都带 ``accepted`` 布尔与原因；
被拒绝时 ``reason`` 为下列错误码之一，被接受时为 ``OK``。
错误码稳定可被前端直接引用。
"""
from __future__ import annotations

OK = "OK"

# 输入与存在性
E_VALIDATION = "VALIDATION_ERROR"          # 入参格式/取值非法
E_NOT_FOUND = "NOT_FOUND"                  # 对象不存在
E_DUPLICATE = "DUPLICATE"                  # 编号重复
E_UNKNOWN_ROLE = "UNKNOWN_ROLE"            # 角色不在契约内

# 状态机
E_INVALID_STATE = "INVALID_STATE"          # 当前状态不允许该操作
E_NOT_SUBMITTED = "NOT_SUBMITTED"          # 尚未进入待确认
E_ALREADY_RELEASED = "ALREADY_RELEASED"    # 同一短缺已有替代发布（互斥）
E_ALREADY_FULFILLED = "ALREADY_FULFILLED"  # 短缺已履行结案
E_ALREADY_SIGNED = "ALREADY_SIGNED"        # 该角色已签署

# 试算闸口（每条风险对应一个拒绝原因）
E_APPLICABILITY_GAP = "APPLICABILITY_GAP"        # 存在未覆盖/不可用车型
E_ENGINEERING_UNCONFIRMED = "ENGINEERING_UNCONFIRMED"
E_SUPPLY_UNCONFIRMED = "SUPPLY_UNCONFIRMED"
E_CUSTOMER_RESTRICTED = "CUSTOMER_RESTRICTED"    # 客户限制未放行
E_QUORUM_MISSING = "QUORUM_MISSING"              # 法定人数不足
E_QUORUM_RULE_UNKNOWN = "QUORUM_RULE_UNKNOWN"
E_SIGNATURE_STALE = "SIGNATURE_STALE"            # 证据/矩阵变更后签署失效
E_SUPPLY_INSUFFICIENT = "SUPPLY_INSUFFICIENT"    # 竞争分配后供给不足
E_TRIAL_STALE = "TRIAL_STALE"                    # 库存等依据已变化，需重新试算
E_EVIDENCE_VERSION = "EVIDENCE_VERSION_CONFLICT" # 证据版本冲突

# 生效
E_FREEZE_REQUIRED = "FREEZE_REQUIRED"      # 生效必须冻结依据
E_RISK_OPEN = "RISK_OPEN"                  # 履行中存在未处置风险

# 原因码 -> 中文说明
MESSAGES = {
    OK: "接受",
    E_VALIDATION: "入参校验失败",
    E_NOT_FOUND: "对象不存在",
    E_DUPLICATE: "编号重复",
    E_UNKNOWN_ROLE: "未知角色",
    E_INVALID_STATE: "当前状态不允许该操作",
    E_NOT_SUBMITTED: "方案尚未进入待确认",
    E_ALREADY_RELEASED: "同一短缺已有替代方案发布",
    E_ALREADY_FULFILLED: "短缺已履行结案",
    E_ALREADY_SIGNED: "该角色已完成签署",
    E_APPLICABILITY_GAP: "存在未覆盖或标记不可用的车型",
    E_ENGINEERING_UNCONFIRMED: "工程适配证据未确认",
    E_SUPPLY_UNCONFIRMED: "供应可用量证据未确认",
    E_CUSTOMER_RESTRICTED: "客户限制未放行",
    E_QUORUM_MISSING: "多方审批法定人数不足",
    E_QUORUM_RULE_UNKNOWN: "法定人数规则不存在",
    E_SIGNATURE_STALE: "依据已变更，签署失效需重签",
    E_SUPPLY_INSUFFICIENT: "多短缺竞争同一替代料后可用供给不足",
    E_TRIAL_STALE: "试算依据已变化，必须重新试算",
    E_EVIDENCE_VERSION: "证据版本冲突",
    E_FREEZE_REQUIRED: "正式生效必须先冻结依据",
    E_RISK_OPEN: "履行中存在未处置的风险",
}
