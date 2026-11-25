"""改期退款领域规则测试。

金额单位：分。覆盖：
- 不转新票税费按时退、可抵扣款挂起待核
- 改期办妥：旧票价转抵扣不退、已退税费不随新票再退
- 改期失败：暂缓款重新核退
- 乱序回执不把终态退回待办
- 签发重试幂等、不新增实退
- 按原票视图的实退/待核与资金守恒
"""

import pytest

from app.errors import DomainIssue
from app.rebook import (
    COMPLETED,
    FAILED,
    IGNORE_ALREADY_TERMINAL,
    IGNORE_DUPLICATE_SEQ,
    IGNORE_REBOOK_MISMATCH,
    PENDING,
    RECEIPT_APPLIED,
    RECEIPT_FAILED,
    RECEIPT_IGNORED,
    RECEIPT_SUCCESS,
    REFUND_FARE_RECHECK,
    REFUND_ISSUED,
    REFUND_PENDING,
    REFUND_TAX_NOW,
    FARE_REFUND_KEY,
    TAX_REFUND_KEY,
    apply_receipt,
    assert_case_balanced,
    issue_refund,
    open_case,
    summary,
)


def command(**overrides):
    base = {
        "original_ticket_no": "0850-0001",
        "original_fare_cents": 100_000,
        "taxes_cents": 6_000,
        "tax_transfer_cents": 2_000,
        "refund_due_date": "2026-12-01",
        "rebook_no": "RB-100",
    }
    base.update(overrides)
    return base


# 规则 1：建案即把不转新票的税费按时退，可能抵扣的票价挂起待核
def test_open_case_refunds_non_transferred_tax_and_holds_fare():
    case = open_case(command())
    assert case["status"] == PENDING
    # 6000 税费中 2000 随新票转移，剩 4000 按时实退
    assert case["refunded_cents"] == 4_000
    assert case["pending_cents"] == 100_000          # 原票价等回执
    assert case["tax_reserved_cents"] == 2_000
    assert case["credited_cents"] == 0
    tax_item = case["refund_items"][0]
    assert tax_item == {"refund_key": TAX_REFUND_KEY, "kind": REFUND_TAX_NOW,
                        "amount_cents": 4_000, "status": REFUND_ISSUED}
    assert_case_balanced(case)


def test_open_case_with_zero_refundable_tax_creates_no_tax_item():
    case = open_case(command(tax_transfer_cents=6_000))
    assert case["refunded_cents"] == 0
    assert case["refund_items"] == []
    assert case["tax_reserved_cents"] == 6_000
    assert_case_balanced(case)


def test_transferred_tax_cannot_exceed_total_taxes():
    with pytest.raises(DomainIssue) as exc:
        open_case(command(tax_transfer_cents=9_000))
    assert exc.value.code == "BAD_REQUEST"


def test_negative_amount_rejected():
    with pytest.raises(DomainIssue):
        open_case(command(original_fare_cents=-1))


def test_missing_ticket_or_due_rejected():
    with pytest.raises(DomainIssue):
        open_case(command(original_ticket_no=""))
    with pytest.raises(DomainIssue):
        open_case(command(refund_due_date=""))


def test_leading_zero_ticket_id_preserved_as_text():
    case = open_case(command(original_ticket_no="0001234567"))
    assert case["original_ticket_no"] == "0001234567"


# 规则 2：改期办妥 —— 旧票价抵扣不退，已退税费不随新票再退
def test_success_receipt_moves_held_fare_to_credited():
    case = open_case(command())
    before_refunded = case["refunded_cents"]
    case, record = apply_receipt(case, {"seq": 1, "kind": RECEIPT_SUCCESS,
                                        "rebook_no": "RB-100", "received_on": "2026-11-26"})
    assert record["outcome"] == RECEIPT_APPLIED
    assert case["status"] == COMPLETED
    assert case["pending_cents"] == 0
    assert case["credited_cents"] == 100_000          # 旧票价抵入新票
    assert case["refunded_cents"] == before_refunded  # 已退税费不重复退
    # 不新增任何退款项
    assert [item["kind"] for item in case["refund_items"]] == [REFUND_TAX_NOW]
    assert_case_balanced(case)


# 规则 3：改期失败 —— 暂缓的款重新核退
def test_failed_receipt_reopens_held_fare_for_refund():
    case = open_case(command())
    case, record = apply_receipt(case, {"seq": 1, "kind": RECEIPT_FAILED,
                                        "rebook_no": "RB-100", "received_on": "2026-11-26"})
    assert record["outcome"] == RECEIPT_APPLIED
    assert case["status"] == FAILED
    assert case["pending_cents"] == 0
    assert case["credited_cents"] == 0
    fare_item = next(item for item in case["refund_items"] if item["kind"] == REFUND_FARE_RECHECK)
    assert fare_item["amount_cents"] == 100_000
    assert fare_item["status"] == REFUND_PENDING      # 先挂起，等签发
    # 暂缓票价进入待签发退款桶，待核桶清零
    assert case["pending_refund_cents"] == 100_000
    # 已退税费仍在已退桶，守恒成立
    assert case["refunded_cents"] == 4_000
    assert_case_balanced(case)


def test_recheck_refund_issued_after_failure_moves_money_to_refunded():
    case = open_case(command())
    apply_receipt(case, {"seq": 1, "kind": RECEIPT_FAILED})
    item, created = issue_refund(case, FARE_REFUND_KEY)
    assert created is True
    assert item["status"] == REFUND_ISSUED
    assert case["refunded_cents"] == 4_000 + 100_000
    assert case["pending_refund_cents"] == 0
    assert summary(case)["pending_refund_total_cents"] == 0
    assert_case_balanced(case)


