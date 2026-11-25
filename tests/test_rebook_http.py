"""改期退款 HTTP 端到端测试。"""

import pytest


def open_case(client, ticket="0850-0001", **overrides):
    payload = {
        "original_ticket_no": ticket,
        "original_fare_cents": 100_000,
        "taxes_cents": 6_000,
        "tax_transfer_cents": 2_000,
        "refund_due_date": "2026-12-01",
        "rebook_no": "RB-100",
    }
    payload.update(overrides)
    return client.post("/api/rebook/cases", json=payload)


def test_open_case_returns_201_and_tax_refunded_view(client):
    response = open_case(client)
    assert response.status_code == 201
    view = response.get_json()
    assert view["status"] == "PENDING"
    assert view["refunded_cents"] == 4_000
    assert view["pending_cents"] == 100_000
    assert view["balanced"] is True


def test_reopen_same_ticket_is_idempotent_not_duplicate(client):
    first = open_case(client)
    second = open_case(client)
    assert first.status_code == 201
    assert second.status_code == 200
    assert second.get_json()["refunded_cents"] == 4_000


def test_get_ticket_view_by_original_ticket(client):
    open_case(client, ticket="0850-0002")
    response = client.get("/api/rebook/tickets/0850-0002")
    assert response.status_code == 200
    view = response.get_json()
    assert view["original_ticket_no"] == "0850-0002"
    assert {"refunded_cents", "pending_cents", "pending_refund_cents",
            "credited_cents", "tax_reserved_cents"} <= set(view)


def test_get_unknown_ticket_404(client):
    response = client.get("/api/rebook/tickets/nope")
    assert response.status_code == 404
    assert response.get_json()["code"] == "REBOOK_CASE_NOT_FOUND"


def test_success_receipt_completes_and_late_failure_does_not_roll_back(client):
    open_case(client)
    ok = client.post("/api/rebook/tickets/0850-0001/receipts",
                     json={"seq": 1, "kind": "SUCCESS", "rebook_no": "RB-100"})
    assert ok.status_code == 200
    assert ok.get_json()["case"]["status"] == "COMPLETED"
    assert ok.get_json()["case"]["credited_cents"] == 100_000

    late = client.post("/api/rebook/tickets/0850-0001/receipts",
                       json={"seq": 2, "kind": "FAILED", "rebook_no": "RB-100"})
    body = late.get_json()
    assert body["receipt"]["outcome"] == "IGNORED"
    assert body["case"]["status"] == "COMPLETED"      # 不退回待办
    assert body["case"]["pending_cents"] == 0


def test_failure_receipt_then_recheck_refund_lifecycle(client):
    open_case(client)
    client.post("/api/rebook/tickets/0850-0001/receipts",
                json={"seq": 1, "kind": "FAILED", "rebook_no": "RB-100"})
    view = client.get("/api/rebook/tickets/0850-0001").get_json()
    assert view["status"] == "FAILED"
    assert view["pending_refund_total_cents"] == 100_000

    # 签发重新核退的票价
    issued = client.post("/api/rebook/tickets/0850-0001/refunds/fare-recheck/issue")
    assert issued.status_code == 200
    assert issued.get_json()["issued_now"] is True
    view = client.get("/api/rebook/tickets/0850-0001").get_json()
    assert view["refunded_cents"] == 104_000
    assert view["balanced"] is True


def test_refund_issue_retry_does_not_add_money(client):
    open_case(client)
    url = "/api/rebook/tickets/0850-0001/refunds/tax-now/issue"
    first = client.post(url).get_json()
    second = client.post(url).get_json()
    assert first["issued_now"] is False
    assert second["issued_now"] is False
    assert second["case"]["refunded_cents"] == 4_000
    assert len(second["case"]["refund_items"]) == 1   # 重试不新增款项


def test_issue_unknown_refund_is_404(client):
    open_case(client)
    response = client.post("/api/rebook/tickets/0850-0001/refunds/nope/issue")
    assert response.status_code == 404
    assert response.get_json()["code"] == "REFUND_NOT_FOUND"


def test_receipts_persist_across_requests(client):
    open_case(client)
    client.post("/api/rebook/tickets/0850-0001/receipts",
                json={"seq": 1, "kind": "SUCCESS", "rebook_no": "RB-100"})
    # 新请求读取同一份持久化状态
    view = client.get("/api/rebook/tickets/0850-0001").get_json()
    assert view["status"] == "COMPLETED"
    assert view["receipts"][0]["outcome"] == "APPLIED"


def test_list_tickets(client):
    open_case(client, ticket="0850-A")
    open_case(client, ticket="0850-B")
    response = client.get("/api/rebook/tickets")
    body = response.get_json()
    assert body["count"] == 2
    assert {item["original_ticket_no"] for item in body["tickets"]} == {"0850-A", "0850-B"}


def test_bad_amount_returns_400(client):
    response = open_case(client, ticket="0850-X", tax_transfer_cents=999_999)
    assert response.status_code == 400
