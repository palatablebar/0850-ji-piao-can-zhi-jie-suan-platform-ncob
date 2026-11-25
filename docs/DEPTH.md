# 机票残值结算台深度登记

- 规模：中型
- 核心规则：退款只退可退票价加税费，改期先把原票价抵作余额，改期费单独收取。
- 样例：`sample_data/dirty.csv` 触发异常，`sample_data/clean.csv` 可完成签发。

## 功能模块

| 模块 | 动作 | 入口 | 核心文件 | 测试 |
|---|---|---|---|---|
| M1 样例采信 | 定位坏行并做重复裁决 | `POST /api/stages/intake` | `app/intake/service.py` | `tests/test_intake.py` |
| M2 领域整理 | 解析字段与单位，隔离不满足前置条件的行 | `POST /api/stages/prepare` | `app/domain/prepare.py` | `tests/test_prepare.py` |
| M3 核心计算 | 执行项目核心规则并生成可比指标 | `POST /api/stages/calculate` | `app/domain/calculate.py` | `tests/test_calculate.py` |
| M4 规则判定 | 给出通过、复核或阻断结论 | `POST /api/stages/decision` | `app/domain/decision.py` | `tests/test_decision.py` |
| M5 运行对账 | 核对来源行、采信、隔离、重复与计算条目 | `POST /api/stages/reconcile` | `app/reconcile/ledger.py` | `tests/test_reconcile.py` |
| M6 批次复核 | 带版本号签发或暂缓，拒绝陈旧复核 | `POST /api/runs/{id}/review` | `app/review/service.py` | `tests/test_review.py` |
| M7 证据交付 | 仅在签发后输出可校验的复核证据 | `GET /api/runs/{id}/evidence` | `app/evidence/render.py` | `tests/test_evidence.py` |

## 输入陷阱

| 编号 | 处理位置 | 测试 |
|---|---|---|
| TRAP-02 | `app/intake/reader.py` | `tests/test_traps.py::test_bom_is_removed` |
| TRAP-03 | `app/intake/reader.py` | `tests/test_traps.py::test_quoted_newline_is_one_record` |
| TRAP-04 | `app/intake/service.py` | `tests/test_traps.py::test_repeated_header_is_ignored` |
| TRAP-05 | `app/intake/service.py` | `tests/test_traps.py::test_extra_column_is_isolated` |
| TRAP-06 | `app/intake/normalize.py` | `tests/test_traps.py::test_fullwidth_digits_are_canonical` |
| TRAP-07 | `app/intake/normalize.py` | `tests/test_traps.py::test_sentinel_is_blank` |
| TRAP-08 | `app/intake/service.py` | `tests/test_traps.py::test_leading_zero_is_preserved` |
| TRAP-14 | `app/intake/service.py` | `tests/test_traps.py::test_conflicting_duplicate_is_isolated` |
| TRAP-16 | `app/intake/reader.py` | `tests/test_traps.py::test_unclosed_quote_rejects_file` |
| TRAP-18 | `app/intake/reader.py` | `tests/test_traps.py::test_empty_and_header_only_differ` |
| TRAP-22 | `app/domain/decision.py` | `tests/test_decision.py` |
| TRAP-26 | `app/review/store.py` | `tests/test_review.py::test_stale_revision_is_rejected` |

## 深度边界

采信层区分编码、重复表头、列数、空值、全角字符、前导零与冲突重复；整理层负责单位和字段边界；计算层执行核心规则；判定层保留原因码；对账层要求来源行等于采信、隔离、重复和忽略之和；复核层使用版本号阻止陈旧签发。

## 改期退款模块（M8）

航司改期回执未到、原票退款临近到期时的挂账核退。状态机单调、资金五桶守恒、退款项幂等。

| 模块 | 动作 | 入口 | 核心文件 | 测试 |
|---|---|---|---|---|
| M8 改期挂账 | 建案按时退不转新票税费、票价挂起待核 | `POST /api/rebook/cases` | `app/rebook/__init__.py` | `tests/test_rebook_domain.py` |
| M8 回执处理 | 乱序回执不回退终态，成功抵扣/失败重核 | `POST /api/rebook/tickets/<no>/receipts` | `app/rebook/__init__.py` | `tests/test_rebook_domain.py` |
| M8 退款签发 | 稳定键幂等，重试不新增实退 | `POST /api/rebook/tickets/<no>/refunds/<key>/issue` | `app/rebook/__init__.py` | `tests/test_rebook_http.py` |
| M8 原票查询 | 按原票查实退与待核金额 | `GET /api/rebook/tickets/<no>` | `app/rebook/routes.py` | `tests/test_rebook_http.py` |

五桶：`refunded`（已实退）、`pending`（待核）、`pending_refund`（待签发退款）、`credited`（已抵扣）、`tax_reserved`（税费预留），合计恒等于原票价加全部税费。规则细节见 `docs/REBOOK.md`。
