"""改期退款编排服务：把领域纯函数与持久化串起来。"""

from app.rebook import (
    apply_receipt,
    issue_refund as domain_issue_refund,
    open_case,
    summary as domain_summary,
)


def open_rebook_case(store, command):
    ticket = str(command.get("original_ticket_no") or "").strip()
    if store.exists(ticket):
        # 重复建案不覆盖既有挂账，返回现有案件，保证可重试
        return domain_summary(store.get_state(ticket)), False
    case = open_case(command)
    store.insert_case(case)
    return domain_summary(case), True


def register_receipt(store, ticket_no, receipt):
    case = store.get_state(ticket_no)
    if case is None:
        from app.errors import DomainIssue
        raise DomainIssue("REBOOK_CASE_NOT_FOUND", 404)
    case, record = apply_receipt(case, receipt)
    store.save_state(ticket_no, case, "REGISTER_RECEIPT", record)
    return domain_summary(case), record


def issue_ticket_refund(store, ticket_no, refund_key):
    case = store.get_state(ticket_no)
    if case is None:
        from app.errors import DomainIssue
        raise DomainIssue("REBOOK_CASE_NOT_FOUND", 404)
    item, created = domain_issue_refund(case, refund_key)
    store.save_state(ticket_no, case, "ISSUE_REFUND",
                     {"refund_key": refund_key, "amount_cents": item["amount_cents"],
                      "created": created})
    return domain_summary(case), item, created


def get_ticket_view(store, ticket_no):
    case = store.get_state(ticket_no)
    if case is None:
        from app.errors import DomainIssue
        raise DomainIssue("REBOOK_CASE_NOT_FOUND", 404)
    return domain_summary(case)


def list_ticket_views(store):
    return [domain_summary(case) for case in store.list_states()]