# 规则 4：乱序/迟到回执不能把已办妥的改期退回待办
def test_late_receipt_after_completion_is_ignored_and_state_holds():
    case = open_case(command())
    apply_receipt(case, {"seq": 1, "kind": RECEIPT_SUCCESS})
    assert case["status"] == COMPLETED

    # 迟到的失败回执（更大的序号、乱序到达）不得回退
    case, record = apply_receipt(case, {"seq": 2, "kind": RECEIPT_FAILED,
                                        "rebook_no": "RB-100", "received_on": "2026-11-27"})
    assert record["outcome"] == RECEIPT_IGNORED
    assert record["ignore_reason"] == IGNORE_ALREADY_TERMINAL
    assert case["status"] == COMPLETED
    assert case["pending_cents"] == 0
    assert case["credited_cents"] == 100_000
    assert_case_balanced(case)


def test_late_receipt_after_failure_is_ignored():
    case = open_case(command())
    apply_receipt(case, {"seq": 1, "kind": RECEIPT_FAILED})
    case, record = apply_receipt(case, {"seq": 3, "kind": RECEIPT_SUCCESS})
    assert record["outcome"] == RECEIPT_IGNORED
    assert record["ignore_reason"] == IGNORE_ALREADY_TERMINAL
    assert case["status"] == FAILED
    # 失败后待核退款项仍然保留，未被成功回执抵扣
    assert case["credited_cents"] == 0
    assert any(item["kind"] == REFUND_FARE_RECHECK for item in case["refund_items"])


def test_duplicate_receipt_seq_is_ignored():
    case = open_case(command())
    apply_receipt(case, {"seq": 1, "kind": RECEIPT_SUCCESS})
    case, record = apply_receipt(case, {"seq": 1, "kind": RECEIPT_SUCCESS})
    assert record["outcome"] == RECEIPT_IGNORED
    assert record["ignore_reason"] == IGNORE_DUPLICATE_SEQ
    assert case["status"] == COMPLETED
    # 重复回执不重复抵扣
    assert case["credited_cents"] == 100_000


def test_receipt_with_unknown_rebook_no_is_ignored():
    case = open_case(command())
    case, record = apply_receipt(case, {"seq": 1, "kind": RECEIPT_SUCCESS,
                                        "rebook_no": "RB-OTHER"})
    assert record["outcome"] == RECEIPT_IGNORED
    assert record["ignore_reason"] == IGNORE_REBOOK_MISMATCH
    assert case["status"] == PENDING


def test_invalid_receipt_kind_or_seq_rejected():
    case = open_case(command())
    with pytest.raises(DomainIssue):
        apply_receipt(case, {"seq": 1, "kind": "WAIT"})
    with pytest.raises(DomainIssue):
        apply_receipt(case, {"seq": 0, "kind": RECEIPT_SUCCESS})


# 规则 5：签发重试不新增实退款项
def test_issue_refund_is_idempotent_on_retry():
    case = open_case(command())
    first, created1 = issue_refund(case, TAX_REFUND_KEY)
    # 建案时税费项已是 ISSUED，重试直接返回原记录
    assert created1 is False
    refunded_after_first = case["refunded_cents"]
    second, created2 = issue_refund(case, TAX_REFUND_KEY)
    assert created2 is False
    assert first == second
    assert case["refunded_cents"] == refunded_after_first
    # 退款项数量不随重试增加
    assert len(case["refund_items"]) == 1


def test_issue_unknown_refund_raises():
    case = open_case(command())
    with pytest.raises(DomainIssue) as exc:
        issue_refund(case, "nope")
    assert exc.value.code == "REFUND_NOT_FOUND"


# 规则 6：工作台按原票查实退与待核
def test_summary_reports_refunded_and_pending_per_ticket():
    case = open_case(command())
    view = summary(case)
    assert view["original_ticket_no"] == "0850-0001"
    assert view["refunded_cents"] == 4_000
    assert view["pending_cents"] == 100_000
    assert view["issued_refund_total_cents"] == 4_000
    assert view["balanced"] is True

    apply_receipt(case, {"seq": 1, "kind": RECEIPT_FAILED})
    view = summary(case)
    # 待核票价转为待签发退款：待核桶清零，待签发退款桶=100000
    assert view["pending_cents"] == 0
    assert view["pending_refund_cents"] == 100_000
    assert view["pending_refund_total_cents"] == 100_000
    assert view["refunded_cents"] == 4_000
    assert view["balanced"] is True


def test_conservation_holds_across_full_success_lifecycle():
    case = open_case(command())
    apply_receipt(case, {"seq": 1, "kind": RECEIPT_SUCCESS})
    apply_receipt(case, {"seq": 2, "kind": RECEIPT_FAILED})  # 乱序迟到
    view = summary(case)
    total = (view["refunded_cents"] + view["pending_cents"]
             + view["pending_refund_cents"] + view["credited_cents"]
             + view["tax_reserved_cents"])
    assert total == view["original_fare_cents"] + view["taxes_cents"] == 106_000
    assert view["balanced"] is True


def test_conservation_holds_across_full_failure_lifecycle():
    case = open_case(command())
    apply_receipt(case, {"seq": 1, "kind": RECEIPT_FAILED})
    issue_refund(case, FARE_REFUND_KEY)
    view = summary(case)
    total = (view["refunded_cents"] + view["pending_cents"]
             + view["pending_refund_cents"] + view["credited_cents"]
             + view["tax_reserved_cents"])
    assert total == 106_000
    # 失败并核退完毕后，除预留税费外全部实退
    assert view["refunded_cents"] == 104_000
    assert view["pending_refund_cents"] == 0
    assert view["tax_reserved_cents"] == 2_000
    assert view["balanced"] is True
