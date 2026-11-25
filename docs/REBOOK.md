# 改期退款（rebook）模块

航司改期回执未到、原票退款却临近到期时的挂账与核退工作台。不依赖外部服务，状态落在本地 SQLite。

## 资金模型

原票价与全部税费在任意时刻都恰好落在五个互斥桶，合计恒等于 `original_fare_cents + taxes_cents`：

| 桶 | 含义 |
|---|---|
| `refunded_cents` | 已实退，等于已签发退款项金额之和 |
| `pending_cents` | 待核：暂缓、等改期回执的原票价 |
| `pending_refund_cents` | 待签发退款：改期失败后重新核退、尚未签发的票价 |
| `credited_cents` | 已抵扣：改期办妥后抵入新票的旧票价 |
| `tax_reserved_cents` | 税费预留：随新票转移、不退的税费 |

每次落账都执行守恒校验，不守恒抛 `REBOOK_UNBALANCED`。金额一律整数分，不用浮点。

## 业务规则到机制

1. **不转新票的税费按时退**：建案即生成 `TAX_NOW` 退款项并签发，金额为 `taxes_cents - tax_transfer_cents`，进入已退桶；可能随改期抵扣的原票价进入待核桶。
2. **改期办妥（SUCCESS 回执）**：待核旧票价整笔转入已抵扣桶，不再退；已退税费不随新票再退，也不产生新退款项。
3. **改期失败（FAILED 回执）**：待核款转入待签发退款桶并生成 `FARE_RECHECK` 退款项，签发后进入已退桶，即“重新核退”。
4. **乱序/迟到回执不回退**：状态机单调，仅允许 `PENDING -> COMPLETED / FAILED`。终态后到达的回执登记为 `IGNORED`（含原因码），状态与资金桶不变。
   - `DUPLICATE_SEQ`：同一回执序号重复到达
   - `ALREADY_TERMINAL`：案件已终态，回执迟到
   - `REBOOK_MISMATCH`：回执改期单号与案件不符
5. **签发重试不新增实退**：退款项按稳定 `refund_key`（`tax-now` / `fare-recheck`）唯一标识，已签发的重试原样返回、`issued_now=false`，金额与条目数都不变。
6. **按原票查询**：`GET /api/rebook/tickets/<原票号>` 返回实退、待核、待签发退款、已抵扣、预留税费及守恒标志。原票号按文本保留前导零。

## HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/rebook/cases` | 建立挂账案件（重复原票号幂等返回，不覆盖） |
| POST | `/api/rebook/tickets/<no>/receipts` | 登记改期回执，容忍乱序 |
| POST | `/api/rebook/tickets/<no>/refunds/<key>/issue` | 签发/重试退款，幂等 |
| GET | `/api/rebook/tickets/<no>` | 按原票查实退与待核 |
| GET | `/api/rebook/tickets` | 列出全部挂账案件 |

错误码：`REBOOK_CASE_NOT_FOUND`、`REBOOK_DUPLICATE_TICKET`、`REBOOK_UNBALANCED`、`REFUND_NOT_FOUND`、`BAD_REQUEST`。

## 建案请求示例

```json
{
  "original_ticket_no": "0850-0001",
  "original_fare_cents": 100000,
  "taxes_cents": 6000,
  "tax_transfer_cents": 2000,
  "refund_due_date": "2026-12-01",
  "rebook_no": "RB-100"
}
```

测试见 `tests/test_rebook_domain.py`（领域规则）与 `tests/test_rebook_http.py`（端到端）。
