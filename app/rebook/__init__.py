"""航司改期退款领域模型（纯函数）。

资金守恒：原票价 + 全部税费，在任意状态下都恰好落在五个互斥桶中：

    refunded_cents         已实退（已签发的退款项之和）
    pending_cents          待核（暂缓、等改期回执的原票价）
    pending_refund_cents   待签发退款（改期失败后重新核退、尚未签发的票价）
    credited_cents         已抵扣（改期办妥后抵入新票的旧票价）
    tax_reserved_cents     税费预留（随新票转移、不退的税费）

状态机单调：PENDING -> COMPLETED（改期办妥）或 PENDING -> FAILED（改期失败）。
终态后到达的回执只记录、不回退状态。金额一律使用整数最小单位（分），避免浮点漂移。
"""

from datetime import date

from app.errors import DomainIssue

# 案件状态
PENDING = "PENDING"          # 改期回执未到，部分款挂起待核
COMPLETED = "COMPLETED"      # 改期办妥：待核款已抵扣，旧票价不再退
FAILED = "FAILED"            # 改期失败：暂缓款重新核退
TERMINAL_STATES = {COMPLETED, FAILED}

# 回执结论
RECEIPT_SUCCESS = "SUCCESS"
RECEIPT_FAILED = "FAILED"
RECEIPT_KINDS = {RECEIPT_SUCCESS, RECEIPT_FAILED}

# 退款项类型
REFUND_TAX_NOW = "TAX_NOW"          # 不转新票、按时退的税费
REFUND_FARE_RECHECK = "FARE_RECHECK"  # 改期失败后重新核退的暂缓票价

# 退款项状态
REFUND_PENDING = "PENDING"
REFUND_ISSUED = "ISSUED"

# 回执处理结果
RECEIPT_APPLIED = "APPLIED"        # 触发了状态流转
RECEIPT_IGNORED = "IGNORED"        # 未触发流转（乱序/重复/终态后到达）

# 回执被忽略的原因码
IGNORE_DUPLICATE_SEQ = "DUPLICATE_SEQ"          # 同一序号重复到达
IGNORE_ALREADY_TERMINAL = "ALREADY_TERMINAL"    # 案件已办妥/失败，回执迟到
IGNORE_REBOOK_MISMATCH = "REBOOK_MISMATCH"      # 回执改期单号与案件不符

# 退款项稳定键（同一案件同一用途只有一条，重试不新增）
TAX_REFUND_KEY = "tax-now"
FARE_REFUND_KEY = "fare-recheck"


def _require_non_negative_int(value, field):
    if isinstance(value, bool) or not isinstance(value, int):
        raise DomainIssue("BAD_REQUEST", detail=f"{field} 必须是整数分")
    if value < 0:
        raise DomainIssue("BAD_REQUEST", detail=f"{field} 不能为负")
    return value


