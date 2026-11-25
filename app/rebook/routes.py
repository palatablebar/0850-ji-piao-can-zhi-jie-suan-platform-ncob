"""改期退款 HTTP 接口。

- POST /api/rebook/cases                 建立挂账案件（建案即按时退不转新票的税费）
- POST /api/rebook/tickets/<no>/receipts 登记改期回执（容忍乱序，不回退终态）
- POST /api/rebook/tickets/<no>/refunds/<key>/issue  签发/重试退款（幂等，不新增实退）
- GET  /api/rebook/tickets/<no>          按原票查实退与待核金额
- GET  /api/rebook/tickets               列出全部挂账案件
"""

from flask import Blueprint, jsonify, request

from app.rebook import service

rebook_api = Blueprint("rebook_api", __name__)


def store():
    from flask import current_app
    return current_app.extensions["rebook"]


@rebook_api.post("/api/rebook/cases")
def open_case_route():
    data = request.get_json(silent=True) or {}
    view, created = service.open_rebook_case(store(), data)
    return jsonify(view), (201 if created else 200)


@rebook_api.post("/api/rebook/tickets/<ticket_no>/receipts")
def receipt_route(ticket_no):
    data = request.get_json(silent=True) or {}
    view, record = service.register_receipt(store(), ticket_no, data)
    return jsonify({"case": view, "receipt": record})


@rebook_api.post("/api/rebook/tickets/<ticket_no>/refunds/<refund_key>/issue")
def issue_route(ticket_no, refund_key):
    view, item, created = service.issue_ticket_refund(store(), ticket_no, refund_key)
    return jsonify({"case": view, "refund_item": item, "issued_now": created})


@rebook_api.get("/api/rebook/tickets/<ticket_no>")
def ticket_route(ticket_no):
    return jsonify(service.get_ticket_view(store(), ticket_no))


@rebook_api.get("/api/rebook/tickets")
def list_route():
    views = service.list_ticket_views(store())
    return jsonify({"count": len(views), "tickets": views})