def open_case(command):
    """根据改期受理指令建立挂账案件。

    入参 command 字段（金额均为整数分）：
      original_ticket_no      原票号（文本，前导零保留）
      original_fare_cents     原票价
      taxes_cents             全部税费
      tax_transfer_cents      随新票转移、不退的税费（<= taxes_cents）
      refund_due_date         原票退款到期日（ISO 日期字符串）
      rebook_no               改期单号（可空）
    """
    ticket = str(command.get("original_ticket_no") or "").strip()
    if not ticket:
        raise DomainIssue("BAD_REQUEST", detail="original_ticket_no 不能为空")

    fare = _require_non_negative_int(command.get("original_fare_cents"), "original_fare_cents")
    taxes = _require_non_negative_int(command.get("taxes_cents"), "taxes_cents")
    tax_transfer = _require_non_negative_int(
        command.get("tax_transfer_cents", 0), "tax_transfer_cents")
    if tax_transfer > taxes:
        raise DomainIssue("BAD_REQUEST", detail="tax_transfer_cents 不能超过 taxes_cents")

    due = str(command.get("refund_due_date") or "").strip()
    if not due:
        raise DomainIssue("BAD_REQUEST", detail="refund_due_date 不能为空")
    # 校验 ISO 日期
    try:
        date.fromisoformat(due)
    except ValueError:
        raise DomainIssue("BAD_REQUEST", detail="refund_due_date 必须是 ISO 日期")

    tax_refund = taxes - tax_transfer          # 已确认不转新票、按时退的税费
    rebook_no = str(command.get("rebook_no") or "").strip() or None

    case = {
        "original_ticket_no": ticket,
        "rebook_no": rebook_no,
        "original_fare_cents": fare,
        "taxes_cents": taxes,
        "tax_transfer_cents": tax_transfer,
        "refund_due_date": due,
        "status": PENDING,
        # 五个互斥资金桶
        "refunded_cents": tax_refund,
        "pending_cents": fare,
        "pending_refund_cents": 0,
        "credited_cents": 0,
        "tax_reserved_cents": tax_transfer,
        # 建案即把不转新票的税费按时退：生成并直接签发改期前唯一一笔退税
        "refund_items": [
            {"refund_key": TAX_REFUND_KEY, "kind": REFUND_TAX_NOW,
             "amount_cents": tax_refund, "status": REFUND_ISSUED}
        ] if tax_refund > 0 else [],
        "receipts": [],
    }
    assert_case_balanced(case)
    return case


def assert_case_balanced(case):
    """守恒校验：五个桶之和恒等于原票价 + 全部税费。"""
    buckets = (case["refunded_cents"] + case["pending_cents"]
               + case["pending_refund_cents"] + case["credited_cents"]
               + case["tax_reserved_cents"])
    origin = case["original_fare_cents"] + case["taxes_cents"]
    if buckets != origin:
        raise DomainIssue("REBOOK_UNBALANCED", detail=(
            f"资金不守恒：桶合计 {buckets} != 原始 {origin}"))
    # 已退桶必须等于已签发退款项金额之和
    issued = sum(item["amount_cents"] for item in case["refund_items"]
                 if item["status"] == REFUND_ISSUED)
    if issued != case["refunded_cents"]:
        raise DomainIssue("REBOOK_UNBALANCED", detail=(
            f"已退金额 {case['refunded_cents']} 与已签发退款项 {issued} 不一致"))
    return True


def apply_receipt(case, receipt):
    """处理一条改期回执（可能乱序到达）。

    返回 (更新后的 case, 处理结论)。结论为 APPLIED 时发生了状态流转；
    IGNORED 时仅登记回执，案件状态与资金桶不变，绝不把终态退回待办。
    """
    kind = receipt.get("kind")
    if kind not in RECEIPT_KINDS:
        raise DomainIssue("BAD_REQUEST", detail="回执 kind 必须是 SUCCESS 或 FAILED")

    seq = receipt.get("seq", 1)
    if isinstance(seq, bool) or not isinstance(seq, int) or seq <= 0:
        raise DomainIssue("BAD_REQUEST", detail="回执 seq 必须是正整数")

    rebook_no = str(receipt.get("rebook_no") or "").strip() or None
    received_on = str(receipt.get("received_on") or "").strip()

    # 同一序号重复到达：忽略
    if any(item["seq"] == seq for item in case["receipts"]):
        record = _ignored(case, seq, kind, rebook_no, received_on, IGNORE_DUPLICATE_SEQ)
        return case, record

    # 改期单号不一致：忽略，避免张冠李戴的回执改动案件
    if rebook_no and case.get("rebook_no") and rebook_no != case["rebook_no"]:
        record = _ignored(case, seq, kind, rebook_no, received_on, IGNORE_REBOOK_MISMATCH)
        return case, record

    # 案件已在终态：迟到/乱序回执只登记，不回退
    if case["status"] in TERMINAL_STATES:
        record = _ignored(case, seq, kind, rebook_no, received_on, IGNORE_ALREADY_TERMINAL)
        return case, record

    # PENDING 状态下，第一条有效决定性回执推动状态机单向前进
    held = case["pending_cents"]
    if kind == RECEIPT_SUCCESS:
        # 改期办妥：暂缓的旧票价抵入新票，不再退；已退税费也不随新票再退
        case["pending_cents"] = 0
        case["credited_cents"] += held
        case["status"] = COMPLETED
    else:
        # 改期失败：暂缓的款重新核退，生成一条待签发退款，款项进入待签发退款桶
        case["pending_cents"] = 0
        case["pending_refund_cents"] += held
        case["refund_items"].append({
            "refund_key": FARE_REFUND_KEY, "kind": REFUND_FARE_RECHECK,
            "amount_cents": held, "status": REFUND_PENDING})
        case["status"] = FAILED

    record = {"seq": seq, "kind": kind, "rebook_no": rebook_no,
              "received_on": received_on, "outcome": RECEIPT_APPLIED, "ignore_reason": None}
    case["receipts"].append(record)
    assert_case_balanced(case)
    return case, record


def _ignored(case, seq, kind, rebook_no, received_on, reason):
    record = {"seq": seq, "kind": kind, "rebook_no": rebook_no,
              "received_on": received_on, "outcome": RECEIPT_IGNORED, "ignore_reason": reason}
    case["receipts"].append(record)
    return record


def issue_refund(case, refund_key):
    """签发（或重试签发）一条退款。

    幂等：退款项按稳定 refund_key 唯一标识。已签发的重试直接返回原记录，
    不新增任何实退款项、不改变已退桶金额。
    """
    item = next((item for item in case["refund_items"] if item["refund_key"] == refund_key), None)
    if item is None:
        raise DomainIssue("REFUND_NOT_FOUND", 404)

    if item["status"] == REFUND_ISSUED:
        # 重试：原样返回，金额不变
        return dict(item), False

    # 新签发：金额从待签发退款桶转入已退桶
    item["status"] = REFUND_ISSUED
    case["pending_refund_cents"] -= item["amount_cents"]
    case["refunded_cents"] += item["amount_cents"]
    assert_case_balanced(case)
    return dict(item), True


def summary(case):
    """工作台按原票查询的视图：实退金额、待核金额及守恒指标。"""
    issued_items = [item for item in case["refund_items"] if item["status"] == REFUND_ISSUED]
    pending_items = [item for item in case["refund_items"] if item["status"] == REFUND_PENDING]
    return {
        "original_ticket_no": case["original_ticket_no"],
        "rebook_no": case["rebook_no"],
        "status": case["status"],
        "refund_due_date": case["refund_due_date"],
        "original_fare_cents": case["original_fare_cents"],
        "taxes_cents": case["taxes_cents"],
        "tax_transfer_cents": case["tax_transfer_cents"],
        # 工作台核心两个数
        "refunded_cents": case["refunded_cents"],       # 已实退
        "pending_cents": case["pending_cents"],         # 待核（等回执的暂缓票价）
        # 其余桶，便于完整核对
        "pending_refund_cents": case["pending_refund_cents"],
        "credited_cents": case["credited_cents"],
        "tax_reserved_cents": case["tax_reserved_cents"],
        "refund_items": [dict(item) for item in case["refund_items"]],
        "issued_refund_total_cents": sum(item["amount_cents"] for item in issued_items),
        "pending_refund_total_cents": sum(item["amount_cents"] for item in pending_items),
        "receipts": [dict(item) for item in case["receipts"]],
        "balanced": _is_balanced(case),
    }


def _is_balanced(case):
    buckets = (case["refunded_cents"] + case["pending_cents"]
               + case["pending_refund_cents"] + case["credited_cents"]
               + case["tax_reserved_cents"])
    origin = case["original_fare_cents"] + case["taxes_cents"]
    issued = sum(item["amount_cents"] for item in case["refund_items"]
                 if item["status"] == REFUND_ISSUED)
    return buckets == origin and issued == case["refunded_cents"]
