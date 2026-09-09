# 采购业务平台数据库详细设计

版本：数据库设计草案 v3（补齐履约、支付与预付、变更包及责任评价字段）；适用范围：`platform/` 的 PostgreSQL 业务库。本文是字段级草案，尚未生成或执行数据库迁移。

## 当前设计范围

本版与[统一设计](DESIGN.md)对应，已将以下能力展开到字段与约束：

- 申请预算分配、逐行预占、部分订单结转、跨版本需求范围、已批准申请修订/终止和对应账本。
- 多公司币种、精度及不可变汇率快照；采购链路中的实物/服务/许可/订阅和数量/金额计量。
- 合同与合同明细、可选授予来源的统一订单、订单预算分配、非实物验收及发票履约引用。
- 正常/例外采购方式审批、RFQ/RFP 核准路径、规则快照、独立见证开标字段。

字段采用同公司 FK、内容版本、来源分配与事务余额校验。表下的 CHECK 必须生成数据库约束；涉及多个记录的金额、范围、父子来源一致性必须采用明确的复合 FK、约束触发器或持锁事务，不能仅校验 ID 存在。

本版已补齐到货/检验/交接、退货核实、合同/订单变更包及签署历史、支付计划/申请/授权/执行/撤销、预付余额与核销、异常改派与多角色评价字段。完整贷项退款自动结算、跨币种结算仍属 P4；当前记录真实资金流入并保留未决阻断。本次仅完成字段设计，尚未实现 FastAPI、DDL、Alembic 迁移或真实数据库验证。

## 1. 阅读方式与物理约定

- **主表**保存一张单据的共同信息；**明细表**保存该单据的多条物料或分配记录；`xxx_line` 是明细表的命名习惯，不是列名或主表的内嵌 JSON。
- **关联表**表达用户与角色、供应商与品类等多对多关系；**流水表**追加记录一次数量或金额变化；**余额表**是同事务维护的汇总。
- 表名使用单数 `snake_case`，首期统一在业务库 `public` schema，按模块确定代码所有者。表名 `app_user` 对应概念设计中的用户实体。助手不获得此库账号。
- 字段列“可空”为“否”表示 NOT NULL；默认值“无”表示必须由用例提供（可空字段省略则为 NULL）。UUID 由应用生成；领域主表的 ID 沿用通用单据 ID。所有表的字段都已展开，未省略隐式公共列。
- 字符串状态使用 `varchar + CHECK`，允许值以字段说明为准；通用单据状态按类型验证。布尔值、金额和数量不得用字符串状态列代替。
- 金额 `numeric(24,6)`，单价/数量 `numeric(20,6)`，税率 `numeric(9,6)`；JSON API 用十进制字符串，服务使用 Decimal。PostgreSQL `numeric` 可用于精确数值计算。[数值类型说明](https://www.postgresql.org/docs/current/datatype-numeric.html)
- 时间用 `timestamptz`，日期用 `date`；新增记录 `created_at` 默认数据库当前时间，`updated_at` 由应用显式更新，不假定 PostgreSQL 自动更新时间。
- `PK` 表示主键，`UQ` 表示唯一约束，`FK` 表示外键。UUID `id` 默认主键。所有外键采用 ON DELETE RESTRICT、ON UPDATE RESTRICT；不用级联删除正式数据。
- 有 `legal_entity_id` 的表额外建立 `UQ(legal_entity_id, id)`。其引用其他法人范围表的字段使用复合 FK `(legal_entity_id, target_id)`，指向 `(legal_entity_id, id)`，使“同法人”由数据库保证。可空关联只允许 target_id 空，法人列始终非空。
- 唯一约束自动提供索引；本文普通索引是候选实现索引，重复前缀在实施时合并，不重复创建主键/唯一索引。外键字段索引按查询与删除检查需求列出。
- 跨行总量、树无环、领域状态迁移不能用普通单行 CHECK 代替，须按业务设计在事务中锁定来源再验证。FK/唯一约束和 CHECK 的能力边界见 [PostgreSQL 约束文档](https://www.postgresql.org/docs/current/ddl-constraints.html)。

### 1.1 主表与明细的具体例子

| 表                      | 记录              | 主要值                                                |
| ----------------------- | ----------------- | ----------------------------------------------------- |
| `business_document`     | 采购申请登记 1 条 | 单号 PR-000001、申请人、部门、当前 revision=1         |
| `purchase_request`      | 申请主表 1 条     | id 与上述登记相同，保存采购用途与批准版本               |
| `document_revision`     | 内容版本 1 条     | document_id 指申请，revision=1                        |
| `purchase_request_line` | 明细第 1 条       | purchase_request_id 指申请，line_no=1，显示器 10 台   |
| `purchase_request_line` | 明细第 2 条       | purchase_request_id 指同一申请，line_no=2，键盘 20 个 |

主表与明细为一对多，通用登记与领域主表为一对一。查询当前申请必须带当前 revision，不能把历史版本的明细一并统计。

### 1.2 内容版本与执行数据

新建单据在一个事务创建通用登记、领域主表和 WORKING 内容版本。草稿阶段可编辑当前版本，更新通用 lock_version；提交审批、发布或直接过账时冻结版本及所有业务内容。退回后的修订创建新的 revision、新的明细 ID，旧行与快照不可覆盖。

领域主表保留当前版本业务头字段，历史头字段取 document_revision.snapshot；下游必须引用精确历史明细 ID。版本快照冻结后的禁止改写，由数据库触发器和服务规则共同实施。分配状态、释放时间等执行元数据允许通过合法业务动作变更，不能借此修改冻结金额和数量。

通用登记 `(id, revision)` 与版本表 `(document_id, revision)` 的循环引用设为 DEFERRABLE INITIALLY DEFERRED，在事务结束检查。所有普通主外键及时检查；只有明确的创建循环和领域存在性检查延迟。撤回未修改重提也需创建新版本，避免同一冻结内容的审批生命周期混用。

### 1.3 状态与索引实现

字段说明中的允许状态须生成显式 CHECK；空字段不自动满足“必须存在”的业务要求，重要列使用 NOT NULL。每个表下面区分数据库约束和事务/服务规则。

审批活跃实例、有效发票分配等“只对部分记录唯一”使用部分唯一索引，状态字面量在 DDL 中须加 SQL 单引号。[部分索引说明](https://www.postgresql.org/docs/current/indexes-partial.html)

库存/预算原流水首期只做整笔反向冲销，一条原流水至多有一条冲销记录；实际退货可按数量分批创建正常退货行。已付款退货等业务限制保持业务设计原边界。


### 1.4 通用单据类型与领域主表

`document_type` 使用下表固定值，领域主表与通用登记同 ID。每种类型生成状态集合检查；状态迁移和批准时的业务动作仍由对应服务控制。普通初态为 DRAFT，异常工单初态为 OPEN。

| document_type               | 唯一领域主表                | 允许的主状态                                                                |
| --------------------------- | --------------------------- | --------------------------------------------------------------------------- |
| `SUPPLIER_ADMISSION`        | `supplier_admission`        | DRAFT, IN_REVIEW, APPROVED, RETURNED, REJECTED, CANCELLED                   |
| `SUPPLIER_STATUS_REQUEST`   | `supplier_status_request`   | DRAFT, IN_REVIEW, APPROVED, RETURNED, REJECTED, CANCELLED                   |
| `PURCHASE_REQUEST`          | `purchase_request`          | DRAFT, IN_REVIEW, APPROVED, RETURNED, REJECTED, CANCELLED                   |
| `RFQ`                       | `rfq`                       | DRAFT, IN_REVIEW, APPROVED, RETURNED, REJECTED, OPEN, CLOSED, UNSEALED, AWARDED, CANCELLED                                    |
| `QUOTATION`                 | `quotation`                 | SEALED, RECORDED, SUPERSEDED, CANCELLED                                     |
| `AWARD`                     | `award`                     | DRAFT, IN_REVIEW, APPROVED, RETURNED, REJECTED, CANCELLED                   |
| `PURCHASE_ORDER`            | `purchase_order`            | DRAFT, IN_REVIEW, APPROVED, RETURNED, REJECTED, CANCELLED, ISSUED, CLOSED   |
| `ORDER_CLOSE_REQUEST`       | `order_close_request`       | DRAFT, IN_REVIEW, APPROVED, RETURNED, REJECTED, CANCELLED                   |
| `RECEIPT`                   | `receipt`                   | DRAFT, POSTED, REVERSED, CANCELLED                                          |
| `RETURN_ORDER`              | `return_order`              | DRAFT, IN_REVIEW, APPROVED, RETURNED, REJECTED, CANCELLED, POSTED, REVERSED |
| `STOCK_ISSUE`               | `stock_issue`               | DRAFT, POSTED, REVERSED, CANCELLED                                          |
| `STOCK_ADJUSTMENT`          | `stock_adjustment`          | DRAFT, IN_REVIEW, APPROVED, RETURNED, REJECTED, CANCELLED, POSTED, REVERSED |
| `BUDGET_ADJUSTMENT`         | `budget_adjustment`         | DRAFT, IN_REVIEW, APPROVED, RETURNED, REJECTED, CANCELLED                   |
| `INVOICE`                   | `invoice`                   | DRAFT, IN_REVIEW, CONFIRMED, RETURNED, REJECTED, VOIDED                     |
| `PAYMENT_RECORD`            | `payment_record`            | IN_REVIEW, NEEDS_CORRECTION, UNRESOLVED, CONFIRMED, CORRECTED               |
| `REQUEST_CHANGE` | `request_change_request` | DRAFT, IN_REVIEW, APPROVED, RETURNED, REJECTED, CANCELLED |
| `PURCHASE_CONTRACT` | `purchase_contract` | DRAFT, IN_REVIEW, APPROVED, PENDING_EFFECTIVE, ACTIVE, CLOSED, RETURNED, REJECTED, TERMINATED |
| `ACCEPTANCE` | `acceptance` | DRAFT, IN_REVIEW, CONFIRMED, RETURNED, REJECTED, REVERSED |
| `PURCHASE_METHOD_DECISION` | `purchase_method_decision` | DRAFT, IN_REVIEW, APPROVED, RETURNED, REJECTED, CANCELLED |
| `CORRECTION_REQUEST`        | `correction_request`        | DRAFT, IN_REVIEW, APPROVED, RETURNED, REJECTED, CANCELLED                   |
| `CHANGE_PACKAGE` | `change_package` | DRAFT, IN_REVIEW, APPROVED, WAITING_CONFIRMATION, APPLIED, RETURNED, REJECTED, CANCELLED |
| `PAYMENT_PLAN` | `payment_plan` | DRAFT, IN_REVIEW, APPROVED, RETURNED, REJECTED, CLOSED |
| `PAYMENT_APPLICATION` | `payment_application` | DRAFT, IN_REVIEW, APPROVED, SETTLED, RETURNED, REJECTED, CANCELLED |
| `PAYMENT_AUTHORIZATION_REVOCATION` | `payment_authorization_revocation` | DRAFT, IN_REVIEW, APPROVED, RETURNED, REJECTED, CANCELLED |
| `PREPAYMENT_APPLICATION` | `prepayment_application` | DRAFT, IN_REVIEW, CONFIRMED, RETURNED, REJECTED, REVERSED |
| `SUPPLIER_EVALUATION` | `supplier_evaluation` | DRAFT, IN_REVIEW, PUBLISHED, RETURNED |
| `EXCEPTION_CASE`            | `exception_case`            | OPEN, IN_PROGRESS, RESOLVED, CANCELLED                                      |

报价 SEALED 表示截止前盲收登记且明细加密封存的状态；RECORDED 表示截止后集中开标解密并冻结的有效报价版本，SUPERSEDED 表示被新版本替代但历史定标引用仍保留。已过账库存单据全部来源流水完成反向冲销后才变为 REVERSED；正常退货不把原收货单变为 REVERSED。仅冲销部分完整来源流水时原主状态保持 POSTED，净数量按行和流水派生。

## 2. 单表目录

| 所属模块 | 物理表 | 中文名称 | 类型 |
| ---- | ---- | ---- | ---- |
| `foundation` | [legal_entity](#table-legal_entity) | 法人主体 | 主数据表 |
| `foundation` | [department](#table-department) | 部门 | 主数据表 |
| `foundation` | [app_user](#table-app_user) | 用户 | 身份表 |
| `foundation` | [role](#table-role) | 角色 | 权限表 |
| `foundation` | [user_role](#table-user_role) | 用户角色关系 | 关联表 |
| `foundation` | [role_permission](#table-role_permission) | 角色动作权限 | 关联表 |
| `foundation` | [user_scope](#table-user_scope) | 用户数据范围 | 权限表 |
| `foundation` | [auth_session](#table-auth_session) | 登录会话 | 身份表 |
| `foundation` | [service_client](#table-service_client) | 服务客户端 | 身份表 |
| `foundation` | [delegation](#table-delegation) | 助手委托 | 身份表 |
| `foundation` | [unit](#table-unit) | 计量单位 | 主数据表 |
| `foundation` | [category](#table-category) | 物料品类 | 主数据表 |
| `foundation` | [material](#table-material) | 物料 | 主数据表 |
| `foundation` | [warehouse](#table-warehouse) | 仓库 | 主数据表 |
| `foundation` | [business_document](#table-business_document) | 通用业务单据登记 | 单据登记表 |
| `foundation` | [document_revision](#table-document_revision) | 单据内容版本 | 版本表 |
| `foundation` | [attachment](#table-attachment) | 附件 | 文件元数据表 |
| `foundation` | [audit_log](#table-audit_log) | 业务审计 | 追加记录表 |
| `foundation` | [business_policy_version](#table-business_policy_version) | 业务规则配置版本 | 规则表 |
| `foundation` | [currency](#table-currency) | 币种 | 主数据表 |
| `foundation` | [exchange_rate_snapshot](#table-exchange_rate_snapshot) | 汇率快照 | 业务明细表 |
| `approvals` | [approval_rule_version](#table-approval_rule_version) | 审批规则版本 | 规则表 |
| `approvals` | [approval_instance](#table-approval_instance) | 审批实例 | 审批运行表 |
| `approvals` | [approval_step](#table-approval_step) | 审批节点 | 审批明细表 |
| `approvals` | [approval_decision](#table-approval_decision) | 审批决定与改派记录 | 追加记录表 |
| `integrations` | [idempotency_record](#table-idempotency_record) | 接口幂等结果 | 集成记录表 |
| `integrations` | [business_change](#table-business_change) | 业务变化 | 追加记录表 |
| `integrations` | [publication_counter](#table-publication_counter) | 变化发布计数器 | 单例协调表 |
| `integrations` | [change_publication](#table-change_publication) | 已发布变化 | 追加记录表 |
| `suppliers` | [supplier](#table-supplier) | 供应商档案 | 主数据表 |
| `suppliers` | [supplier_category](#table-supplier_category) | 供应商品类关系 | 关联表 |
| `suppliers` | [supplier_qualification](#table-supplier_qualification) | 供应商资质 | 业务明细表 |
| `suppliers` | [supplier_admission](#table-supplier_admission) | 供应商准入申请 | 单据主表 |
| `suppliers` | [supplier_status_request](#table-supplier_status_request) | 供应商状态变更申请 | 单据主表 |
| `suppliers` | [supplier_performance_snapshot](#table-supplier_performance_snapshot) | 供应商绩效快照 | 统计快照表 |
| `procurement` | [purchase_request](#table-purchase_request) | 采购申请主表 | 单据主表 |
| `procurement` | [purchase_request_line](#table-purchase_request_line) | 采购申请明细表 | 单据明细表 |
| `procurement` | [rfq](#table-rfq) | 询价主表 | 单据主表 |
| `procurement` | [rfq_line](#table-rfq_line) | 询价明细表 | 单据明细表 |
| `procurement` | [rfq_invitation](#table-rfq_invitation) | 询价邀请关系 | 关联表 |
| `procurement` | [quotation](#table-quotation) | 供应商报价主表 | 单据主表 |
| `procurement` | [quotation_line](#table-quotation_line) | 报价明细表 | 单据明细表 |
| `procurement` | [award](#table-award) | 定标主表 | 单据主表 |
| `procurement` | [award_line](#table-award_line) | 定标明细表 | 单据明细表 |
| `procurement` | [purchase_order](#table-purchase_order) | 采购订单主表 | 单据主表 |
| `procurement` | [purchase_order_line](#table-purchase_order_line) | 采购订单明细表 | 单据明细表 |
| `procurement` | [order_close_request](#table-order_close_request) | 订单余量关闭主表 | 单据主表 |
| `procurement` | [order_close_line](#table-order_close_line) | 订单余量关闭明细表 | 单据明细表 |
| `procurement` | [purchase_request_item](#table-purchase_request_item) | 跨版本需求项 | 业务明细表 |
| `procurement` | [request_change_request](#table-request_change_request) | 已批准申请修订或终止 | 单据主表 |
| `procurement` | [request_change_line](#table-request_change_line) | 申请修订影响范围 | 业务明细表 |
| `procurement` | [order_source_allocation](#table-order_source_allocation) | 订单申请范围分配 | 业务明细表 |
| `procurement` | [purchase_contract](#table-purchase_contract) | 采购合同 | 单据主表 |
| `procurement` | [purchase_contract_line](#table-purchase_contract_line) | 合同版本明细 | 业务明细表 |
| `procurement` | [acceptance](#table-acceptance) | 非实物验收单 | 单据主表 |
| `procurement` | [acceptance_line](#table-acceptance_line) | 非实物验收明细 | 业务明细表 |
| `procurement` | [purchase_method_decision](#table-purchase_method_decision) | 采购方式决定 | 单据主表 |
| `procurement` | [purchase_method_scope](#table-purchase_method_scope) | 方式决定范围 | 业务明细表 |
| `inventory` | [receipt](#table-receipt) | 采购收货主表 | 单据主表 |
| `inventory` | [receipt_line](#table-receipt_line) | 采购收货明细表 | 单据明细表 |
| `inventory` | [return_order](#table-return_order) | 采购退货主表 | 单据主表 |
| `inventory` | [return_line](#table-return_line) | 采购退货明细表 | 单据明细表 |
| `inventory` | [stock_issue](#table-stock_issue) | 库存领用主表 | 单据主表 |
| `inventory` | [stock_issue_line](#table-stock_issue_line) | 库存领用明细表 | 单据明细表 |
| `inventory` | [stock_adjustment](#table-stock_adjustment) | 库存调整主表 | 单据主表 |
| `inventory` | [stock_adjustment_line](#table-stock_adjustment_line) | 库存调整明细表 | 单据明细表 |
| `inventory` | [stock_balance](#table-stock_balance) | 库存余额 | 汇总表 |
| `inventory` | [stock_ledger](#table-stock_ledger) | 库存流水 | 追加账本表 |
| `finance` | [budget_account](#table-budget_account) | 预算账户 | 汇总表 |
| `finance` | [budget_adjustment](#table-budget_adjustment) | 预算调整申请 | 单据主表 |
| `finance` | [budget_reservation](#table-budget_reservation) | 订单行预算占用 | 余额明细表 |
| `finance` | [budget_ledger](#table-budget_ledger) | 预算流水 | 追加账本表 |
| `finance` | [invoice](#table-invoice) | 发票主表 | 单据主表 |
| `finance` | [invoice_line](#table-invoice_line) | 发票明细表 | 单据明细表 |
| `finance` | [invoice_match_run](#table-invoice_match_run) | 发票匹配批次 | 分析快照表 |
| `finance` | [invoice_allocation](#table-invoice_allocation) | 发票收货分配 | 分配明细表 |
| `finance` | [payment_record](#table-payment_record) | 付款登记主表 | 单据主表 |
| `finance` | [payment_allocation](#table-payment_allocation) | 实付执行分配 | 业务记录表 |
| `finance` | [request_budget_allocation](#table-request_budget_allocation) | 申请行预算分配 | 业务明细表 |
| `finance` | [budget_preencumbrance](#table-budget_preencumbrance) | 申请预算预占余额 | 业务明细表 |
| `finance` | [order_budget_allocation](#table-order_budget_allocation) | 订单来源预算分配 | 业务明细表 |
| `workflows` | [correction_request](#table-correction_request) | 跨模块纠错申请 | 单据主表 |
| `workflows` | [exception_case](#table-exception_case) | 异常工单 | 单据主表 |
| `inventory` | [return_review](#table-return_review) | 退货分工核实记录 | 业务记录表 |
| `inventory` | [arrival](#table-arrival) | 到货事实 | 业务记录表 |
| `inventory` | [arrival_line](#table-arrival_line) | 到货明细及保管余额 | 业务记录表 |
| `inventory` | [arrival_inspection](#table-arrival_inspection) | 分批检验及复检事实 | 业务记录表 |
| `inventory` | [arrival_handoff](#table-arrival_handoff) | 拒收与未入库退回交接 | 业务记录表 |
| `procurement` | [contract_signature_event](#table-contract_signature_event) | 合同签署及确认历史 | 业务记录表 |
| `procurement` | [change_package](#table-change_package) | 合同与订单联动变更包 | 单据主表 |
| `procurement` | [change_package_item](#table-change_package_item) | 变更包版本清单 | 业务记录表 |
| `finance` | [payment_plan](#table-payment_plan) | 订单支付计划 | 单据主表 |
| `finance` | [payment_plan_line](#table-payment_plan_line) | 支付计划阶段 | 业务记录表 |
| `finance` | [payment_application](#table-payment_application) | 付款申请 | 单据主表 |
| `finance` | [payment_application_line](#table-payment_application_line) | 付款申请来源占用 | 业务记录表 |
| `finance` | [payment_authorization](#table-payment_authorization) | 逐申请行支付授权 | 业务记录表 |
| `finance` | [payment_execution](#table-payment_execution) | 支付执行任务 | 业务记录表 |
| `finance` | [payment_execution_review](#table-payment_execution_review) | 支付失败与重复核实 | 业务记录表 |
| `finance` | [payment_authorization_revocation](#table-payment_authorization_revocation) | 未执行授权撤销 | 单据主表 |
| `finance` | [prepayment_balance](#table-prepayment_balance) | 逐笔预付余额 | 业务记录表 |
| `finance` | [prepayment_application](#table-prepayment_application) | 预付核销单 | 单据主表 |
| `finance` | [prepayment_application_line](#table-prepayment_application_line) | 核销双边分配 | 业务记录表 |
| `finance` | [payment_unresolved_case](#table-payment_unresolved_case) | 未决资金与退款阻断 | 业务记录表 |
| `workflows` | [exception_assignment](#table-exception_assignment) | 异常责任改派历史 | 业务记录表 |
| `suppliers` | [supplier_evaluation](#table-supplier_evaluation) | 供应商评价批次 | 单据主表 |
| `suppliers` | [supplier_evaluation_dimension](#table-supplier_evaluation_dimension) | 角色评价明细 | 业务记录表 |
| `inventory` | [arrival_custody_movement](#table-arrival_custody_movement) | 到货保管数量流水 | 业务记录表 |
| `procurement` | [change_package_line](#table-change_package_line) | 订单变更明细承接 | 业务记录表 |

## 3. 公共基础模块

<a id="table-legal_entity"></a>

### `legal_entity`：法人主体

所属模块：`foundation`；类型：主数据表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | --------------- | ---- | ------ | ---------- |
| `id` | `uuid` | 否 | `无` | 主键；应用生成 UUID；PK |
| `code` | `varchar(32)` | 否 | `无` | 法人编码 |
| `name` | `varchar(200)` | 否 | `无` | 法人名称 |
| `status` | `varchar(32)` | 否 | `'ACTIVE'` | 允许值：ACTIVE, INACTIVE |
| `created_at` | `timestamptz` | 否 | `CURRENT_TIMESTAMP` | 创建时间 |
| `base_currency` | `char(3)` | 否 | `无` | 本位币；FK → `currency.code` |
| `timezone` | `varchar(64)` | 否 | `Asia/Shanghai` | 业务日期时区 |

数据库约束与索引：

- PK：`id`。
- UQ：`(code)`。
- FK：`(base_currency) → currency(code)`。

<a id="table-department"></a>

### `department`：部门

所属模块：`foundation`；类型：主数据表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | --------------- | ---- | ------ | ---------- |
| `id` | `uuid` | 否 | `无` | 主键；应用生成 UUID；PK |
| `legal_entity_id` | `uuid` | 否 | `无` | 所属法人；FK → `legal_entity.id` |
| `parent_id` | `uuid` | 是 | `无` | 上级部门；FK → `department.id` |
| `code` | `varchar(32)` | 否 | `无` | 部门编码 |
| `name` | `varchar(100)` | 否 | `无` | 部门名称 |
| `status` | `varchar(32)` | 否 | `'ACTIVE'` | 允许值：ACTIVE, INACTIVE |
| `created_at` | `timestamptz` | 否 | `CURRENT_TIMESTAMP` | 创建时间 |

数据库约束与索引：

- PK：`id`。
- UQ：`(legal_entity_id, code)`。
- UQ：`(legal_entity_id, id)`。
- FK：`(legal_entity_id) → legal_entity(id)`。
- FK：`(legal_entity_id, parent_id) → department(legal_entity_id, id)`。
- CHECK：`parent_id IS NULL OR parent_id <> id`。
- 普通索引：`(legal_entity_id, parent_id)`。

补充约束与执行规则：

- 同法人树；禁止成环需服务递归校验，单行 CHECK 只能排除自指。

<a id="table-app_user"></a>

### `app_user`：用户

所属模块：`foundation`；类型：身份表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | --------------- | ---- | ------ | ---------- |
| `id` | `uuid` | 否 | `无` | 主键；应用生成 UUID；PK |
| `legal_entity_id` | `uuid` | 否 | `无` | 所属法人；FK → `legal_entity.id` |
| `department_id` | `uuid` | 否 | `无` | 默认部门；FK → `department.id` |
| `login_name` | `varchar(100)` | 否 | `无` | 规范化小写登录名 |
| `display_name` | `varchar(100)` | 否 | `无` | 显示名称 |
| `password_hash` | `text` | 否 | `无` | 密码散列 |
| `status` | `varchar(32)` | 否 | `'ACTIVE'` | 允许值：ACTIVE, DISABLED |
| `auth_version` | `integer` | 否 | `1` | 权限/会话撤销版本 |
| `updated_at` | `timestamptz` | 否 | `无` | 最后修改时间 |
| `created_at` | `timestamptz` | 否 | `CURRENT_TIMESTAMP` | 创建时间 |

数据库约束与索引：

- PK：`id`。
- UQ：`(legal_entity_id, login_name)`。
- UQ：`(legal_entity_id, id)`。
- FK：`(legal_entity_id) → legal_entity(id)`。
- FK：`(legal_entity_id, department_id) → department(legal_entity_id, id)`。
- CHECK：`auth_version > 0`。
- 普通索引：`(legal_entity_id, department_id)`。

补充约束与执行规则：

- 概念文档中的 user 在物理层命名为 app_user，避免与 SQL 内置名称混淆；不保存明文密码。

<a id="table-role"></a>

### `role`：角色

所属模块：`foundation`；类型：权限表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | --------------- | ---- | ------ | ---------- |
| `id` | `uuid` | 否 | `无` | 主键；应用生成 UUID；PK |
| `legal_entity_id` | `uuid` | 否 | `无` | 所属法人；FK → `legal_entity.id` |
| `code` | `varchar(32)` | 否 | `无` | 角色编码 |
| `name` | `varchar(100)` | 否 | `无` | 角色名称 |
| `status` | `varchar(32)` | 否 | `'ACTIVE'` | 允许值：ACTIVE, INACTIVE |
| `created_at` | `timestamptz` | 否 | `CURRENT_TIMESTAMP` | 创建时间 |

数据库约束与索引：

- PK：`id`。
- UQ：`(legal_entity_id, code)`。
- UQ：`(legal_entity_id, id)`。
- FK：`(legal_entity_id) → legal_entity(id)`。

<a id="table-user_role"></a>

### `user_role`：用户角色关系

所属模块：`foundation`；类型：关联表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | --------------- | ---- | ------ | ---------- |
| `id` | `uuid` | 否 | `无` | 主键；应用生成 UUID；PK |
| `legal_entity_id` | `uuid` | 否 | `无` | 所属法人；FK → `legal_entity.id` |
| `user_id` | `uuid` | 否 | `无` | 用户；FK → `app_user.id` |
| `role_id` | `uuid` | 否 | `无` | 角色；FK → `role.id` |
| `created_at` | `timestamptz` | 否 | `CURRENT_TIMESTAMP` | 创建时间 |

数据库约束与索引：

- PK：`id`。
- UQ：`(user_id, role_id)`。
- UQ：`(legal_entity_id, id)`。
- FK：`(legal_entity_id) → legal_entity(id)`。
- FK：`(legal_entity_id, user_id) → app_user(legal_entity_id, id)`。
- FK：`(legal_entity_id, role_id) → role(legal_entity_id, id)`。
- 普通索引：`(legal_entity_id, user_id)`。
- 普通索引：`(legal_entity_id, role_id)`。

<a id="table-role_permission"></a>

### `role_permission`：角色动作权限

所属模块：`foundation`；类型：关联表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | --------------- | ---- | ------ | ---------- |
| `id` | `uuid` | 否 | `无` | 主键；应用生成 UUID；PK |
| `legal_entity_id` | `uuid` | 否 | `无` | 所属法人；FK → `legal_entity.id` |
| `role_id` | `uuid` | 否 | `无` | 角色；FK → `role.id` |
| `permission_code` | `varchar(120)` | 否 | `无` | 动作权限编码 |
| `created_at` | `timestamptz` | 否 | `CURRENT_TIMESTAMP` | 创建时间 |

数据库约束与索引：

- PK：`id`。
- UQ：`(role_id, permission_code)`。
- UQ：`(legal_entity_id, id)`。
- FK：`(legal_entity_id) → legal_entity(id)`。
- FK：`(legal_entity_id, role_id) → role(legal_entity_id, id)`。
- 普通索引：`(legal_entity_id, role_id)`。

补充约束与执行规则：

- 权限编码来自代码维护的动作目录，服务校验有效值。

<a id="table-user_scope"></a>

### `user_scope`：用户数据范围

所属模块：`foundation`；类型：权限表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | --------------- | ---- | ------ | ---------- |
| `id` | `uuid` | 否 | `无` | 主键；应用生成 UUID；PK |
| `legal_entity_id` | `uuid` | 否 | `无` | 所属法人；FK → `legal_entity.id` |
| `user_id` | `uuid` | 否 | `无` | 用户；FK → `app_user.id` |
| `scope_type` | `varchar(32)` | 否 | `'SELF'` | 允许值：ENTITY, DEPARTMENT, WAREHOUSE, SELF |
| `department_id` | `uuid` | 是 | `无` | 授权部门；FK → `department.id` |
| `warehouse_id` | `uuid` | 是 | `无` | 授权仓库；FK → `warehouse.id` |
| `include_children` | `boolean` | 否 | `false` | 是否包含子部门 |
| `created_at` | `timestamptz` | 否 | `CURRENT_TIMESTAMP` | 创建时间 |

数据库约束与索引：

- PK：`id`。
- UQ：`(user_id, scope_type, department_id, warehouse_id)`。
- UQ：`(legal_entity_id, id)`。
- FK：`(legal_entity_id) → legal_entity(id)`。
- FK：`(legal_entity_id, user_id) → app_user(legal_entity_id, id)`。
- FK：`(legal_entity_id, department_id) → department(legal_entity_id, id)`。
- FK：`(legal_entity_id, warehouse_id) → warehouse(legal_entity_id, id)`。
- CHECK：`(scope_type = 'DEPARTMENT' AND department_id IS NOT NULL AND warehouse_id IS NULL) OR (scope_type = 'WAREHOUSE' AND warehouse_id IS NOT NULL AND department_id IS NULL) OR (scope_type IN ('ENTITY','SELF') AND department_id IS NULL AND warehouse_id IS NULL)`。
- CHECK：`NOT include_children OR scope_type = 'DEPARTMENT'`。
- 普通索引：`(legal_entity_id, user_id)`。
- 普通索引：`(legal_entity_id, department_id)`。
- 普通索引：`(legal_entity_id, warehouse_id)`。

补充约束与执行规则：

- 本表的复合唯一约束采用 NULLS NOT DISTINCT，避免空范围重复授权。

<a id="table-auth_session"></a>

### `auth_session`：登录会话

所属模块：`foundation`；类型：身份表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | --------------- | ---- | ------ | ---------- |
| `id` | `uuid` | 否 | `无` | 主键；应用生成 UUID；PK |
| `legal_entity_id` | `uuid` | 否 | `无` | 所属法人；FK → `legal_entity.id` |
| `user_id` | `uuid` | 否 | `无` | 用户；FK → `app_user.id` |
| `token_hash` | `varchar(128)` | 否 | `无` | 会话令牌摘要 |
| `csrf_hash` | `varchar(128)` | 否 | `无` | CSRF 秘密摘要 |
| `auth_version` | `integer` | 否 | `无` | 签发时用户授权版本 |
| `expires_at` | `timestamptz` | 否 | `无` | 失效时间 |
| `revoked_at` | `timestamptz` | 是 | `无` | 撤销时间 |
| `last_seen_at` | `timestamptz` | 是 | `无` | 最近访问时间 |
| `created_at` | `timestamptz` | 否 | `CURRENT_TIMESTAMP` | 创建时间 |

数据库约束与索引：

- PK：`id`。
- UQ：`(token_hash)`。
- UQ：`(legal_entity_id, id)`。
- FK：`(legal_entity_id) → legal_entity(id)`。
- FK：`(legal_entity_id, user_id) → app_user(legal_entity_id, id)`。
- CHECK：`expires_at > created_at`。
- 普通索引：`(expires_at)`。
- 普通索引：`(legal_entity_id, user_id)`。

<a id="table-service_client"></a>

### `service_client`：服务客户端

所属模块：`foundation`；类型：身份表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | --------------- | ---- | ------ | ---------- |
| `id` | `uuid` | 否 | `无` | 主键；应用生成 UUID；PK |
| `legal_entity_id` | `uuid` | 否 | `无` | 所属法人；FK → `legal_entity.id` |
| `client_code` | `varchar(32)` | 否 | `无` | 服务编码 |
| `credential_hash` | `text` | 否 | `无` | 服务凭证摘要 |
| `allowed_actions` | `jsonb` | 否 | `'[]'::jsonb` | 可委托动作数组 |
| `status` | `varchar(32)` | 否 | `'ACTIVE'` | 允许值：ACTIVE, DISABLED |
| `created_at` | `timestamptz` | 否 | `CURRENT_TIMESTAMP` | 创建时间 |

数据库约束与索引：

- PK：`id`。
- UQ：`(legal_entity_id, client_code)`。
- UQ：`(legal_entity_id, id)`。
- FK：`(legal_entity_id) → legal_entity(id)`。

补充约束与执行规则：

- 服务端动作白名单；凭证轮换时保留有限验证窗口并撤销旧委托。

<a id="table-delegation"></a>

### `delegation`：助手委托

所属模块：`foundation`；类型：身份表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | --------------- | ---- | ------ | ---------- |
| `id` | `uuid` | 否 | `无` | 主键；应用生成 UUID；PK |
| `legal_entity_id` | `uuid` | 否 | `无` | 所属法人；FK → `legal_entity.id` |
| `user_id` | `uuid` | 否 | `无` | 授权用户；FK → `app_user.id` |
| `service_client_id` | `uuid` | 否 | `无` | 获授权客户端；FK → `service_client.id` |
| `session_id` | `uuid` | 否 | `无` | 授权来源会话；FK → `auth_session.id` |
| `target_document_id` | `uuid` | 是 | `无` | 限定单据；FK → `business_document.id` |
| `token_hash` | `varchar(128)` | 否 | `无` | 不透明委托令牌摘要 |
| `actions` | `jsonb` | 否 | `'[]'::jsonb` | 委托动作数组 |
| `scope_snapshot` | `jsonb` | 否 | `'{}'::jsonb` | 组织/仓库范围快照 |
| `expires_at` | `timestamptz` | 否 | `无` | 到期时间 |
| `revoked_at` | `timestamptz` | 是 | `无` | 撤销时间 |
| `created_at` | `timestamptz` | 否 | `CURRENT_TIMESTAMP` | 创建时间 |

数据库约束与索引：

- PK：`id`。
- UQ：`(token_hash)`。
- UQ：`(legal_entity_id, id)`。
- FK：`(legal_entity_id) → legal_entity(id)`。
- FK：`(legal_entity_id, user_id) → app_user(legal_entity_id, id)`。
- FK：`(legal_entity_id, service_client_id) → service_client(legal_entity_id, id)`。
- FK：`(legal_entity_id, session_id) → auth_session(legal_entity_id, id)`。
- FK：`(legal_entity_id, target_document_id) → business_document(legal_entity_id, id)`。
- CHECK：`expires_at > created_at`。
- 普通索引：`(legal_entity_id, user_id)`。
- 普通索引：`(legal_entity_id, service_client_id)`。
- 普通索引：`(legal_entity_id, session_id)`。
- 普通索引：`(legal_entity_id, target_document_id)`。

补充约束与执行规则：

- 每次请求仍查询最新用户权限、来源会话与撤销状态，快照不构成永久授权。

<a id="table-unit"></a>

### `unit`：计量单位

所属模块：`foundation`；类型：主数据表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | --------------- | ---- | ------ | ---------- |
| `id` | `uuid` | 否 | `无` | 主键；应用生成 UUID；PK |
| `legal_entity_id` | `uuid` | 否 | `无` | 所属法人；FK → `legal_entity.id` |
| `code` | `varchar(32)` | 否 | `无` | 编码 |
| `name` | `varchar(100)` | 否 | `无` | 名称 |
| `status` | `varchar(32)` | 否 | `'ACTIVE'` | 允许值：ACTIVE, INACTIVE |
| `created_at` | `timestamptz` | 否 | `CURRENT_TIMESTAMP` | 创建时间 |

数据库约束与索引：

- PK：`id`。
- UQ：`(legal_entity_id, code)`。
- UQ：`(legal_entity_id, id)`。
- FK：`(legal_entity_id) → legal_entity(id)`。

补充约束与执行规则：

- 被历史单据引用后停用，不物理删除。

<a id="table-category"></a>

### `category`：物料品类

所属模块：`foundation`；类型：主数据表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | --------------- | ---- | ------ | ---------- |
| `id` | `uuid` | 否 | `无` | 主键；应用生成 UUID；PK |
| `legal_entity_id` | `uuid` | 否 | `无` | 所属法人；FK → `legal_entity.id` |
| `code` | `varchar(32)` | 否 | `无` | 编码 |
| `name` | `varchar(100)` | 否 | `无` | 名称 |
| `parent_id` | `uuid` | 是 | `无` | 上级品类；FK → `category.id` |
| `status` | `varchar(32)` | 否 | `'ACTIVE'` | 允许值：ACTIVE, INACTIVE |
| `created_at` | `timestamptz` | 否 | `CURRENT_TIMESTAMP` | 创建时间 |

数据库约束与索引：

- PK：`id`。
- UQ：`(legal_entity_id, code)`。
- UQ：`(legal_entity_id, id)`。
- FK：`(legal_entity_id) → legal_entity(id)`。
- FK：`(legal_entity_id, parent_id) → category(legal_entity_id, id)`。
- 普通索引：`(legal_entity_id, parent_id)`。

补充约束与执行规则：

- 被历史单据引用后停用，不物理删除。

<a id="table-material"></a>

### `material`：物料

所属模块：`foundation`；类型：主数据表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | --------------- | ---- | ------ | ---------- |
| `id` | `uuid` | 否 | `无` | 主键；应用生成 UUID；PK |
| `legal_entity_id` | `uuid` | 否 | `无` | 所属法人；FK → `legal_entity.id` |
| `code` | `varchar(64)` | 否 | `无` | 物料编码 |
| `name` | `varchar(200)` | 否 | `无` | 名称 |
| `specification` | `text` | 否 | `无` | 规格 |
| `category_id` | `uuid` | 否 | `无` | 所属品类；FK → `category.id` |
| `unit_id` | `uuid` | 否 | `无` | 唯一基础计量单位；FK → `unit.id` |
| `status` | `varchar(32)` | 否 | `'ACTIVE'` | 允许值：ACTIVE, INACTIVE |
| `created_at` | `timestamptz` | 否 | `CURRENT_TIMESTAMP` | 创建时间 |

数据库约束与索引：

- PK：`id`。
- UQ：`(legal_entity_id, code)`。
- UQ：`(legal_entity_id, id)`。
- FK：`(legal_entity_id) → legal_entity(id)`。
- FK：`(legal_entity_id, category_id) → category(legal_entity_id, id)`。
- FK：`(legal_entity_id, unit_id) → unit(legal_entity_id, id)`。
- 普通索引：`(legal_entity_id, category_id)`。
- 普通索引：`(legal_entity_id, unit_id)`。

补充约束与执行规则：

- 已有业务引用后，基础单位不可修改；首期不自动做单位换算。

<a id="table-warehouse"></a>

### `warehouse`：仓库

所属模块：`foundation`；类型：主数据表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | --------------- | ---- | ------ | ---------- |
| `id` | `uuid` | 否 | `无` | 主键；应用生成 UUID；PK |
| `legal_entity_id` | `uuid` | 否 | `无` | 所属法人；FK → `legal_entity.id` |
| `code` | `varchar(32)` | 否 | `无` | 仓库编码 |
| `name` | `varchar(100)` | 否 | `无` | 仓库名称 |
| `department_id` | `uuid` | 否 | `无` | 管理部门；FK → `department.id` |
| `address` | `text` | 否 | `无` | 地址 |
| `status` | `varchar(32)` | 否 | `'ACTIVE'` | 允许值：ACTIVE, INACTIVE |
| `created_at` | `timestamptz` | 否 | `CURRENT_TIMESTAMP` | 创建时间 |

数据库约束与索引：

- PK：`id`。
- UQ：`(legal_entity_id, code)`。
- UQ：`(legal_entity_id, id)`。
- FK：`(legal_entity_id) → legal_entity(id)`。
- FK：`(legal_entity_id, department_id) → department(legal_entity_id, id)`。
- 普通索引：`(legal_entity_id, department_id)`。

<a id="table-business_document"></a>

### `business_document`：通用业务单据登记

所属模块：`foundation`；类型：单据登记表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | --------------- | ---- | ------ | ---------- |
| `id` | `uuid` | 否 | `无` | 主键；应用生成 UUID；PK |
| `legal_entity_id` | `uuid` | 否 | `无` | 所属法人；FK → `legal_entity.id` |
| `document_type` | `varchar(40)` | 否 | `无` | 领域类型，见领域主表映射 |
| `number` | `varchar(64)` | 否 | `无` | 按类型序列生成的业务编号 |
| `department_id` | `uuid` | 否 | `无` | 归属部门；FK → `department.id` |
| `owner_id` | `uuid` | 否 | `无` | 业务负责人；FK → `app_user.id` |
| `created_by` | `uuid` | 否 | `无` | 实际创建人/委托用户；FK → `app_user.id` |
| `revision` | `integer` | 否 | `1` | 当前内容版本 |
| `lock_version` | `integer` | 否 | `1` | 并发控制版本 |
| `status` | `varchar(32)` | 否 | `'DRAFT'` | 领域业务状态 |
| `updated_at` | `timestamptz` | 否 | `无` | 更新时由应用写入 |
| `created_at` | `timestamptz` | 否 | `CURRENT_TIMESTAMP` | 创建时间 |

数据库约束与索引：

- PK：`id`。
- UQ：`(legal_entity_id, document_type, number)`。
- UQ：`(legal_entity_id, id)`。
- FK：`(legal_entity_id) → legal_entity(id)`。
- FK：`(legal_entity_id, department_id) → department(legal_entity_id, id)`。
- FK：`(legal_entity_id, owner_id) → app_user(legal_entity_id, id)`。
- FK：`(legal_entity_id, created_by) → app_user(legal_entity_id, id)`。
- CHECK：`revision > 0`。
- CHECK：`lock_version > 0`。
- 普通索引：`(legal_entity_id, department_id, status, created_at, id)`。
- 普通索引：`(owner_id, created_at, id)`。
- 普通索引：`(legal_entity_id, department_id)`。
- 普通索引：`(legal_entity_id, owner_id)`。
- 普通索引：`(legal_entity_id, created_by)`。

补充约束与执行规则：

- 状态值按 document_type 由类型化业务服务校验，领域主表不复制当前审批状态。每条记录恰有一个类型匹配的领域主表，在同一事务创建；延迟约束触发器在事务结束验证类型和领域行存在性。当前 (id, revision) 延迟外键指向 document_revision(document_id, revision)。

<a id="table-document_revision"></a>

### `document_revision`：单据内容版本

所属模块：`foundation`；类型：版本表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | --------------- | ---- | ------ | ---------- |
| `id` | `uuid` | 否 | `无` | 主键；应用生成 UUID；PK |
| `legal_entity_id` | `uuid` | 否 | `无` | 所属法人；FK → `legal_entity.id` |
| `document_id` | `uuid` | 否 | `无` | 所属单据；FK → `business_document.id` |
| `revision` | `integer` | 否 | `无` | 内容版本 |
| `state` | `varchar(32)` | 否 | `'WORKING'` | 允许值：WORKING, FROZEN |
| `snapshot` | `jsonb` | 否 | `'{}'::jsonb` | 冻结时完整单据头与明细快照 |
| `content_hash` | `varchar(64)` | 是 | `无` | 冻结内容摘要 |
| `frozen_at` | `timestamptz` | 是 | `无` | 冻结时间 |
| `created_at` | `timestamptz` | 否 | `CURRENT_TIMESTAMP` | 创建时间 |

数据库约束与索引：

- PK：`id`。
- UQ：`(document_id, revision)`。
- UQ：`(legal_entity_id, id)`。
- FK：`(legal_entity_id) → legal_entity(id)`。
- FK：`(legal_entity_id, document_id) → business_document(legal_entity_id, id)`。
- CHECK：`revision > 0`。
- CHECK：`(state = 'WORKING' AND frozen_at IS NULL) OR (state = 'FROZEN' AND frozen_at IS NOT NULL AND content_hash IS NOT NULL)`。
- 普通索引：`(legal_entity_id, document_id)`。

补充约束与执行规则：

- 创建草稿同时创建 WORKING 版本；提交、发布或直接过账时冻结。冻结内容和明细通过数据库触发器禁止改写。修订创建新版本和新明细 ID，旧引用保留。

<a id="table-attachment"></a>

### `attachment`：附件

所属模块：`foundation`；类型：文件元数据表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | --------------- | ---- | ------ | ---------- |
| `id` | `uuid` | 否 | `无` | 主键；应用生成 UUID；PK |
| `legal_entity_id` | `uuid` | 否 | `无` | 所属法人；FK → `legal_entity.id` |
| `document_id` | `uuid` | 是 | `无` | 所属单据，未绑定上传可为空；FK → `business_document.id` |
| `uploaded_by` | `uuid` | 否 | `无` | 上传人；FK → `app_user.id` |
| `object_key` | `varchar(512)` | 否 | `无` | 受控文件存储键 |
| `original_name` | `varchar(255)` | 否 | `无` | 原文件名 |
| `content_type` | `varchar(100)` | 否 | `无` | 验证后的内容类型 |
| `size_bytes` | `bigint` | 否 | `无` | 字节数 |
| `sha256` | `varchar(64)` | 否 | `无` | 内容摘要 |
| `status` | `varchar(32)` | 否 | `'STAGED'` | 允许值：STAGED, BOUND, QUARANTINED |
| `created_at` | `timestamptz` | 否 | `CURRENT_TIMESTAMP` | 创建时间 |

数据库约束与索引：

- PK：`id`。
- UQ：`(object_key)`。
- UQ：`(legal_entity_id, id)`。
- FK：`(legal_entity_id) → legal_entity(id)`。
- FK：`(legal_entity_id, document_id) → business_document(legal_entity_id, id)`。
- FK：`(legal_entity_id, uploaded_by) → app_user(legal_entity_id, id)`。
- CHECK：`size_bytes >= 0`。
- CHECK：`status <> 'BOUND' OR document_id IS NOT NULL`。
- 普通索引：`(legal_entity_id, document_id)`。
- 普通索引：`(legal_entity_id, uploaded_by)`。

补充约束与执行规则：

- STAGED 文件仅上传者可见，绑定单据须校验法人和权限；后台清理未绑定且过期文件。资质附件必须先绑定准入申请单据。

<a id="table-audit_log"></a>

### `audit_log`：业务审计

所属模块：`foundation`；类型：追加记录表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | --------------- | ---- | ------ | ---------- |
| `id` | `uuid` | 否 | `无` | 主键；应用生成 UUID；PK |
| `legal_entity_id` | `uuid` | 否 | `无` | 所属法人；FK → `legal_entity.id` |
| `actor_user_id` | `uuid` | 是 | `无` | 实际用户；FK → `app_user.id` |
| `service_client_id` | `uuid` | 是 | `无` | 调用服务；FK → `service_client.id` |
| `document_id` | `uuid` | 是 | `无` | 相关单据；FK → `business_document.id` |
| `action` | `varchar(120)` | 否 | `无` | 业务动作 |
| `request_id` | `varchar(64)` | 否 | `无` | 请求追踪标识 |
| `assistant_task_id` | `varchar(128)` | 是 | `无` | 助手任务标识 |
| `change_summary` | `jsonb` | 否 | `'{}'::jsonb` | 必要的变更摘要 |
| `reason` | `text` | 是 | `无` | 业务原因 |
| `created_at` | `timestamptz` | 否 | `CURRENT_TIMESTAMP` | 创建时间 |

数据库约束与索引：

- PK：`id`。
- UQ：`(legal_entity_id, id)`。
- FK：`(legal_entity_id) → legal_entity(id)`。
- FK：`(legal_entity_id, actor_user_id) → app_user(legal_entity_id, id)`。
- FK：`(legal_entity_id, service_client_id) → service_client(legal_entity_id, id)`。
- FK：`(legal_entity_id, document_id) → business_document(legal_entity_id, id)`。
- CHECK：`actor_user_id IS NOT NULL OR service_client_id IS NOT NULL`。
- 普通索引：`(document_id, created_at, id)`。
- 普通索引：`(request_id)`。
- 普通索引：`(legal_entity_id, actor_user_id)`。
- 普通索引：`(legal_entity_id, service_client_id)`。
- 普通索引：`(legal_entity_id, document_id)`。

补充约束与执行规则：

- 追加写；不保存凭证原文。失败鉴权另记安全日志，不依赖业务事务成功。

<a id="table-business_policy_version"></a>

### `business_policy_version`：业务规则配置版本

所属模块：`foundation`；类型：规则表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | --------------- | ---- | ------ | ---------- |
| `id` | `uuid` | 否 | `无` | 主键；应用生成 UUID；PK |
| `legal_entity_id` | `uuid` | 否 | `无` | 所属法人；FK → `legal_entity.id` |
| `policy_code` | `varchar(64)` | 否 | `无` | 采购方式/匹配容差/提醒等规则编码 |
| `version` | `integer` | 否 | `无` | 版本 |
| `settings` | `jsonb` | 否 | `'{}'::jsonb` | 经类型化 schema 校验的配置 |
| `approved_by` | `uuid` | 否 | `无` | 配置批准人；FK → `app_user.id` |
| `effective_at` | `timestamptz` | 否 | `无` | 生效时间 |
| `retired_at` | `timestamptz` | 是 | `无` | 停用时间 |
| `created_at` | `timestamptz` | 否 | `CURRENT_TIMESTAMP` | 创建时间 |

数据库约束与索引：

- PK：`id`。
- UQ：`(legal_entity_id, policy_code, version)`。
- UQ：`(legal_entity_id, id)`。
- FK：`(legal_entity_id) → legal_entity(id)`。
- FK：`(legal_entity_id, approved_by) → app_user(legal_entity_id, id)`。
- CHECK：`version > 0`。
- 普通索引：`(legal_entity_id, approved_by)`。

补充约束与执行规则：

- 补齐概念设计中的业务规则版本实体；使用方保存 ID 或 policy_code/version。审核生效后禁止覆盖内容。
- settings 按 policy_code 类型化：METHOD_APPROVAL 配置方式/公司/类别/金额阈值/例外/审批角色；ORDER_AUTO_APPROVAL 配置 RFQ/RFP 开关、金额阈值、关键字段清单与强制人工条件；BID_OPENING 配置独立见证角色与禁止同人规则；REQUEST_BUDGET 配置按范围比例、币种精度及批准后修订规则。缺少配置阻止相关提交，不默认免审。

<a id="table-currency"></a>

### `currency`：币种

所属模块：`foundation`；类型：主数据表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | --------------- | ---- | ------ | ---------- |
| `code` | `char(3)` | 否 | `无` | 币种代码；PK |
| `name` | `varchar(80)` | 否 | `无` | 显示名称 |
| `minor_units` | `smallint` | 否 | `无` | 允许金额小数位，CHECK 0 至 6 |
| `enabled` | `boolean` | 否 | `true` | 是否允许新单据使用 |

数据库约束与索引：

- PK：`code`。
- CHECK：`minor_units BETWEEN 0 AND 6`。
- 已被正式单据使用的精度不直接改写；停用不阻止历史结算。

<a id="table-exchange_rate_snapshot"></a>

### `exchange_rate_snapshot`：汇率快照

所属模块：`foundation`；类型：业务明细表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | --------------- | ---- | ------ | ---------- |
| `id` | `uuid` | 否 | `无` | 主键；应用生成 UUID |
| `legal_entity_id` | `uuid` | 否 | `无` | 所属公司；FK → `legal_entity.id` |
| `from_currency` | `char(3)` | 否 | `无` | 原币；FK → `currency.code` |
| `to_currency` | `char(3)` | 否 | `无` | 目标币；FK → `currency.code` |
| `rate` | `numeric(28,12)` | 否 | `无` | 目标币金额 = 原币金额 × rate |
| `rate_date` | `date` | 否 | `无` | 汇率日期 |
| `source` | `varchar(200)` | 否 | `无` | 获准汇率来源 |
| `purpose` | `varchar(32)` | 否 | `无` | CHECK 允许值：ESTIMATE, COMMITMENT, COMPARISON |
| `policy_version_id` | `uuid` | 否 | `无` | 汇率口径；FK → `business_policy_version.id` |
| `recorded_by` | `uuid` | 否 | `无` | 记录人；FK → `app_user.id` |
| `created_at` | `timestamptz` | 否 | `CURRENT_TIMESTAMP` | 创建时间 |

数据库约束与索引：

- PK：`id`。
- UQ：`(legal_entity_id, id)`。
- FK：`(legal_entity_id) → legal_entity(id)`。
- FK：`(from_currency) → currency(code)`。
- FK：`(to_currency) → currency(code)`。
- FK：`(legal_entity_id, policy_version_id) → business_policy_version(legal_entity_id, id)`。
- FK：`(legal_entity_id, recorded_by) → app_user(legal_entity_id, id)`。
- CHECK：`rate > 0`。
- CHECK：`from_currency <> to_currency OR rate = 1`。
- 正式引用后不可编辑；同币种也记录 rate=1。原币、目标币字段类型为 char(3)，FK 直接指 currency.code。

## 4. 审批模块

<a id="table-approval_rule_version"></a>

### `approval_rule_version`：审批规则版本

所属模块：`approvals`；类型：规则表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | --------------- | ---- | ------ | ---------- |
| `id` | `uuid` | 否 | `无` | 主键；应用生成 UUID；PK |
| `legal_entity_id` | `uuid` | 否 | `无` | 所属法人；FK → `legal_entity.id` |
| `rule_code` | `varchar(64)` | 否 | `无` | 规则标识 |
| `version` | `integer` | 否 | `无` | 配置版本 |
| `document_type` | `varchar(40)` | 否 | `无` | 适用单据类型 |
| `department_id` | `uuid` | 是 | `无` | 限定部门；FK → `department.id` |
| `min_amount` | `numeric(24,6)` | 否 | `0` | 含税金额下界 |
| `max_amount` | `numeric(24,6)` | 是 | `无` | 上界，空为无上限 |
| `steps_config` | `jsonb` | 否 | `'[]'::jsonb` | 有序节点与候选用户/角色配置 |
| `effective_at` | `timestamptz` | 否 | `无` | 生效时间 |
| `retired_at` | `timestamptz` | 是 | `无` | 停用时间 |
| `created_at` | `timestamptz` | 否 | `CURRENT_TIMESTAMP` | 创建时间 |

数据库约束与索引：

- PK：`id`。
- UQ：`(legal_entity_id, rule_code, version)`。
- UQ：`(legal_entity_id, id)`。
- FK：`(legal_entity_id) → legal_entity(id)`。
- FK：`(legal_entity_id, department_id) → department(legal_entity_id, id)`。
- CHECK：`version > 0`。
- CHECK：`min_amount >= 0`。
- CHECK：`max_amount IS NULL OR max_amount >= min_amount`。
- 普通索引：`(legal_entity_id, department_id)`。

补充约束与执行规则：

- 生效规则快照不可修改；同范围规则重叠由配置服务阻止，找不到唯一适用规则拒绝提交。

<a id="table-approval_instance"></a>

### `approval_instance`：审批实例

所属模块：`approvals`；类型：审批运行表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | --------------- | ---- | ------ | ---------- |
| `id` | `uuid` | 否 | `无` | 主键；应用生成 UUID；PK |
| `legal_entity_id` | `uuid` | 否 | `无` | 所属法人；FK → `legal_entity.id` |
| `document_id` | `uuid` | 否 | `无` | 目标单据；FK → `business_document.id` |
| `revision` | `integer` | 否 | `无` | 绑定冻结版本 |
| `rule_version_id` | `uuid` | 否 | `无` | 规则版本；FK → `approval_rule_version.id` |
| `submitted_by` | `uuid` | 否 | `无` | 提交人；FK → `app_user.id` |
| `status` | `varchar(32)` | 否 | `'IN_REVIEW'` | 允许值：IN_REVIEW, APPROVED, RETURNED, REJECTED, WITHDRAWN |
| `current_step_no` | `integer` | 否 | `1` | 当前顺序节点 |
| `completed_at` | `timestamptz` | 是 | `无` | 结束时间 |
| `created_at` | `timestamptz` | 否 | `CURRENT_TIMESTAMP` | 创建时间 |

数据库约束与索引：

- PK：`id`。
- UQ：`(legal_entity_id, id)`。
- FK：`(legal_entity_id) → legal_entity(id)`。
- FK：`(legal_entity_id, document_id) → business_document(legal_entity_id, id)`。
- FK：`(legal_entity_id, rule_version_id) → approval_rule_version(legal_entity_id, id)`。
- FK：`(legal_entity_id, submitted_by) → app_user(legal_entity_id, id)`。
- CHECK：`revision > 0`。
- CHECK：`current_step_no > 0`。
- 普通索引：`(status, created_at, id)`。
- 普通索引：`(legal_entity_id, document_id)`。
- 普通索引：`(legal_entity_id, rule_version_id)`。
- 普通索引：`(legal_entity_id, submitted_by)`。

补充约束与执行规则：

- 复合外键 (document_id, revision) → document_revision(document_id, revision)。部分唯一索引 (document_id, revision) WHERE status = IN_REVIEW；允许退回后新版本重提。

<a id="table-approval_step"></a>

### `approval_step`：审批节点

所属模块：`approvals`；类型：审批明细表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | --------------- | ---- | ------ | ---------- |
| `id` | `uuid` | 否 | `无` | 主键；应用生成 UUID；PK |
| `legal_entity_id` | `uuid` | 否 | `无` | 所属法人；FK → `legal_entity.id` |
| `instance_id` | `uuid` | 否 | `无` | 所属审批实例；FK → `approval_instance.id` |
| `step_no` | `integer` | 否 | `无` | 顺序号 |
| `assignee_id` | `uuid` | 否 | `无` | 当前审批人；FK → `app_user.id` |
| `original_assignee_id` | `uuid` | 否 | `无` | 首次分配审批人；FK → `app_user.id` |
| `status` | `varchar(32)` | 否 | `'WAITING'` | 允许值：WAITING, PENDING, APPROVED, RETURNED, REJECTED, SKIPPED |
| `activated_at` | `timestamptz` | 是 | `无` | 激活时间 |
| `completed_at` | `timestamptz` | 是 | `无` | 完成时间 |
| `created_at` | `timestamptz` | 否 | `CURRENT_TIMESTAMP` | 创建时间 |

数据库约束与索引：

- PK：`id`。
- UQ：`(instance_id, step_no)`。
- UQ：`(legal_entity_id, id)`。
- FK：`(legal_entity_id) → legal_entity(id)`。
- FK：`(legal_entity_id, instance_id) → approval_instance(legal_entity_id, id)`。
- FK：`(legal_entity_id, assignee_id) → app_user(legal_entity_id, id)`。
- FK：`(legal_entity_id, original_assignee_id) → app_user(legal_entity_id, id)`。
- CHECK：`step_no > 0`。
- 普通索引：`(assignee_id, status, created_at, id)`。
- 普通索引：`(legal_entity_id, instance_id)`。
- 普通索引：`(legal_entity_id, assignee_id)`。
- 普通索引：`(legal_entity_id, original_assignee_id)`。

补充约束与执行规则：

- 首节点在创建时激活为 PENDING；终止流程的后续节点为 SKIPPED，不能以此跳过必需审批。

<a id="table-approval_decision"></a>

### `approval_decision`：审批决定与改派记录

所属模块：`approvals`；类型：追加记录表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | --------------- | ---- | ------ | ---------- |
| `id` | `uuid` | 否 | `无` | 主键；应用生成 UUID；PK |
| `legal_entity_id` | `uuid` | 否 | `无` | 所属法人；FK → `legal_entity.id` |
| `step_id` | `uuid` | 否 | `无` | 目标节点；FK → `approval_step.id` |
| `actor_id` | `uuid` | 否 | `无` | 操作者；FK → `app_user.id` |
| `decision` | `varchar(32)` | 否 | `无` | 允许值：APPROVE, RETURN, REJECT, REASSIGN |
| `new_assignee_id` | `uuid` | 是 | `无` | 改派后的审批人；FK → `app_user.id` |
| `comment` | `text` | 否 | `无` | 意见或改派原因 |
| `expected_lock_version` | `integer` | 否 | `无` | 提交时单据并发版本 |
| `created_at` | `timestamptz` | 否 | `CURRENT_TIMESTAMP` | 创建时间 |

数据库约束与索引：

- PK：`id`。
- UQ：`(legal_entity_id, id)`。
- FK：`(legal_entity_id) → legal_entity(id)`。
- FK：`(legal_entity_id, step_id) → approval_step(legal_entity_id, id)`。
- FK：`(legal_entity_id, actor_id) → app_user(legal_entity_id, id)`。
- FK：`(legal_entity_id, new_assignee_id) → app_user(legal_entity_id, id)`。
- CHECK：`(decision = 'REASSIGN') = (new_assignee_id IS NOT NULL)`。
- 普通索引：`(legal_entity_id, step_id)`。
- 普通索引：`(legal_entity_id, actor_id)`。
- 普通索引：`(legal_entity_id, new_assignee_id)`。

补充约束与执行规则：

- 部分唯一索引 (step_id) WHERE decision IN (APPROVE, RETURN, REJECT)，一个节点只有一个最终决定；REASSIGN 可有多条且不可覆盖。

## 5. 集成与幂等模块

<a id="table-idempotency_record"></a>

### `idempotency_record`：接口幂等结果

所属模块：`integrations`；类型：集成记录表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | --------------- | ---- | ------ | ---------- |
| `id` | `uuid` | 否 | `无` | 主键；应用生成 UUID；PK |
| `legal_entity_id` | `uuid` | 否 | `无` | 所属法人；FK → `legal_entity.id` |
| `actor_user_id` | `uuid` | 是 | `无` | 用户或委托用户；FK → `app_user.id` |
| `service_client_id` | `uuid` | 是 | `无` | 服务调用者；FK → `service_client.id` |
| `principal_key` | `varchar(160)` | 否 | `无` | 后端由用户与服务身份组合生成 |
| `action` | `varchar(160)` | 否 | `无` | 含目标资源路径的动作 |
| `idempotency_key` | `varchar(128)` | 否 | `无` | 客户端幂等键 |
| `request_hash` | `varchar(64)` | 否 | `无` | 规范化输入摘要 |
| `http_status` | `smallint` | 否 | `无` | 已提交响应状态 |
| `response_body` | `jsonb` | 否 | `'{}'::jsonb` | 权限裁剪后的原响应 |
| `document_id` | `uuid` | 是 | `无` | 结果单据；FK → `business_document.id` |
| `created_at` | `timestamptz` | 否 | `CURRENT_TIMESTAMP` | 创建时间 |

数据库约束与索引：

- PK：`id`。
- UQ：`(legal_entity_id, principal_key, action, idempotency_key)`。
- UQ：`(legal_entity_id, id)`。
- FK：`(legal_entity_id) → legal_entity(id)`。
- FK：`(legal_entity_id, actor_user_id) → app_user(legal_entity_id, id)`。
- FK：`(legal_entity_id, service_client_id) → service_client(legal_entity_id, id)`。
- FK：`(legal_entity_id, document_id) → business_document(legal_entity_id, id)`。
- CHECK：`actor_user_id IS NOT NULL OR service_client_id IS NOT NULL`。
- CHECK：`http_status BETWEEN 200 AND 299`。
- 普通索引：`(legal_entity_id, actor_user_id)`。
- 普通索引：`(legal_entity_id, service_client_id)`。
- 普通索引：`(legal_entity_id, document_id)`。

补充约束与执行规则：

- 原子 INSERT ON CONFLICT/唯一键锁；记录与业务成功一起提交。失败事务不留成功记录。查询幂等结果仍校验当前身份与对象可见性；业务数据保留期间不自动过期删除本键。
- 首期在业务操作前对作用域与幂等键的稳定哈希取得事务级 advisory lock，读取已有结果；成功时再插入完整结果。哈希碰撞最多导致额外串行，不构成身份匹配。不得先提交业务、再异步写幂等结果。

<a id="table-business_change"></a>

### `business_change`：业务变化

所属模块：`integrations`；类型：追加记录表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | --------------- | ---- | ------ | ---------- |
| `id` | `uuid` | 否 | `无` | 主键；应用生成 UUID；PK |
| `legal_entity_id` | `uuid` | 否 | `无` | 所属法人；FK → `legal_entity.id` |
| `document_id` | `uuid` | 是 | `无` | 相关单据；FK → `business_document.id` |
| `object_type` | `varchar(64)` | 否 | `无` | 对象类别 |
| `object_id` | `uuid` | 否 | `无` | 变更对象标识 |
| `object_version` | `integer` | 否 | `无` | 对象业务或锁版本 |
| `event_type` | `varchar(100)` | 否 | `无` | 事件类型 |
| `payload` | `jsonb` | 否 | `'{}'::jsonb` | 最小事件载荷，禁止存令牌 |
| `request_id` | `varchar(64)` | 否 | `无` | 产生变化的请求 |
| `created_at` | `timestamptz` | 否 | `CURRENT_TIMESTAMP` | 创建时间 |

数据库约束与索引：

- PK：`id`。
- UQ：`(legal_entity_id, id)`。
- FK：`(legal_entity_id) → legal_entity(id)`。
- FK：`(legal_entity_id, document_id) → business_document(legal_entity_id, id)`。
- CHECK：`object_version > 0`。
- 普通索引：`(legal_entity_id, document_id)`。

补充约束与执行规则：

- object_id 是外部可见事件定位，不宣称为多态外键；单据事件必须填 document_id，其他事件通过类型化写入服务验证对象。与业务事务同提交。

<a id="table-publication_counter"></a>

### `publication_counter`：变化发布计数器

所属模块：`integrations`；类型：单例协调表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | --------------- | ---- | ------ | ---------- |
| `id` | `uuid` | 否 | `无` | 主键；应用生成 UUID；PK |
| `counter_name` | `varchar(32)` | 否 | `'business_changes'` | 固定名称 |
| `last_sequence` | `bigint` | 否 | `0` | 最后已发布序号 |
| `created_at` | `timestamptz` | 否 | `CURRENT_TIMESTAMP` | 创建时间 |

数据库约束与索引：

- PK：`id`。
- UQ：`(counter_name)`。
- CHECK：`counter_name = 'business_changes'`。
- CHECK：`last_sequence >= 0`。

补充约束与执行规则：

- 初始化预建一行，SELECT FOR UPDATE 锁定后递增；不能用会提前分配序号的普通 sequence 代替提交顺序。

<a id="table-change_publication"></a>

### `change_publication`：已发布变化

所属模块：`integrations`；类型：追加记录表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | --------------- | ---- | ------ | ---------- |
| `id` | `uuid` | 否 | `无` | 主键；应用生成 UUID；PK |
| `legal_entity_id` | `uuid` | 否 | `无` | 所属法人；FK → `legal_entity.id` |
| `change_id` | `uuid` | 否 | `无` | 变化记录；FK → `business_change.id` |
| `publication_sequence` | `bigint` | 否 | `无` | 发布器事务分配的顺序号 |
| `published_at` | `timestamptz` | 否 | `无` | 发布时间 |
| `created_at` | `timestamptz` | 否 | `CURRENT_TIMESTAMP` | 创建时间 |

数据库约束与索引：

- PK：`id`。
- UQ：`(change_id)`。
- UQ：`(publication_sequence)`。
- UQ：`(legal_entity_id, id)`。
- FK：`(legal_entity_id) → legal_entity(id)`。
- FK：`(legal_entity_id, change_id) → business_change(legal_entity_id, id)`。
- CHECK：`publication_sequence > 0`。
- 普通索引：`(legal_entity_id, change_id)`。

补充约束与执行规则：

- 同变化至多一条发布记录；与计数器递增同一短事务提交。

## 6. 供应商模块

<a id="table-supplier"></a>

### `supplier`：供应商档案

所属模块：`suppliers`；类型：主数据表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | --------------- | ---- | ------ | ---------- |
| `id` | `uuid` | 否 | `无` | 主键；应用生成 UUID；PK |
| `legal_entity_id` | `uuid` | 否 | `无` | 所属法人；FK → `legal_entity.id` |
| `code` | `varchar(64)` | 否 | `无` | 供应商编码 |
| `name` | `varchar(200)` | 否 | `无` | 法定/登记名称 |
| `registration_type` | `varchar(32)` | 否 | `无` | 登记标识类型 |
| `registration_no` | `varchar(100)` | 否 | `无` | 规范化登记标识 |
| `contact_name` | `varchar(100)` | 否 | `无` | 联系人 |
| `contact_phone` | `varchar(64)` | 否 | `无` | 联系方式 |
| `admission_status` | `varchar(32)` | 否 | `'PROSPECT'` | 允许值：PROSPECT, DRAFT, IN_REVIEW, APPROVED, REJECTED |
| `operating_status` | `varchar(32)` | 否 | `'ACTIVE'` | 允许值：ACTIVE, SUSPENDED, ARCHIVED |
| `lock_version` | `integer` | 否 | `1` | 档案并发版本 |
| `updated_at` | `timestamptz` | 否 | `无` | 更新时间 |
| `created_at` | `timestamptz` | 否 | `CURRENT_TIMESTAMP` | 创建时间 |

数据库约束与索引：

- PK：`id`。
- UQ：`(legal_entity_id, code)`。
- UQ：`(legal_entity_id, registration_type, registration_no)`。
- UQ：`(legal_entity_id, id)`。
- FK：`(legal_entity_id) → legal_entity(id)`。
- CHECK：`lock_version > 0`。

补充约束与执行规则：

- 订单核准（含合同派生订单）必须同时满足 APPROVED 和 ACTIVE；PROSPECT（潜在供应商）仅允许参与受邀寻源、提交密封响应与评审比选，拟定标后必须通过正式准入审批才可进入签约与订单生效。运营状态 ACTIVE 单独不构成准入。

<a id="table-supplier_category"></a>

### `supplier_category`：供应商品类关系

所属模块：`suppliers`；类型：关联表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | --------------- | ---- | ------ | ---------- |
| `id` | `uuid` | 否 | `无` | 主键；应用生成 UUID；PK |
| `legal_entity_id` | `uuid` | 否 | `无` | 所属法人；FK → `legal_entity.id` |
| `supplier_id` | `uuid` | 否 | `无` | 供应商；FK → `supplier.id` |
| `category_id` | `uuid` | 否 | `无` | 可供货品类；FK → `category.id` |
| `created_at` | `timestamptz` | 否 | `CURRENT_TIMESTAMP` | 创建时间 |

数据库约束与索引：

- PK：`id`。
- UQ：`(supplier_id, category_id)`。
- UQ：`(legal_entity_id, id)`。
- FK：`(legal_entity_id) → legal_entity(id)`。
- FK：`(legal_entity_id, supplier_id) → supplier(legal_entity_id, id)`。
- FK：`(legal_entity_id, category_id) → category(legal_entity_id, id)`。
- 普通索引：`(legal_entity_id, supplier_id)`。
- 普通索引：`(legal_entity_id, category_id)`。

<a id="table-supplier_qualification"></a>

### `supplier_qualification`：供应商资质

所属模块：`suppliers`；类型：业务明细表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | --------------- | ---- | ------ | ---------- |
| `id` | `uuid` | 否 | `无` | 主键；应用生成 UUID；PK |
| `legal_entity_id` | `uuid` | 否 | `无` | 所属法人；FK → `legal_entity.id` |
| `supplier_id` | `uuid` | 否 | `无` | 所属供应商；FK → `supplier.id` |
| `admission_id` | `uuid` | 否 | `无` | 来源准入申请；FK → `supplier_admission.id` |
| `qualification_type` | `varchar(32)` | 否 | `无` | 资质类别 |
| `certificate_no` | `varchar(100)` | 否 | `无` | 证书号码 |
| `attachment_id` | `uuid` | 否 | `无` | 证照附件；FK → `attachment.id` |
| `valid_from` | `date` | 否 | `无` | 有效起日 |
| `valid_to` | `date` | 是 | `无` | 有效止日 |
| `status` | `varchar(32)` | 否 | `'PENDING'` | 允许值：PENDING, ACTIVE, REPLACED, REVOKED |
| `created_at` | `timestamptz` | 否 | `CURRENT_TIMESTAMP` | 创建时间 |

数据库约束与索引：

- PK：`id`。
- UQ：`(legal_entity_id, id)`。
- FK：`(legal_entity_id) → legal_entity(id)`。
- FK：`(legal_entity_id, supplier_id) → supplier(legal_entity_id, id)`。
- FK：`(legal_entity_id, admission_id) → supplier_admission(legal_entity_id, id)`。
- FK：`(legal_entity_id, attachment_id) → attachment(legal_entity_id, id)`。
- CHECK：`valid_to IS NULL OR valid_to >= valid_from`。
- 普通索引：`(supplier_id, status, valid_to)`。
- 普通索引：`(legal_entity_id, supplier_id)`。
- 普通索引：`(legal_entity_id, admission_id)`。
- 普通索引：`(legal_entity_id, attachment_id)`。

补充约束与执行规则：

- 更新资质新增记录，经准入批准后生效；旧资质标记 REPLACED，不覆盖历史。

<a id="table-supplier_admission"></a>

### `supplier_admission`：供应商准入申请

所属模块：`suppliers`；类型：单据主表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | --------------- | ---- | ------ | ---------- |
| `id` | `uuid` | 否 | `无` | 主键；沿用 business_document.id；FK → `business_document.id`；PK |
| `legal_entity_id` | `uuid` | 否 | `无` | 所属法人；FK → `legal_entity.id` |
| `supplier_id` | `uuid` | 否 | `无` | 申请准入的供应商；FK → `supplier.id` |
| `submitted_profile` | `jsonb` | 否 | `'{}'::jsonb` | 申请资料与供货范围快照 |
| `reason` | `text` | 否 | `无` | 申请原因 |
| `created_at` | `timestamptz` | 否 | `CURRENT_TIMESTAMP` | 创建时间 |

数据库约束与索引：

- PK：`id`。
- UQ：`(legal_entity_id, id)`。
- FK：`(legal_entity_id, id) → business_document(legal_entity_id, id)`。
- FK：`(legal_entity_id) → legal_entity(id)`。
- FK：`(legal_entity_id, supplier_id) → supplier(legal_entity_id, id)`。
- 普通索引：`(legal_entity_id, supplier_id)`。
- 主表类型须与 `business_document.document_type` 匹配，状态使用通用登记的当前状态。

<a id="table-supplier_status_request"></a>

### `supplier_status_request`：供应商状态变更申请

所属模块：`suppliers`；类型：单据主表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | --------------- | ---- | ------ | ---------- |
| `id` | `uuid` | 否 | `无` | 主键；沿用 business_document.id；FK → `business_document.id`；PK |
| `legal_entity_id` | `uuid` | 否 | `无` | 所属法人；FK → `legal_entity.id` |
| `supplier_id` | `uuid` | 否 | `无` | 供应商；FK → `supplier.id` |
| `target_status` | `varchar(32)` | 否 | `'SUSPENDED'` | 允许值：ACTIVE, SUSPENDED, ARCHIVED |
| `reason` | `text` | 否 | `无` | 状态变更理由 |
| `expected_supplier_version` | `integer` | 否 | `无` | 提交时档案版本 |
| `created_at` | `timestamptz` | 否 | `CURRENT_TIMESTAMP` | 创建时间 |

数据库约束与索引：

- PK：`id`。
- UQ：`(legal_entity_id, id)`。
- FK：`(legal_entity_id, id) → business_document(legal_entity_id, id)`。
- FK：`(legal_entity_id) → legal_entity(id)`。
- FK：`(legal_entity_id, supplier_id) → supplier(legal_entity_id, id)`。
- 普通索引：`(legal_entity_id, supplier_id)`。
- 主表类型须与 `business_document.document_type` 匹配，状态使用通用登记的当前状态。

补充约束与执行规则：

- 批准时重新验证来源版本和恢复准入条件；不直接写任意准入状态。

<a id="table-supplier_performance_snapshot"></a>

### `supplier_performance_snapshot`：供应商绩效快照

所属模块：`suppliers`；类型：统计快照表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | --------------- | ---- | ------ | ---------- |
| `id` | `uuid` | 否 | `无` | 主键；应用生成 UUID；PK |
| `legal_entity_id` | `uuid` | 否 | `无` | 所属法人；FK → `legal_entity.id` |
| `supplier_id` | `uuid` | 否 | `无` | 供应商；FK → `supplier.id` |
| `period_start` | `date` | 否 | `无` | 统计起日 |
| `period_end` | `date` | 否 | `无` | 统计止日 |
| `rule_version` | `varchar(64)` | 否 | `无` | 指标规则版本 |
| `sample_count` | `integer` | 否 | `无` | 到期订单行样本数 |
| `on_time_count` | `integer` | 否 | `无` | 准时完成原约定数量的行数 |
| `exception_count` | `integer` | 否 | `无` | 取消/退货/改期行数 |
| `source_manifest` | `jsonb` | 否 | `'{}'::jsonb` | 样本订单行 ID 与版本清单 |
| `data_cutoff` | `timestamptz` | 否 | `无` | 数据截止时间 |
| `created_at` | `timestamptz` | 否 | `CURRENT_TIMESTAMP` | 创建时间 |

数据库约束与索引：

- PK：`id`。
- UQ：`(supplier_id, period_start, period_end, rule_version, data_cutoff)`。
- UQ：`(legal_entity_id, id)`。
- FK：`(legal_entity_id) → legal_entity(id)`。
- FK：`(legal_entity_id, supplier_id) → supplier(legal_entity_id, id)`。
- CHECK：`period_end >= period_start`。
- CHECK：`sample_count >= 0`。
- CHECK：`on_time_count BETWEEN 0 AND sample_count`。
- CHECK：`exception_count BETWEEN 0 AND sample_count`。
- 普通索引：`(legal_entity_id, supplier_id)`。

补充约束与执行规则：

- 比率由计数计算，sample_count=0 返回无样本，不存虚构分数。

## 7. 采购模块

<a id="table-purchase_request"></a>

### `purchase_request`：采购申请主表

所属模块：`procurement`；类型：单据主表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | --------------- | ---- | ------ | ---------- |
| `id` | `uuid` | 否 | `无` | 主键；沿用 business_document.id；FK → `business_document.id`；PK |
| `legal_entity_id` | `uuid` | 否 | `无` | 所属法人；FK → `legal_entity.id` |
| `requester_id` | `uuid` | 否 | `无` | 申请人；FK → `app_user.id` |
| `purpose` | `text` | 否 | `无` | 采购用途 |
| `required_date` | `date` | 否 | `无` | 期望到货日期 |
| `estimated_gross_amount` | `numeric(24,6)` | 否 | `0` | 预计含税合计 |
| `created_at` | `timestamptz` | 否 | `CURRENT_TIMESTAMP` | 创建时间 |
| `currency` | `char(3)` | 否 | `无` | 申请币种；FK → `currency.code` |
| `approved_revision` | `integer` | 是 | `无` | 当前有效批准内容版本；初次未批准为空 |
| `active_change_id` | `uuid` | 是 | `无` | 正在办理的修订或终止；FK → `request_change_request.id` |

数据库约束与索引：

- PK：`id`。
- UQ：`(legal_entity_id, id)`。
- FK：`(legal_entity_id, id) → business_document(legal_entity_id, id)`。
- FK：`(legal_entity_id) → legal_entity(id)`。
- FK：`(legal_entity_id, requester_id) → app_user(legal_entity_id, id)`。
- 普通索引：`(legal_entity_id, requester_id)`。
- 主表类型须与 `business_document.document_type` 匹配，状态使用通用登记的当前状态。

补充约束与执行规则：

- 申请批准原子建立逐行预算预占；仅初次审批退回/拒绝不产生预占。修改已批准申请另走 request_change_request。
- FK：`(currency) → currency(code)`。
- FK：`(legal_entity_id, active_change_id) → request_change_request(legal_entity_id, id)`。
- FK：`(id, approved_revision) → document_revision(document_id, revision)`。通用 revision 表示当前工作版本，approved_revision 才是可采购的批准版本。
- active_change_id 指向本申请的提案，触发器验证 request_change_request.request_id=id；通用状态在修订中仍保留 APPROVED，工作版本与 approved_revision 分开显示。

<a id="table-purchase_request_line"></a>

### `purchase_request_line`：采购申请明细表

所属模块：`procurement`；类型：单据明细表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | --------------- | ---- | ------ | ---------- |
| `id` | `uuid` | 否 | `无` | 主键；应用生成 UUID；PK |
| `legal_entity_id` | `uuid` | 否 | `无` | 所属法人；FK → `legal_entity.id` |
| `purchase_request_id` | `uuid` | 否 | `无` | 所属主表；FK → `purchase_request.id` |
| `revision` | `integer` | 否 | `1` | 所属内容版本 |
| `line_no` | `integer` | 否 | `无` | 当前内容版本内行号 |
| `material_id` | `uuid` | 是 | `无` | 物料；FK → `material.id` |
| `material_name` | `varchar(200)` | 否 | `无` | 物料名称快照 |
| `specification` | `text` | 否 | `无` | 规格快照 |
| `unit_id` | `uuid` | 是 | `无` | 基础单位；FK → `unit.id` |
| `unit_code` | `varchar(32)` | 是 | `无` | 单位编码快照 |
| `quantity` | `numeric(20,6)` | 是 | `无` | 申请数量 |
| `estimated_unit_price` | `numeric(20,6)` | 是 | `无` | 预估未税单价 |
| `estimated_tax_rate` | `numeric(9,6)` | 否 | `无` | 预估税率 |
| `estimated_gross_amount` | `numeric(24,6)` | 否 | `0` | 预计含税行金额 |
| `required_date` | `date` | 否 | `无` | 该行交期 |
| `created_at` | `timestamptz` | 否 | `CURRENT_TIMESTAMP` | 创建时间 |
| `request_item_id` | `uuid` | 否 | `无` | 稳定需求项；FK → `purchase_request_item.id` |
| `approved_scope` | `numeric(24,6)` | 否 | `无` | 本内容版本获批累计需求范围；QUANTITY 为单位数，AMOUNT 为申请币种金额 |
| `purchase_type` | `varchar(32)` | 否 | `无` | CHECK 允许值：GOODS, SERVICE, LICENSE, SUBSCRIPTION |
| `measurement_basis` | `varchar(32)` | 否 | `无` | CHECK 允许值：QUANTITY, AMOUNT |
| `category_id` | `uuid` | 否 | `无` | 采购类别；FK → `category.id` |
| `acceptance_criteria` | `text` | 是 | `无` | 非实物成果/期间/里程碑标准 |
| `service_start` | `date` | 是 | `无` | 服务或订阅起日 |
| `service_end` | `date` | 是 | `无` | 服务或订阅止日 |
| `estimated_remaining_amount` | `numeric(24,6)` | 否 | `0` | 未下单范围的预计含税成本，供本版本预算分配合计；已下单成本保持历史来源 |

数据库约束与索引：

- PK：`id`。
- UQ：`(purchase_request_id, revision, line_no)`。
- UQ：`(legal_entity_id, id)`。
- FK：`(legal_entity_id) → legal_entity(id)`。
- FK：`(legal_entity_id, purchase_request_id) → purchase_request(legal_entity_id, id)`。
- FK：`(legal_entity_id, material_id) → material(legal_entity_id, id)`。
- FK：`(legal_entity_id, unit_id) → unit(legal_entity_id, id)`。
- CHECK：`revision > 0`。
- CHECK：`line_no > 0`。
- CHECK：`quantity > 0`。
- CHECK：`estimated_unit_price >= 0`。
- CHECK：`estimated_tax_rate BETWEEN 0 AND 1`。
- 普通索引：`(legal_entity_id, purchase_request_id)`。
- 普通索引：`(legal_entity_id, material_id)`。
- 普通索引：`(legal_entity_id, unit_id)`。

补充约束与执行规则：

- 复合外键 (purchase_request_id, revision) → document_revision(document_id, revision)。修改冻结版本时建立新行 ID，旧行保留供下游引用。
- 申请行累计有效下单量通过关联订单计算，在审批订单时锁定本行并校验；不保存另一个可手工修改的批准数量。
- FK：`(legal_entity_id, request_item_id) → purchase_request_item(legal_entity_id, id)`。
- UQ：`(purchase_request_id, revision, request_item_id)`；同一需求项在同版本只出现一次。批准范围不得小于该稳定项已被有效订单占用的范围；历史来源跨版本累计。
- FK：`(legal_entity_id, category_id) → category(legal_entity_id, id)`。
- CHECK：`purchase_type <> 'GOODS' OR measurement_basis = 'QUANTITY'`；有 material_id 字段的实物行还要求 material_id IS NOT NULL。
- CHECK：`(measurement_basis = 'QUANTITY' AND quantity IS NOT NULL AND quantity > 0) OR (measurement_basis = 'AMOUNT' AND quantity IS NULL)`。
- CHECK：`approved_scope > 0 AND estimated_remaining_amount >= 0`；初次批准时 estimated_remaining_amount=estimated_gross_amount，修订后两者分别表达剩余采购预算和完整需求估算，不据完整估算重占历史订单预算。QUANTITY 下 approved_scope=quantity，AMOUNT 为稳定范围币种额度。
- CHECK：`(measurement_basis = 'QUANTITY' AND unit_id IS NOT NULL AND unit_code IS NOT NULL AND estimated_unit_price IS NOT NULL) OR (measurement_basis = 'AMOUNT' AND unit_id IS NULL AND unit_code IS NULL AND estimated_unit_price IS NULL)`。
- CHECK：`purchase_type = 'GOODS' OR acceptance_criteria IS NOT NULL`；CHECK：`(service_start IS NULL AND service_end IS NULL) OR (service_start IS NOT NULL AND service_end IS NOT NULL AND service_end >= service_start)`。
- CHECK：`purchase_type <> 'GOODS' OR material_id IS NOT NULL`。

<a id="table-rfq"></a>

### `rfq`：询价主表

所属模块：`procurement`；类型：单据主表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | --------------- | ---- | ------ | ---------- |
| `id` | `uuid` | 否 | `无` | 主键；沿用 business_document.id；FK → `business_document.id`；PK |
| `legal_entity_id` | `uuid` | 否 | `无` | 所属法人；FK → `legal_entity.id` |
| `purchase_request_id` | `uuid` | 否 | `无` | 来源申请；FK → `purchase_request.id` |
| `deadline` | `timestamptz` | 否 | `无` | 报价截止时间 |
| `currency` | `char(3)` | 否 | `无` | 报价币种；FK → `currency.code` |
| `minimum_suppliers` | `integer` | 否 | `无` | 最低报价家数快照 |
| `rule_version` | `varchar(64)` | 否 | `无` | 采购方式规则版本 |
| `exception_document_id` | `uuid` | 是 | `无` | 不足家数的已批准例外；FK → `purchase_method_decision.id` |
| `published_at` | `timestamptz` | 是 | `无` | 发布时间 |
| `closed_at` | `timestamptz` | 是 | `无` | 截止时间 |
| `unsealed_at` | `timestamptz` | 是 | `无` | 集中开标解密时间 |
| `created_at` | `timestamptz` | 否 | `CURRENT_TIMESTAMP` | 创建时间 |
| `method_decision_id` | `uuid` | 否 | `无` | ；FK → `purchase_method_decision.id` |
| `opening_policy_id` | `uuid` | 否 | `无` | ；FK → `business_policy_version.id` |
| `opened_by` | `uuid` | 是 | `无` | ；FK → `app_user.id` |
| `opening_witness_id` | `uuid` | 是 | `无` | ；FK → `app_user.id` |
| `sourcing_type` | `varchar(16)` | 否 | `无` | CHECK 允许值 RFQ, RFP, TENDER；复用征集头，不把 RFP 或招标当 RFQ 报价语义 |

数据库约束与索引：

- PK：`id`。
- UQ：`(legal_entity_id, id)`。
- FK：`(legal_entity_id, id) → business_document(legal_entity_id, id)`。
- FK：`(legal_entity_id) → legal_entity(id)`。
- FK：`(legal_entity_id, purchase_request_id) → purchase_request(legal_entity_id, id)`。
- FK：`(legal_entity_id, exception_document_id) → purchase_method_decision(legal_entity_id, id)`。
- CHECK：`minimum_suppliers > 0`。
- 普通索引：`(legal_entity_id, purchase_request_id)`。
- 普通索引：`(legal_entity_id, exception_document_id)`。
- 主表类型须与 `business_document.document_type` 匹配，状态使用通用登记的当前状态。

补充约束与执行规则：

- 状态流转：OPEN → CLOSED 必须到达 deadline；CLOSED → UNSEALED 必须有效密封响应家数满足 minimum_suppliers 或已审批绑定 exception_document_id。
- FK：`(currency) → currency(code)`；金额依币种 minor_units 校验舍入，禁止跨币种直接合计。
- FK：`(legal_entity_id, method_decision_id) → purchase_method_decision(legal_entity_id, id)`。
- FK：`(legal_entity_id, opening_policy_id) → business_policy_version(legal_entity_id, id)`。
- FK：`(legal_entity_id, opened_by) → app_user(legal_entity_id, id)`。
- FK：`(legal_entity_id, opening_witness_id) → app_user(legal_entity_id, id)`。
- 征集发布必须绑定有效且覆盖全部范围的方式决定；正常方式需审批时不得绕过。开标按角色+实际用户职责分离检查：采购员/本轮盲收经办用户不能单人触发开标，兼任角色也不能绕过。
- sourcing_type 必须等于采购方式决定的 method；同一轮 request_id/revision 和范围来自 purchase_method_scope。opened_by 与 opening_witness_id 本期为同一独立见证用户；旧 unsealed_at 是唯一开标时间，不另存 opened_at。

<a id="table-rfq_line"></a>

### `rfq_line`：询价明细表

所属模块：`procurement`；类型：单据明细表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | --------------- | ---- | ------ | ---------- |
| `id` | `uuid` | 否 | `无` | 主键；应用生成 UUID；PK |
| `legal_entity_id` | `uuid` | 否 | `无` | 所属法人；FK → `legal_entity.id` |
| `rfq_id` | `uuid` | 否 | `无` | 所属主表；FK → `rfq.id` |
| `revision` | `integer` | 否 | `1` | 所属内容版本 |
| `line_no` | `integer` | 否 | `无` | 当前内容版本内行号 |
| `request_line_id` | `uuid` | 否 | `无` | 来源申请明细；FK → `purchase_request_line.id` |
| `material_id` | `uuid` | 是 | `无` | 物料；FK → `material.id` |
| `material_name` | `varchar(200)` | 否 | `无` | 物料名称快照 |
| `specification` | `text` | 否 | `无` | 规格快照 |
| `unit_id` | `uuid` | 是 | `无` | 基础单位；FK → `unit.id` |
| `unit_code` | `varchar(32)` | 是 | `无` | 单位编码快照 |
| `quantity` | `numeric(20,6)` | 是 | `无` | 询价数量 |
| `required_date` | `date` | 否 | `无` | 要求交期 |
| `created_at` | `timestamptz` | 否 | `CURRENT_TIMESTAMP` | 创建时间 |
| `purchase_type` | `varchar(32)` | 否 | `无` | CHECK 允许值：GOODS, SERVICE, LICENSE, SUBSCRIPTION |
| `measurement_basis` | `varchar(32)` | 否 | `无` | CHECK 允许值：QUANTITY, AMOUNT |
| `category_id` | `uuid` | 否 | `无` | 采购类别；FK → `category.id` |
| `acceptance_criteria` | `text` | 是 | `无` | 非实物成果/期间/里程碑标准 |
| `service_start` | `date` | 是 | `无` | 服务或订阅起日 |
| `service_end` | `date` | 是 | `无` | 服务或订阅止日 |
| `requested_scope` | `numeric(24,6)` | 否 | `无` | 本轮征集的需求范围单位，绑定稳定需求项的数量/申请币金额 |

数据库约束与索引：

- PK：`id`。
- UQ：`(rfq_id, revision, line_no)`。
- UQ：`(rfq_id, revision, request_line_id)`。
- UQ：`(legal_entity_id, id)`。
- FK：`(legal_entity_id) → legal_entity(id)`。
- FK：`(legal_entity_id, rfq_id) → rfq(legal_entity_id, id)`。
- FK：`(legal_entity_id, request_line_id) → purchase_request_line(legal_entity_id, id)`。
- FK：`(legal_entity_id, material_id) → material(legal_entity_id, id)`。
- FK：`(legal_entity_id, unit_id) → unit(legal_entity_id, id)`。
- CHECK：`revision > 0`。
- CHECK：`line_no > 0`。
- CHECK：`quantity > 0`。
- 普通索引：`(legal_entity_id, rfq_id)`。
- 普通索引：`(legal_entity_id, request_line_id)`。
- 普通索引：`(legal_entity_id, material_id)`。
- 普通索引：`(legal_entity_id, unit_id)`。

补充约束与执行规则：

- 复合外键 (rfq_id, revision) → document_revision(document_id, revision)。修改冻结版本时建立新行 ID，旧行保留供下游引用。
- 来源申请必须是 rfq.purchase_request_id，版本必须已批准；服务校验来源和数量。
- FK：`(legal_entity_id, category_id) → category(legal_entity_id, id)`。
- CHECK：`purchase_type <> 'GOODS' OR measurement_basis = 'QUANTITY'`；有 material_id 字段的实物行还要求 material_id IS NOT NULL。
- CHECK：`(measurement_basis = 'QUANTITY' AND quantity IS NOT NULL AND quantity > 0) OR (measurement_basis = 'AMOUNT' AND quantity IS NULL)`。
- CHECK：`(measurement_basis = 'QUANTITY' AND unit_id IS NOT NULL AND unit_code IS NOT NULL) OR (measurement_basis = 'AMOUNT' AND unit_id IS NULL AND unit_code IS NULL)`。
- CHECK：`purchase_type = 'GOODS' OR acceptance_criteria IS NOT NULL`；CHECK：`(service_start IS NULL AND service_end IS NULL) OR (service_start IS NOT NULL AND service_end IS NOT NULL AND service_end >= service_start)`。
- CHECK：`purchase_type <> 'GOODS' OR material_id IS NOT NULL`。
- CHECK：`requested_scope > 0`；不得超当前可征集范围，AMOUNT 无需虚构数量，部分征集显式记范围。

<a id="table-rfq_invitation"></a>

### `rfq_invitation`：询价邀请关系

所属模块：`procurement`；类型：关联表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | --------------- | ---- | ------ | ---------- |
| `id` | `uuid` | 否 | `无` | 主键；应用生成 UUID；PK |
| `legal_entity_id` | `uuid` | 否 | `无` | 所属法人；FK → `legal_entity.id` |
| `rfq_id` | `uuid` | 否 | `无` | 询价；FK → `rfq.id` |
| `rfq_revision` | `integer` | 否 | `无` | 发布时询价版本 |
| `supplier_id` | `uuid` | 否 | `无` | 邀请供应商；FK → `supplier.id` |
| `invited_at` | `timestamptz` | 是 | `无` | 线下邀请登记时间 |
| `status` | `varchar(32)` | 否 | `'INVITED'` | 允许值：INVITED, RESPONDED, DECLINED |
| `created_at` | `timestamptz` | 否 | `CURRENT_TIMESTAMP` | 创建时间 |

数据库约束与索引：

- PK：`id`。
- UQ：`(rfq_id, rfq_revision, supplier_id)`。
- UQ：`(legal_entity_id, id)`。
- FK：`(legal_entity_id) → legal_entity(id)`。
- FK：`(legal_entity_id, rfq_id) → rfq(legal_entity_id, id)`。
- FK：`(legal_entity_id, supplier_id) → supplier(legal_entity_id, id)`。
- 普通索引：`(legal_entity_id, rfq_id)`。
- 普通索引：`(legal_entity_id, supplier_id)`。

补充约束与执行规则：

- 复合外键 (rfq_id, rfq_revision) → document_revision(document_id, revision)。邀请不表示自动发送邮件。

<a id="table-quotation"></a>

### `quotation`：供应商报价主表

所属模块：`procurement`；类型：单据主表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | --------------- | ---- | ------ | ---------- |
| `id` | `uuid` | 否 | `无` | 主键；沿用 business_document.id；FK → `business_document.id`；PK |
| `legal_entity_id` | `uuid` | 否 | `无` | 所属法人；FK → `legal_entity.id` |
| `invitation_id` | `uuid` | 否 | `无` | 报价对应邀请；FK → `rfq_invitation.id` |
| `quote_version` | `integer` | 否 | `无` | 该邀请报价版本 |
| `valid_until` | `date` | 否 | `无` | 报价有效期 |
| `currency` | `char(3)` | 否 | `无` | 币种；FK → `currency.code` |
| `payment_terms` | `text` | 否 | `无` | 付款条件 |
| `source_attachment_id` | `uuid` | 否 | `无` | 报价凭证；FK → `attachment.id` |
| `sealed_hash` | `varchar(128)` | 否 | `无` | 密封凭证（封条或文件数字哈希快照） |
| `gross_amount` | `numeric(24,6)` | 否 | `0` | 报价含税合计（开标前为 0，开标后解密写入） |
| `sealed_at` | `timestamptz` | 否 | `CURRENT_TIMESTAMP` | 盲收登记时间 |
| `unsealed_at` | `timestamptz` | 是 | `无` | 集中开标解密时间 |
| `created_at` | `timestamptz` | 否 | `CURRENT_TIMESTAMP` | 创建时间 |

数据库约束与索引：

- PK：`id`。
- UQ：`(invitation_id, quote_version)`。
- UQ：`(legal_entity_id, id)`。
- FK：`(legal_entity_id, id) → business_document(legal_entity_id, id)`。
- FK：`(legal_entity_id) → legal_entity(id)`。
- FK：`(legal_entity_id, invitation_id) → rfq_invitation(legal_entity_id, id)`。
- FK：`(legal_entity_id, source_attachment_id) → attachment(legal_entity_id, id)`。
- CHECK：`quote_version > 0`。
- 普通索引：`(legal_entity_id, invitation_id)`。
- 普通索引：`(legal_entity_id, source_attachment_id)`。
- 主表类型须与 `business_document.document_type` 匹配，状态使用通用登记的当前状态。

补充约束与执行规则：

- 截标前（状态 SEALED）：盲收登记仅保存 sealed_hash 与 sealed_at，严禁录入或向业务接口暴露报价金额明细；
- 集中开标解密（状态转 RECORDED）：截标后由开标事务核验 sealed_hash 未篡改，原子解密并写入明细行与 gross_amount，记录 unsealed_at；
- 提交一个报价版本后冻结；修订创建新的 quotation 主表和 quote_version，定标引用精确报价行，不覆盖旧报价。
- FK：`(currency) → currency(code)`；金额依币种 minor_units 校验舍入，禁止跨币种直接合计。

<a id="table-quotation_line"></a>

### `quotation_line`：报价明细表

所属模块：`procurement`；类型：单据明细表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | --------------- | ---- | ------ | ---------- |
| `id` | `uuid` | 否 | `无` | 主键；应用生成 UUID；PK |
| `legal_entity_id` | `uuid` | 否 | `无` | 所属法人；FK → `legal_entity.id` |
| `quotation_id` | `uuid` | 否 | `无` | 所属主表；FK → `quotation.id` |
| `revision` | `integer` | 否 | `1` | 所属内容版本 |
| `line_no` | `integer` | 否 | `无` | 当前内容版本内行号 |
| `rfq_line_id` | `uuid` | 否 | `无` | 对应询价行；FK → `rfq_line.id` |
| `quantity` | `numeric(20,6)` | 是 | `无` | 报价数量 |
| `unit_price` | `numeric(20,6)` | 是 | `无` | 未税单价 |
| `tax_rate` | `numeric(9,6)` | 否 | `无` | 税率，如 0.130000 |
| `net_amount` | `numeric(24,6)` | 否 | `0` | 未税行金额 |
| `tax_amount` | `numeric(24,6)` | 否 | `0` | 税额 |
| `gross_amount` | `numeric(24,6)` | 否 | `0` | 含税行金额 |
| `promised_date` | `date` | 否 | `无` | 承诺交期 |
| `created_at` | `timestamptz` | 否 | `CURRENT_TIMESTAMP` | 创建时间 |
| `purchase_type` | `varchar(32)` | 否 | `无` | CHECK 允许值：GOODS, SERVICE, LICENSE, SUBSCRIPTION |
| `measurement_basis` | `varchar(32)` | 否 | `无` | CHECK 允许值：QUANTITY, AMOUNT |
| `category_id` | `uuid` | 否 | `无` | 采购类别；FK → `category.id` |
| `acceptance_criteria` | `text` | 是 | `无` | 非实物成果/期间/里程碑标准 |
| `service_start` | `date` | 是 | `无` | 服务或订阅起日 |
| `service_end` | `date` | 是 | `无` | 服务或订阅止日 |
| `offered_scope` | `numeric(24,6)` | 否 | `无` | 供应商响应覆盖的来源需求范围，沿用 rfq_line 的范围单位 |

数据库约束与索引：

- PK：`id`。
- UQ：`(quotation_id, revision, line_no)`。
- UQ：`(quotation_id, revision, rfq_line_id)`。
- UQ：`(legal_entity_id, id)`。
- FK：`(legal_entity_id) → legal_entity(id)`。
- FK：`(legal_entity_id, quotation_id) → quotation(legal_entity_id, id)`。
- FK：`(legal_entity_id, rfq_line_id) → rfq_line(legal_entity_id, id)`。
- CHECK：`revision > 0`。
- CHECK：`line_no > 0`。
- CHECK：`quantity > 0`。
- CHECK：`unit_price >= 0`。
- CHECK：`tax_rate BETWEEN 0 AND 1`。
- CHECK：`gross_amount = net_amount + tax_amount`。
- 普通索引：`(legal_entity_id, quotation_id)`。
- 普通索引：`(legal_entity_id, rfq_line_id)`。

补充约束与执行规则：

- 复合外键 (quotation_id, revision) → document_revision(document_id, revision)。修改冻结版本时建立新行 ID，旧行保留供下游引用。
- 所引用询价行必须属于邀请的询价及发布版本；合计由后端计算。
- FK：`(legal_entity_id, category_id) → category(legal_entity_id, id)`。
- CHECK：`purchase_type <> 'GOODS' OR measurement_basis = 'QUANTITY'`；有 material_id 字段的实物行还要求 material_id IS NOT NULL。
- CHECK：`(measurement_basis = 'QUANTITY' AND quantity IS NOT NULL AND quantity > 0) OR (measurement_basis = 'AMOUNT' AND quantity IS NULL)`。
- CHECK：`(measurement_basis = 'QUANTITY' AND unit_price IS NOT NULL) OR (measurement_basis = 'AMOUNT' AND unit_price IS NULL)`。
- CHECK：`purchase_type = 'GOODS' OR acceptance_criteria IS NOT NULL`；CHECK：`(service_start IS NULL AND service_end IS NULL) OR (service_start IS NOT NULL AND service_end IS NOT NULL AND service_end >= service_start)`。
- CHECK：`offered_scope > 0`；不得超本轮 requested_scope，报价金额与需求范围分别记录。

<a id="table-award"></a>

### `award`：定标主表

所属模块：`procurement`；类型：单据主表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | --------------- | ---- | ------ | ---------- |
| `id` | `uuid` | 否 | `无` | 主键；沿用 business_document.id；FK → `business_document.id`；PK |
| `legal_entity_id` | `uuid` | 否 | `无` | 所属法人；FK → `legal_entity.id` |
| `rfq_id` | `uuid` | 否 | `无` | 对应询价；FK → `rfq.id` |
| `selection_reason` | `text` | 否 | `无` | 选择依据 |
| `comparison_rule_version` | `varchar(64)` | 否 | `无` | 比价规则版本 |
| `created_at` | `timestamptz` | 否 | `CURRENT_TIMESTAMP` | 创建时间 |

数据库约束与索引：

- PK：`id`。
- UQ：`(legal_entity_id, id)`。
- FK：`(legal_entity_id, id) → business_document(legal_entity_id, id)`。
- FK：`(legal_entity_id) → legal_entity(id)`。
- FK：`(legal_entity_id, rfq_id) → rfq(legal_entity_id, id)`。
- 普通索引：`(legal_entity_id, rfq_id)`。
- 主表类型须与 `business_document.document_type` 匹配，状态使用通用登记的当前状态。

<a id="table-award_line"></a>

### `award_line`：定标明细表

所属模块：`procurement`；类型：单据明细表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | --------------- | ---- | ------ | ---------- |
| `id` | `uuid` | 否 | `无` | 主键；应用生成 UUID；PK |
| `legal_entity_id` | `uuid` | 否 | `无` | 所属法人；FK → `legal_entity.id` |
| `award_id` | `uuid` | 否 | `无` | 所属主表；FK → `award.id` |
| `revision` | `integer` | 否 | `1` | 所属内容版本 |
| `line_no` | `integer` | 否 | `无` | 当前内容版本内行号 |
| `rfq_line_id` | `uuid` | 否 | `无` | 询价行；FK → `rfq_line.id` |
| `quotation_line_id` | `uuid` | 否 | `无` | 获选报价行；FK → `quotation_line.id` |
| `supplier_id` | `uuid` | 否 | `无` | 获选供应商；FK → `supplier.id` |
| `awarded_scope` | `numeric(24,6)` | 否 | `无` | 批准分配数量 |
| `reason` | `text` | 否 | `无` | 该行选择理由 |
| `created_at` | `timestamptz` | 否 | `CURRENT_TIMESTAMP` | 创建时间 |
| `purchase_type` | `varchar(32)` | 否 | `无` | CHECK 允许值：GOODS, SERVICE, LICENSE, SUBSCRIPTION |
| `measurement_basis` | `varchar(32)` | 否 | `无` | CHECK 允许值：QUANTITY, AMOUNT |
| `category_id` | `uuid` | 否 | `无` | 采购类别；FK → `category.id` |
| `acceptance_criteria` | `text` | 是 | `无` | 非实物成果/期间/里程碑标准 |
| `service_start` | `date` | 是 | `无` | 服务或订阅起日 |
| `service_end` | `date` | 是 | `无` | 服务或订阅止日 |

数据库约束与索引：

- PK：`id`。
- UQ：`(award_id, revision, line_no)`。
- UQ：`(award_id, revision, rfq_line_id)`。
- UQ：`(legal_entity_id, id)`。
- FK：`(legal_entity_id) → legal_entity(id)`。
- FK：`(legal_entity_id, award_id) → award(legal_entity_id, id)`。
- FK：`(legal_entity_id, rfq_line_id) → rfq_line(legal_entity_id, id)`。
- FK：`(legal_entity_id, quotation_line_id) → quotation_line(legal_entity_id, id)`。
- FK：`(legal_entity_id, supplier_id) → supplier(legal_entity_id, id)`。
- CHECK：`revision > 0`。
- CHECK：`line_no > 0`。
- CHECK：`awarded_scope > 0`。
- 普通索引：`(legal_entity_id, award_id)`。
- 普通索引：`(legal_entity_id, rfq_line_id)`。
- 普通索引：`(legal_entity_id, quotation_line_id)`。
- 普通索引：`(legal_entity_id, supplier_id)`。

补充约束与执行规则：

- 复合外键 (award_id, revision) → document_revision(document_id, revision)。修改冻结版本时建立新行 ID，旧行保留供下游引用。
- 当前首期每个定标版本同一询价行只选一家；报价行必须来自该询价行且供应商一致。
- FK：`(legal_entity_id, category_id) → category(legal_entity_id, id)`。
- CHECK：`purchase_type <> 'GOODS' OR measurement_basis = 'QUANTITY'`；有 material_id 字段的实物行还要求 material_id IS NOT NULL。
- CHECK：`purchase_type = 'GOODS' OR acceptance_criteria IS NOT NULL`；CHECK：`(service_start IS NULL AND service_end IS NULL) OR (service_start IS NOT NULL AND service_end IS NOT NULL AND service_end >= service_start)`。
- awarded_scope 使用来源稳定需求项范围单位；QUOTE 按数量报价或按金额响应均不虚构件数。订单各来源分配累计不得超该获批范围。

<a id="table-purchase_order"></a>

### `purchase_order`：采购订单主表

所属模块：`procurement`；类型：单据主表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | --------------- | ---- | ------ | ---------- |
| `id` | `uuid` | 否 | `无` | 主键；沿用 business_document.id；FK → `business_document.id`；PK |
| `legal_entity_id` | `uuid` | 否 | `无` | 所属法人；FK → `legal_entity.id` |
| `award_id` | `uuid` | 是 | `无` | 定标来源；FK → `award.id` |
| `supplier_id` | `uuid` | 否 | `无` | 唯一供应商；FK → `supplier.id` |
| `warehouse_id` | `uuid` | 是 | `无` | 唯一收货仓库；FK → `warehouse.id` |
| `supplier_name` | `varchar(200)` | 否 | `无` | 供应商名称快照 |
| `currency` | `char(3)` | 否 | `无` | 币种；FK → `currency.code` |
| `required_date` | `date` | 否 | `无` | 默认交期 |
| `net_amount` | `numeric(24,6)` | 否 | `0` | 未税合计 |
| `tax_amount` | `numeric(24,6)` | 否 | `0` | 税额合计 |
| `gross_amount` | `numeric(24,6)` | 否 | `0` | 含税合计 |
| `issued_at` | `timestamptz` | 是 | `无` | 正式发布时间 |
| `delivery_attachment_id` | `uuid` | 是 | `无` | 订单送达凭证；FK → `attachment.id` |
| `created_at` | `timestamptz` | 否 | `CURRENT_TIMESTAMP` | 创建时间 |
| `contract_id` | `uuid` | 是 | `无` | ；FK → `purchase_contract.id` |
| `contract_revision` | `integer` | 是 | `无` | 约束合同版本 |
| `origin` | `varchar(32)` | 否 | `无` | CHECK 允许值：AWARD, CONTRACT_FIXED, CONTRACT_RELEASE, EXCEPTION |
| `execution_eligibility` | `varchar(32)` | 否 | `BLOCKED` | CHECK 允许值：WAITING_CONTRACT, READY, BLOCKED |
| `approval_instance_id` | `uuid` | 是 | `无` | ；FK → `approval_instance.id` |
| `approval_policy_id` | `uuid` | 是 | `无` | ；FK → `business_policy_version.id` |
| `approval_route` | `varchar(32)` | 否 | `UNASSESSED` | CHECK 允许值：UNASSESSED, PENDING_QUALIFICATION, PENDING_APPROVAL, AUTO_APPROVED, MANUAL_APPROVED, CONTRACT_APPROVED |
| `generation_key` | `varchar(160)` | 是 | `无` | 来源文档/版本/派生分组去重键 |
| `source_fingerprint` | `varchar(128)` | 是 | `无` | 获批供应商/范围/价格/税率/交付/付款/预算等完整关键字段摘要 |
| `method_decision_id` | `uuid` | 否 | `无` | ；FK → `purchase_method_decision.id` |

数据库约束与索引：

- PK：`id`。
- UQ：`(legal_entity_id, id)`。
- FK：`(legal_entity_id, id) → business_document(legal_entity_id, id)`。
- FK：`(legal_entity_id) → legal_entity(id)`。
- FK：`(legal_entity_id, award_id) → award(legal_entity_id, id)`。
- FK：`(legal_entity_id, supplier_id) → supplier(legal_entity_id, id)`。
- FK：`(legal_entity_id, warehouse_id) → warehouse(legal_entity_id, id)`。
- FK：`(legal_entity_id, delivery_attachment_id) → attachment(legal_entity_id, id)`。
- CHECK：`gross_amount = net_amount + tax_amount`。
- 普通索引：`(legal_entity_id, award_id)`。
- 普通索引：`(legal_entity_id, supplier_id)`。
- 普通索引：`(legal_entity_id, warehouse_id)`。
- 普通索引：`(legal_entity_id, delivery_attachment_id)`。
- 主表类型须与 `business_document.document_type` 匹配，状态使用通用登记的当前状态。

补充约束与执行规则：

- 审批/交付/结算状态不重复存三个可任意编辑字段：主状态在 business_document，交付和结算由明细账本派生。
- FK：`(currency) → currency(code)`；金额依币种 minor_units 校验舍入，禁止跨币种直接合计。
- FK：`(legal_entity_id, contract_id) → purchase_contract(legal_entity_id, id)`。
- FK：`(legal_entity_id, approval_instance_id) → approval_instance(legal_entity_id, id)`。
- FK：`(legal_entity_id, approval_policy_id) → business_policy_version(legal_entity_id, id)`。
- UQ：`(legal_entity_id, generation_key)`（非空时）；CHECK：`(contract_id IS NULL) = (contract_revision IS NULL)`。FK：`(contract_id, contract_revision) → document_revision(document_id, revision)`。
- origin=AWARD 必须有 award_id；CONTRACT_FIXED/CONTRACT_RELEASE 必须有 contract_id；EXCEPTION 必须有已批准采购方式例外依据。合同供应商/币种/公司一致；仓库只对实物履约要求。预算归属全部由 order_budget_allocation 表达。
- FK：`(legal_entity_id, method_decision_id) → purchase_method_decision(legal_entity_id, id)`。
- CHECK：`origin <> 'AWARD' OR award_id IS NOT NULL`；CHECK：`origin NOT IN ('CONTRACT_FIXED','CONTRACT_RELEASE') OR contract_id IS NOT NULL`。
- CHECK：`approval_route NOT IN ('PENDING_APPROVAL','MANUAL_APPROVED','CONTRACT_APPROVED') OR approval_instance_id IS NOT NULL`；CHECK：`approval_route <> 'AUTO_APPROVED' OR (approval_policy_id IS NOT NULL AND source_fingerprint IS NOT NULL)`。
- 草稿初始 UNASSESSED；提交时原子建立审批实例再进入 PENDING_APPROVAL。约束触发器校验人工实例属于本订单当前版本；CONTRACT_APPROVED 引用同公司合同当前获批版本实例及其派生订单清单。自动核准保存不可变规则和来源快照。
- 自动核准仅获批 RFQ/RFP、无需合同、全关键条款无偏离、公司规则明确允许、供应商已准入启用且预算与来源有效。其余进入可见待办，不允许永久悬空草稿。

<a id="table-purchase_order_line"></a>

### `purchase_order_line`：采购订单明细表

所属模块：`procurement`；类型：单据明细表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | --------------- | ---- | ------ | ---------- |
| `id` | `uuid` | 否 | `无` | 主键；应用生成 UUID；PK |
| `legal_entity_id` | `uuid` | 否 | `无` | 所属法人；FK → `legal_entity.id` |
| `purchase_order_id` | `uuid` | 否 | `无` | 所属主表；FK → `purchase_order.id` |
| `revision` | `integer` | 否 | `1` | 所属内容版本 |
| `line_no` | `integer` | 否 | `无` | 当前内容版本内行号 |
| `award_line_id` | `uuid` | 是 | `无` | 定标来源行；FK → `award_line.id` |
| `material_id` | `uuid` | 是 | `无` | 物料；FK → `material.id` |
| `material_name` | `varchar(200)` | 否 | `无` | 物料名称快照 |
| `specification` | `text` | 否 | `无` | 规格快照 |
| `unit_id` | `uuid` | 是 | `无` | 基础单位；FK → `unit.id` |
| `unit_code` | `varchar(32)` | 是 | `无` | 单位编码快照 |
| `ordered_quantity` | `numeric(20,6)` | 是 | `无` | 获批订购数量 |
| `unit_price` | `numeric(20,6)` | 是 | `无` | 未税单价 |
| `tax_rate` | `numeric(9,6)` | 否 | `无` | 税率，如 0.130000 |
| `net_amount` | `numeric(24,6)` | 否 | `0` | 未税行金额 |
| `tax_amount` | `numeric(24,6)` | 否 | `0` | 税额 |
| `gross_amount` | `numeric(24,6)` | 否 | `0` | 含税行金额 |
| `required_date` | `date` | 否 | `无` | 该行交期 |
| `created_at` | `timestamptz` | 否 | `CURRENT_TIMESTAMP` | 创建时间 |
| `purchase_type` | `varchar(32)` | 否 | `无` | CHECK 允许值：GOODS, SERVICE, LICENSE, SUBSCRIPTION |
| `measurement_basis` | `varchar(32)` | 否 | `无` | CHECK 允许值：QUANTITY, AMOUNT |
| `category_id` | `uuid` | 否 | `无` | 采购类别；FK → `category.id` |
| `acceptance_criteria` | `text` | 是 | `无` | 非实物成果/期间/里程碑标准 |
| `service_start` | `date` | 是 | `无` | 服务或订阅起日 |
| `service_end` | `date` | 是 | `无` | 服务或订阅止日 |
| `contract_line_id` | `uuid` | 是 | `无` | ；FK → `purchase_contract_line.id` |
| `warehouse_id` | `uuid` | 是 | `无` | 该实物行收货仓库；FK → `warehouse.id` |
| `ordered_scope` | `numeric(24,6)` | 否 | `无` | 本订单履约范围：QUANTITY 等于 ordered_quantity；AMOUNT 等于订单币种 gross_amount |

数据库约束与索引：

- PK：`id`。
- UQ：`(purchase_order_id, revision, line_no)`。
- UQ：`(legal_entity_id, id)`。
- FK：`(legal_entity_id) → legal_entity(id)`。
- FK：`(legal_entity_id, purchase_order_id) → purchase_order(legal_entity_id, id)`。
- FK：`(legal_entity_id, award_line_id) → award_line(legal_entity_id, id)`。
- FK：`(legal_entity_id, material_id) → material(legal_entity_id, id)`。
- FK：`(legal_entity_id, unit_id) → unit(legal_entity_id, id)`。
- CHECK：`revision > 0`。
- CHECK：`line_no > 0`。
- CHECK：`ordered_quantity > 0`。
- CHECK：`unit_price >= 0`。
- CHECK：`tax_rate BETWEEN 0 AND 1`。
- CHECK：`gross_amount = net_amount + tax_amount`。
- 普通索引：`(legal_entity_id, purchase_order_id)`。
- 普通索引：`(legal_entity_id, award_line_id)`。
- 普通索引：`(legal_entity_id, material_id)`。
- 普通索引：`(legal_entity_id, unit_id)`。

补充约束与执行规则：

- 复合外键 (purchase_order_id, revision) → document_revision(document_id, revision)。修改冻结版本时建立新行 ID，旧行保留供下游引用。
- 批准数量和价格冻结；累计收货、取消、退货通过已生效明细聚合计算，事务锁本行防止超量。来源申请、定标、物料须相互一致。
- FK：`(legal_entity_id, category_id) → category(legal_entity_id, id)`。
- CHECK：`purchase_type <> 'GOODS' OR measurement_basis = 'QUANTITY'`；有 material_id 字段的实物行还要求 material_id IS NOT NULL。
- CHECK：`(measurement_basis = 'QUANTITY' AND ordered_quantity IS NOT NULL AND ordered_quantity > 0) OR (measurement_basis = 'AMOUNT' AND ordered_quantity IS NULL)`。
- 申请来源通过 order_source_allocation 建立；批准前必须有至少一条完整来源及对应预算分配。不同版本不能绕过同一 request_item 的范围锁。
- FK：`(legal_entity_id, contract_line_id) → purchase_contract_line(legal_entity_id, id)`。
- FK：`(legal_entity_id, warehouse_id) → warehouse(legal_entity_id, id)`。
- 合同订单的 contract_line_id 非空且属于主表指定合同及版本；无合同订单该字段空。GOODS 要求 warehouse_id 非空，其他类型为空。按合同/来源链建立复合 FK 或延迟约束触发器，不只校验单列存在。
- CHECK：`(measurement_basis = 'QUANTITY' AND unit_id IS NOT NULL AND unit_code IS NOT NULL AND unit_price IS NOT NULL) OR (measurement_basis = 'AMOUNT' AND unit_id IS NULL AND unit_code IS NULL AND unit_price IS NULL)`。
- CHECK：`purchase_type = 'GOODS' OR acceptance_criteria IS NOT NULL`；CHECK：`(service_start IS NULL AND service_end IS NULL) OR (service_start IS NOT NULL AND service_end IS NOT NULL AND service_end >= service_start)`。
- CHECK：`purchase_type <> 'GOODS' OR material_id IS NOT NULL`。
- CHECK：`ordered_scope > 0 AND ((measurement_basis = 'QUANTITY' AND ordered_quantity IS NOT NULL AND ordered_scope = ordered_quantity) OR (measurement_basis = 'AMOUNT' AND ordered_quantity IS NULL AND ordered_scope = gross_amount))`；金额型零价交付应改用可计数的 QUANTITY 里程碑而非零金额范围。

<a id="table-order_close_request"></a>

### `order_close_request`：订单余量关闭主表

所属模块：`procurement`；类型：单据主表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | --------------- | ---- | ------ | ---------- |
| `id` | `uuid` | 否 | `无` | 主键；沿用 business_document.id；FK → `business_document.id`；PK |
| `legal_entity_id` | `uuid` | 否 | `无` | 所属法人；FK → `legal_entity.id` |
| `purchase_order_id` | `uuid` | 否 | `无` | 订单；FK → `purchase_order.id` |
| `reason` | `text` | 否 | `无` | 取消原因 |
| `created_at` | `timestamptz` | 否 | `CURRENT_TIMESTAMP` | 创建时间 |

数据库约束与索引：

- PK：`id`。
- UQ：`(legal_entity_id, id)`。
- FK：`(legal_entity_id, id) → business_document(legal_entity_id, id)`。
- FK：`(legal_entity_id) → legal_entity(id)`。
- FK：`(legal_entity_id, purchase_order_id) → purchase_order(legal_entity_id, id)`。
- 普通索引：`(legal_entity_id, purchase_order_id)`。
- 主表类型须与 `business_document.document_type` 匹配，状态使用通用登记的当前状态。

<a id="table-order_close_line"></a>

### `order_close_line`：订单余量关闭明细表

所属模块：`procurement`；类型：单据明细表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | --------------- | ---- | ------ | ---------- |
| `id` | `uuid` | 否 | `无` | 主键；应用生成 UUID；PK |
| `legal_entity_id` | `uuid` | 否 | `无` | 所属法人；FK → `legal_entity.id` |
| `order_close_request_id` | `uuid` | 否 | `无` | 所属主表；FK → `order_close_request.id` |
| `revision` | `integer` | 否 | `1` | 所属内容版本 |
| `line_no` | `integer` | 否 | `无` | 当前内容版本内行号 |
| `order_line_id` | `uuid` | 否 | `无` | 订单行；FK → `purchase_order_line.id` |
| `cancel_scope` | `numeric(20,6)` | 否 | `无` | 申请取消的待收量 |
| `released_amount` | `numeric(24,6)` | 否 | `0` | 批准时计算的预算释放金额 |
| `created_at` | `timestamptz` | 否 | `CURRENT_TIMESTAMP` | 创建时间 |

数据库约束与索引：

- PK：`id`。
- UQ：`(order_close_request_id, revision, line_no)`。
- UQ：`(order_close_request_id, revision, order_line_id)`。
- UQ：`(legal_entity_id, id)`。
- FK：`(legal_entity_id) → legal_entity(id)`。
- FK：`(legal_entity_id, order_close_request_id) → order_close_request(legal_entity_id, id)`。
- FK：`(legal_entity_id, order_line_id) → purchase_order_line(legal_entity_id, id)`。
- CHECK：`revision > 0`。
- CHECK：`line_no > 0`。
- CHECK：`cancel_scope > 0`。
- CHECK：`released_amount >= 0`。
- 普通索引：`(legal_entity_id, order_close_request_id)`。
- 普通索引：`(legal_entity_id, order_line_id)`。

补充约束与执行规则：

- 复合外键 (order_close_request_id, revision) → document_revision(document_id, revision)。修改冻结版本时建立新行 ID，旧行保留供下游引用。
- 只有审批通过的关闭明细计入有效取消量；不得超当前待收量。
- cancel_scope 使用该订单行 measurement_basis 的范围单位；released_amount 为订单币种显示金额。按未履约比例逐个预算分配释放预算币余额，原子更新订单来源 released_scope，不跨币种直接求和。

<a id="table-purchase_request_item"></a>

### `purchase_request_item`：跨版本需求项

所属模块：`procurement`；类型：业务明细表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | --------------- | ---- | ------ | ---------- |
| `id` | `uuid` | 否 | `无` | 主键；应用生成 UUID |
| `legal_entity_id` | `uuid` | 否 | `无` | 所属公司；FK → `legal_entity.id` |
| `request_id` | `uuid` | 否 | `无` | 所属申请；FK → `purchase_request.id` |
| `scope_basis` | `varchar(32)` | 否 | `无` | CHECK 允许值：QUANTITY, AMOUNT |
| `scope_currency` | `char(3)` | 是 | `无` | AMOUNT 范围的申请币种；FK → `currency.code` |
| `unit_id` | `uuid` | 是 | `无` | QUANTITY 范围单位；FK → `unit.id` |
| `procurement_state` | `varchar(32)` | 否 | `ACTIVE` | CHECK 允许值：ACTIVE, REVISING, CLOSED |
| `lock_version` | `integer` | 否 | `1` | 跨版本来源竞争锁版本 |
| `created_at` | `timestamptz` | 否 | `CURRENT_TIMESTAMP` | 创建时间 |

数据库约束与索引：

- PK：`id`。
- UQ：`(legal_entity_id, id)`。
- FK：`(legal_entity_id) → legal_entity(id)`。
- FK：`(legal_entity_id, request_id) → purchase_request(legal_entity_id, id)`。
- FK：`(scope_currency) → currency(code)`。
- FK：`(legal_entity_id, unit_id) → unit(legal_entity_id, id)`。
- CHECK：`(scope_basis = 'QUANTITY' AND unit_id IS NOT NULL AND scope_currency IS NULL) OR (scope_basis = 'AMOUNT' AND scope_currency IS NOT NULL AND unit_id IS NULL)`。
- 单位/币种和范围身份不得随修订改变；改变标的身份则关闭未执行范围并新建需求项。

<a id="table-request_change_request"></a>

### `request_change_request`：已批准申请修订或终止

所属模块：`procurement`；类型：单据主表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | --------------- | ---- | ------ | ---------- |
| `id` | `uuid` | 否 | `无` | 主键；沿用 business_document.id；FK → `business_document.id` |
| `legal_entity_id` | `uuid` | 否 | `无` | 所属公司；FK → `legal_entity.id` |
| `request_id` | `uuid` | 否 | `无` | 申请；FK → `purchase_request.id` |
| `base_revision` | `integer` | 否 | `无` | 原有效批准版本 |
| `proposed_revision` | `integer` | 否 | `无` | 待批准内容版本；修订和终止均必须生成 |
| `change_type` | `varchar(32)` | 否 | `无` | CHECK 允许值：AMEND, TERMINATE |
| `submitted_by` | `uuid` | 否 | `无` | 提交人；FK → `app_user.id` |
| `approval_instance_id` | `uuid` | 是 | `无` | 审批实例；FK → `approval_instance.id` |
| `reason` | `text` | 否 | `无` | 修改或终止理由 |
| `applied_at` | `timestamptz` | 是 | `无` | 批准且原子应用时间 |
| `created_at` | `timestamptz` | 否 | `CURRENT_TIMESTAMP` | 创建时间 |

数据库约束与索引：

- PK：`id`。
- UQ：`(legal_entity_id, id)`。
- FK：`(legal_entity_id, id) → business_document(legal_entity_id, id)`。
- FK：`(legal_entity_id) → legal_entity(id)`。
- FK：`(legal_entity_id, request_id) → purchase_request(legal_entity_id, id)`。
- FK：`(legal_entity_id, submitted_by) → app_user(legal_entity_id, id)`。
- FK：`(legal_entity_id, approval_instance_id) → approval_instance(legal_entity_id, id)`。
- FK：`(request_id, base_revision) → document_revision(document_id, revision)`；FK：`(request_id, proposed_revision) → document_revision(document_id, revision)`。
- CHECK：`base_revision > 0 AND proposed_revision > base_revision`。
- 同一申请通过 purchase_request.active_change_id 唯一指向活跃提案；创建时锁申请行并由约束触发器禁止两个活跃提案，活跃状态来自 business_document。
- 禁止改写已下单范围。提案退回、拒绝或撤回不释放旧有效预占；批准时原子应用。
- 类型与 business_document.document_type 一致；内容版本冻结与审批使用通用机制。
- CHECK：`proposed_revision > base_revision`；AMEND 必有 proposed_revision，TERMINATE 同样生成体现关闭范围的新版本后才能批准应用；旧版本保留。

<a id="table-request_change_line"></a>

### `request_change_line`：申请修订影响范围

所属模块：`procurement`；类型：业务明细表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | --------------- | ---- | ------ | ---------- |
| `id` | `uuid` | 否 | `无` | 主键；应用生成 UUID |
| `legal_entity_id` | `uuid` | 否 | `无` | 所属公司；FK → `legal_entity.id` |
| `change_request_id` | `uuid` | 否 | `无` | ；FK → `request_change_request.id` |
| `request_item_id` | `uuid` | 否 | `无` | ；FK → `purchase_request_item.id` |
| `old_line_id` | `uuid` | 是 | `无` | 原批准明细；FK → `purchase_request_line.id` |
| `new_line_id` | `uuid` | 是 | `无` | 提案明细；FK → `purchase_request_line.id` |
| `affected_scope` | `numeric(24,6)` | 否 | `0` | 修改/终止的未下单范围单位 |
| `created_at` | `timestamptz` | 否 | `CURRENT_TIMESTAMP` | 创建时间 |

数据库约束与索引：

- PK：`id`。
- UQ：`(legal_entity_id, id)`。
- FK：`(legal_entity_id) → legal_entity(id)`。
- FK：`(legal_entity_id, change_request_id) → request_change_request(legal_entity_id, id)`。
- FK：`(legal_entity_id, request_item_id) → purchase_request_item(legal_entity_id, id)`。
- FK：`(legal_entity_id, old_line_id) → purchase_request_line(legal_entity_id, id)`。
- FK：`(legal_entity_id, new_line_id) → purchase_request_line(legal_entity_id, id)`。
- UQ：`(change_request_id, request_item_id)`。
- CHECK：`affected_scope > 0 AND num_nonnulls(old_line_id, new_line_id) >= 1`。
- 旧/新行须属于同一需求项及变更单指定的申请版本；数据库复合 FK 或延迟约束触发器验证来源链。

<a id="table-order_source_allocation"></a>

### `order_source_allocation`：订单申请范围分配

所属模块：`procurement`；类型：业务明细表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | --------------- | ---- | ------ | ---------- |
| `id` | `uuid` | 否 | `无` | 主键；应用生成 UUID |
| `legal_entity_id` | `uuid` | 否 | `无` | 所属公司；FK → `legal_entity.id` |
| `order_line_id` | `uuid` | 否 | `无` | ；FK → `purchase_order_line.id` |
| `request_line_id` | `uuid` | 否 | `无` | 准确批准版本的来源行；FK → `purchase_request_line.id` |
| `request_item_id` | `uuid` | 否 | `无` | 跨版本竞争锁；FK → `purchase_request_item.id` |
| `allocated_scope` | `numeric(24,6)` | 否 | `无` | 按需求项单位/申请币种计算的范围，不是订单成交金额 |
| `released_scope` | `numeric(24,6)` | 否 | `0` | 获批关闭或不补货退货释放的范围 |
| `status` | `varchar(32)` | 否 | `DRAFT` | CHECK 允许值：DRAFT, ACTIVE, CLOSED |
| `created_at` | `timestamptz` | 否 | `CURRENT_TIMESTAMP` | 创建时间 |

数据库约束与索引：

- PK：`id`。
- UQ：`(legal_entity_id, id)`。
- FK：`(legal_entity_id) → legal_entity(id)`。
- FK：`(legal_entity_id, order_line_id) → purchase_order_line(legal_entity_id, id)`。
- FK：`(legal_entity_id, request_line_id) → purchase_request_line(legal_entity_id, id)`。
- FK：`(legal_entity_id, request_item_id) → purchase_request_item(legal_entity_id, id)`。
- UQ：`(order_line_id, request_line_id)`。
- CHECK：`allocated_scope > 0 AND released_scope BETWEEN 0 AND allocated_scope`。
- 同一稳定需求项所有有效版本累计 allocated_scope-released_scope 不超当前 approved_scope；冻结期间禁止新占用，订单草稿不占量。
- 金额型服务用原批准范围金额作为范围单位，采购降价不自动增加可采购服务范围。
- QUANTITY 来源须同单位；金额型来源各自以申请范围币种记 allocated_scope，不能跨币种直接相加。订单验收/关闭使用订单币种 ordered_scope，恢复需求范围按各来源 allocated_scope × 本次关闭或不补货退货范围 / 订单 ordered_scope 比例分配，最后一笔吸收范围精度尾差；不得把成交金额当需求范围直接释放。

<a id="table-purchase_contract"></a>

### `purchase_contract`：采购合同

所属模块：`procurement`；类型：单据主表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | --------------- | ---- | ------ | ---------- |
| `id` | `uuid` | 否 | `无` | 主键；沿用 business_document.id；FK → `business_document.id` |
| `legal_entity_id` | `uuid` | 否 | `无` | 所属公司；FK → `legal_entity.id` |
| `supplier_id` | `uuid` | 否 | `无` | ；FK → `supplier.id` |
| `currency` | `char(3)` | 否 | `无` | 交易币；FK → `currency.code` |
| `contract_type` | `varchar(32)` | 否 | `无` | CHECK 允许值：STANDARD, FRAMEWORK, SERVICE |
| `order_mode` | `varchar(32)` | 否 | `无` | CHECK 允许值：FIXED, RELEASE |
| `limit_amount` | `numeric(24,6)` | 否 | `无` | 合同总额/上限 |
| `effective_from` | `date` | 否 | `无` | 生效日期 |
| `effective_to` | `date` | 是 | `无` | 终止日期 |
| `signed_at` | `timestamptz` | 是 | `无` | 双方签署事实时间 |
| `signed_attachment_id` | `uuid` | 是 | `无` | ；FK → `attachment.id` |
| `approval_instance_id` | `uuid` | 是 | `无` | ；FK → `approval_instance.id` |
| `created_at` | `timestamptz` | 否 | `CURRENT_TIMESTAMP` | 创建时间 |

数据库约束与索引：

- PK：`id`。
- UQ：`(legal_entity_id, id)`。
- FK：`(legal_entity_id, id) → business_document(legal_entity_id, id)`。
- FK：`(legal_entity_id) → legal_entity(id)`。
- FK：`(legal_entity_id, supplier_id) → supplier(legal_entity_id, id)`。
- FK：`(currency) → currency(code)`。
- FK：`(legal_entity_id, signed_attachment_id) → attachment(legal_entity_id, id)`。
- FK：`(legal_entity_id, approval_instance_id) → approval_instance(legal_entity_id, id)`。
- CHECK：`limit_amount >= 0 AND (effective_to IS NULL OR effective_to >= effective_from)`。
- 合同主状态在 business_document：DRAFT/IN_REVIEW/APPROVED/PENDING_EFFECTIVE/ACTIVE/CLOSED/RETURNED/REJECTED/TERMINATED；核准固定范围合同时派生订单及预算原子生效。
- 类型与 business_document.document_type 一致；内容版本冻结与审批使用通用机制。

<a id="table-purchase_contract_line"></a>

### `purchase_contract_line`：合同版本明细

所属模块：`procurement`；类型：业务明细表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | --------------- | ---- | ------ | ---------- |
| `id` | `uuid` | 否 | `无` | 主键；应用生成 UUID |
| `legal_entity_id` | `uuid` | 否 | `无` | 所属公司；FK → `legal_entity.id` |
| `contract_id` | `uuid` | 否 | `无` | ；FK → `purchase_contract.id` |
| `revision` | `integer` | 否 | `1` | 合同内容版本 |
| `line_no` | `integer` | 否 | `无` | 行号 |
| `description` | `text` | 否 | `无` | 约定采购范围 |
| `purchase_type` | `varchar(32)` | 否 | `无` | CHECK 允许值：GOODS, SERVICE, LICENSE, SUBSCRIPTION |
| `scope_basis` | `varchar(32)` | 否 | `无` | CHECK 允许值：QUANTITY, AMOUNT |
| `scope_limit` | `numeric(24,6)` | 否 | `无` | 范围单位上限 |
| `gross_limit` | `numeric(24,6)` | 否 | `无` | 币种金额上限 |
| `terms` | `jsonb` | 否 | `无` | 有 schema 的价格/交付/验收/支付条件快照 |
| `created_at` | `timestamptz` | 否 | `CURRENT_TIMESTAMP` | 创建时间 |

数据库约束与索引：

- PK：`id`。
- UQ：`(legal_entity_id, id)`。
- FK：`(legal_entity_id) → legal_entity(id)`。
- FK：`(legal_entity_id, contract_id) → purchase_contract(legal_entity_id, id)`。
- UQ：`(contract_id, revision, line_no)`。
- FK：`(contract_id, revision) → document_revision(document_id, revision)`。
- CHECK：`revision > 0 AND line_no > 0 AND scope_limit > 0 AND gross_limit >= 0`。
- 同一合同逻辑行各版本用 line_no 稳定识别；并发释放订单锁合同及该逻辑行，跨版本累计有效分配，不因改版重置上限。

<a id="table-acceptance"></a>

### `acceptance`：非实物验收单

所属模块：`procurement`；类型：单据主表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | --------------- | ---- | ------ | ---------- |
| `id` | `uuid` | 否 | `无` | 主键；沿用 business_document.id；FK → `business_document.id` |
| `legal_entity_id` | `uuid` | 否 | `无` | 所属公司；FK → `legal_entity.id` |
| `order_id` | `uuid` | 否 | `无` | ；FK → `purchase_order.id` |
| `submitted_by` | `uuid` | 否 | `无` | ；FK → `app_user.id` |
| `policy_version_id` | `uuid` | 否 | `无` | ；FK → `business_policy_version.id` |
| `approval_instance_id` | `uuid` | 是 | `无` | ；FK → `approval_instance.id` |
| `confirmed_by` | `uuid` | 是 | `无` | ；FK → `app_user.id` |
| `confirmed_at` | `timestamptz` | 是 | `无` | 实际确认时间 |
| `created_at` | `timestamptz` | 否 | `CURRENT_TIMESTAMP` | 创建时间 |

数据库约束与索引：

- PK：`id`。
- UQ：`(legal_entity_id, id)`。
- FK：`(legal_entity_id, id) → business_document(legal_entity_id, id)`。
- FK：`(legal_entity_id) → legal_entity(id)`。
- FK：`(legal_entity_id, order_id) → purchase_order(legal_entity_id, id)`。
- FK：`(legal_entity_id, submitted_by) → app_user(legal_entity_id, id)`。
- FK：`(legal_entity_id, policy_version_id) → business_policy_version(legal_entity_id, id)`。
- FK：`(legal_entity_id, approval_instance_id) → approval_instance(legal_entity_id, id)`。
- FK：`(legal_entity_id, confirmed_by) → app_user(legal_entity_id, id)`。
- 状态 DRAFT/IN_REVIEW/CONFIRMED/RETURNED/REJECTED/REVERSED 存通用登记；需审批时最终审批人与提交人分离。
- 类型与 business_document.document_type 一致；内容版本冻结与审批使用通用机制。

<a id="table-acceptance_line"></a>

### `acceptance_line`：非实物验收明细

所属模块：`procurement`；类型：业务明细表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | --------------- | ---- | ------ | ---------- |
| `id` | `uuid` | 否 | `无` | 主键；应用生成 UUID |
| `legal_entity_id` | `uuid` | 否 | `无` | 所属公司；FK → `legal_entity.id` |
| `acceptance_id` | `uuid` | 否 | `无` | ；FK → `acceptance.id` |
| `revision` | `integer` | 否 | `1` | 内容版本 |
| `line_no` | `integer` | 否 | `无` | 行号 |
| `order_line_id` | `uuid` | 否 | `无` | ；FK → `purchase_order_line.id` |
| `accepted_scope` | `numeric(24,6)` | 否 | `无` | 对应订单计量方式的验收范围 |
| `accepted_gross_amount` | `numeric(24,6)` | 否 | `无` | 订单币种验收含税额 |
| `period_start` | `date` | 是 | `无` | 服务期间起 |
| `period_end` | `date` | 是 | `无` | 服务期间止 |
| `milestone_key` | `varchar(100)` | 是 | `无` | 里程碑标识 |
| `proof_attachment_id` | `uuid` | 否 | `无` | ；FK → `attachment.id` |
| `created_at` | `timestamptz` | 否 | `CURRENT_TIMESTAMP` | 创建时间 |

数据库约束与索引：

- PK：`id`。
- UQ：`(legal_entity_id, id)`。
- FK：`(legal_entity_id) → legal_entity(id)`。
- FK：`(legal_entity_id, acceptance_id) → acceptance(legal_entity_id, id)`。
- FK：`(legal_entity_id, order_line_id) → purchase_order_line(legal_entity_id, id)`。
- FK：`(legal_entity_id, proof_attachment_id) → attachment(legal_entity_id, id)`。
- UQ：`(acceptance_id, revision, line_no)`。FK：`(acceptance_id, revision) → document_revision(document_id, revision)`。
- CHECK：`accepted_scope > 0 AND accepted_gross_amount >= 0 AND ((period_start IS NULL AND period_end IS NULL) OR (period_start IS NOT NULL AND period_end >= period_start))`。
- 仅非实物订单，来源必须属于主表 order_id；确认时锁订单行，累计范围/金额不可超单，期间/里程碑不得重复记账。
- CHECK：`(period_start IS NULL AND period_end IS NULL) OR (period_start IS NOT NULL AND period_end IS NOT NULL AND period_end >= period_start)`。

<a id="table-purchase_method_decision"></a>

### `purchase_method_decision`：采购方式决定

所属模块：`procurement`；类型：单据主表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | --------------- | ---- | ------ | ---------- |
| `id` | `uuid` | 否 | `无` | 主键；沿用 business_document.id；FK → `business_document.id` |
| `legal_entity_id` | `uuid` | 否 | `无` | 所属公司；FK → `legal_entity.id` |
| `request_id` | `uuid` | 否 | `无` | ；FK → `purchase_request.id` |
| `request_revision` | `integer` | 否 | `无` | 需求批准版本 |
| `method` | `varchar(32)` | 否 | `无` | CHECK 允许值：AGREEMENT, RFQ, RFP, TENDER, SINGLE_SOURCE |
| `is_exception` | `boolean` | 否 | `false` | 是否例外 |
| `reason` | `text` | 否 | `无` | 方式选择及例外理由 |
| `policy_version_id` | `uuid` | 否 | `无` | ；FK → `business_policy_version.id` |
| `approval_instance_id` | `uuid` | 是 | `无` | ；FK → `approval_instance.id` |
| `requires_approval` | `boolean` | 否 | `无` | 规则判断快照 |
| `decided_at` | `timestamptz` | 是 | `无` | 正式批准或明确免审批时刻 |
| `created_at` | `timestamptz` | 否 | `CURRENT_TIMESTAMP` | 创建时间 |
| `approved_supplier_count` | `integer` | 是 | `无` | 已批准例外允许的最低有效响应家数；非此类例外为空 |

数据库约束与索引：

- PK：`id`。
- UQ：`(legal_entity_id, id)`。
- FK：`(legal_entity_id, id) → business_document(legal_entity_id, id)`。
- FK：`(legal_entity_id) → legal_entity(id)`。
- FK：`(legal_entity_id, request_id) → purchase_request(legal_entity_id, id)`。
- FK：`(legal_entity_id, policy_version_id) → business_policy_version(legal_entity_id, id)`。
- FK：`(legal_entity_id, approval_instance_id) → approval_instance(legal_entity_id, id)`。
- FK：`(request_id, request_revision) → document_revision(document_id, revision)`。
- 状态 DRAFT/IN_REVIEW/APPROVED/RETURNED/REJECTED/CANCELLED。需审批时 approval_instance_id 必填，免审批必须有明确规则版本及审计；不默认通过。
- 类型与 business_document.document_type 一致；内容版本冻结与审批使用通用机制。
- CHECK：`approved_supplier_count IS NULL OR approved_supplier_count > 0`；CHECK：`NOT is_exception OR requires_approval`。例外决定仍用本表及同一审批入口，不另建例外审批单据体系。
- rfq.exception_document_id 可引用后续获批例外决定，但须 is_exception=true，覆盖该轮采购需求、版本及家数条件；原发布依据保留，不改写旧征集快照。

<a id="table-purchase_method_scope"></a>

### `purchase_method_scope`：方式决定范围

所属模块：`procurement`；类型：业务明细表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | --------------- | ---- | ------ | ---------- |
| `id` | `uuid` | 否 | `无` | 主键；应用生成 UUID |
| `legal_entity_id` | `uuid` | 否 | `无` | 所属公司；FK → `legal_entity.id` |
| `decision_id` | `uuid` | 否 | `无` | ；FK → `purchase_method_decision.id` |
| `request_line_id` | `uuid` | 否 | `无` | ；FK → `purchase_request_line.id` |
| `scope_amount` | `numeric(24,6)` | 否 | `无` | 该方式覆盖的需求范围单位 |
| `created_at` | `timestamptz` | 否 | `CURRENT_TIMESTAMP` | 创建时间 |

数据库约束与索引：

- PK：`id`。
- UQ：`(legal_entity_id, id)`。
- FK：`(legal_entity_id) → legal_entity(id)`。
- FK：`(legal_entity_id, decision_id) → purchase_method_decision(legal_entity_id, id)`。
- FK：`(legal_entity_id, request_line_id) → purchase_request_line(legal_entity_id, id)`。
- UQ：`(decision_id, request_line_id)`。CHECK：`scope_amount > 0`。
- 来源行属于决定的申请版本；同需求范围不能同时被冲突的有效方式覆盖；修订冻结时禁止继续发布。

## 8. 库存与仓储模块

<a id="table-receipt"></a>

### `receipt`：采购收货主表

所属模块：`inventory`；类型：单据主表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | --------------- | ---- | ------ | ---------- |
| `id` | `uuid` | 否 | `无` | 主键；沿用 business_document.id；FK → `business_document.id`；PK |
| `legal_entity_id` | `uuid` | 否 | `无` | 所属法人；FK → `legal_entity.id` |
| `purchase_order_id` | `uuid` | 否 | `无` | 订单；FK → `purchase_order.id` |
| `warehouse_id` | `uuid` | 否 | `无` | 收货仓库；FK → `warehouse.id` |
| `receipt_date` | `date` | 否 | `无` | 业务收货日期 |
| `received_by` | `uuid` | 否 | `无` | 收货人；FK → `app_user.id` |
| `posted_at` | `timestamptz` | 是 | `无` | 过账时间 |
| `created_at` | `timestamptz` | 否 | `CURRENT_TIMESTAMP` | 创建时间 |

数据库约束与索引：

- PK：`id`。
- UQ：`(legal_entity_id, id)`。
- FK：`(legal_entity_id, id) → business_document(legal_entity_id, id)`。
- FK：`(legal_entity_id) → legal_entity(id)`。
- FK：`(legal_entity_id, purchase_order_id) → purchase_order(legal_entity_id, id)`。
- FK：`(legal_entity_id, warehouse_id) → warehouse(legal_entity_id, id)`。
- FK：`(legal_entity_id, received_by) → app_user(legal_entity_id, id)`。
- 普通索引：`(legal_entity_id, purchase_order_id)`。
- 普通索引：`(legal_entity_id, warehouse_id)`。
- 普通索引：`(legal_entity_id, received_by)`。
- 主表类型须与 `business_document.document_type` 匹配，状态使用通用登记的当前状态。

<a id="table-receipt_line"></a>

### `receipt_line`：采购收货明细表

所属模块：`inventory`；类型：单据明细表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | --------------- | ---- | ------ | ---------- |
| `id` | `uuid` | 否 | `无` | 主键；应用生成 UUID；PK |
| `legal_entity_id` | `uuid` | 否 | `无` | 所属法人；FK → `legal_entity.id` |
| `receipt_id` | `uuid` | 否 | `无` | 所属主表；FK → `receipt.id` |
| `revision` | `integer` | 否 | `1` | 所属内容版本 |
| `line_no` | `integer` | 否 | `无` | 当前内容版本内行号 |
| `order_line_id` | `uuid` | 否 | `无` | 对应订单行；FK → `purchase_order_line.id` |
| `material_id` | `uuid` | 否 | `无` | 收货物料；FK → `material.id` |
| `received_quantity` | `numeric(20,6)` | 否 | `无` | 验收合格实收数量 |
| `budget_execution_amount` | `numeric(24,6)` | 否 | `0` | 订单口径转执行金额 |
| `created_at` | `timestamptz` | 否 | `CURRENT_TIMESTAMP` | 创建时间 |
| `arrival_line_id` | `uuid` | 否 | `无` | FK → `arrival_line.id` |

数据库约束与索引：

- PK：`id`。
- UQ：`(receipt_id, revision, line_no)`。
- UQ：`(legal_entity_id, id)`。
- FK：`(legal_entity_id) → legal_entity(id)`。
- FK：`(legal_entity_id, receipt_id) → receipt(legal_entity_id, id)`。
- FK：`(legal_entity_id, order_line_id) → purchase_order_line(legal_entity_id, id)`。
- FK：`(legal_entity_id, material_id) → material(legal_entity_id, id)`。
- CHECK：`revision > 0`。
- CHECK：`line_no > 0`。
- CHECK：`received_quantity > 0`。
- CHECK：`budget_execution_amount >= 0`。
- 普通索引：`(legal_entity_id, receipt_id)`。
- 普通索引：`(legal_entity_id, order_line_id)`。
- 普通索引：`(legal_entity_id, material_id)`。

补充约束与执行规则：

- 复合外键 (receipt_id, revision) → document_revision(document_id, revision)。修改冻结版本时建立新行 ID，旧行保留供下游引用。
- 须与订单仓库/物料一致；只统计 POSTED 收货。过账同事务写 stock_ledger 与 budget_ledger。

- 入库须来源到货订单/物料一致，锁 arrival_line 消耗 qualified_quantity 并增加 posted_quantity；与库存、预算、收货过账同事务。收货纠错按原行反向记录并恢复合格保管余额，不能抹掉已发生交接。

<a id="table-return_order"></a>

### `return_order`：采购退货主表

所属模块：`inventory`；类型：单据主表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | --------------- | ---- | ------ | ---------- |
| `id` | `uuid` | 否 | `无` | 主键；沿用 business_document.id；FK → `business_document.id`；PK |
| `legal_entity_id` | `uuid` | 否 | `无` | 所属法人；FK → `legal_entity.id` |
| `purchase_order_id` | `uuid` | 否 | `无` | 来源订单；FK → `purchase_order.id` |
| `warehouse_id` | `uuid` | 否 | `无` | 退货仓库；FK → `warehouse.id` |
| `return_date` | `date` | 否 | `无` | 退货日期 |
| `reason` | `text` | 否 | `无` | 退货原因 |
| `posted_at` | `timestamptz` | 是 | `无` | 过账时间 |
| `created_at` | `timestamptz` | 否 | `CURRENT_TIMESTAMP` | 创建时间 |
| `submitted_by` | `uuid` | 否 | `无` | FK → `app_user.id` |
| `discovered_by` | `uuid` | 否 | `无` | FK → `app_user.id` |
| `approval_instance_id` | `uuid` | 是 | `无` | FK → `approval_instance.id` |

数据库约束与索引：

- PK：`id`。
- UQ：`(legal_entity_id, id)`。
- FK：`(legal_entity_id, id) → business_document(legal_entity_id, id)`。
- FK：`(legal_entity_id) → legal_entity(id)`。
- FK：`(legal_entity_id, purchase_order_id) → purchase_order(legal_entity_id, id)`。
- FK：`(legal_entity_id, warehouse_id) → warehouse(legal_entity_id, id)`。
- 普通索引：`(legal_entity_id, purchase_order_id)`。
- 普通索引：`(legal_entity_id, warehouse_id)`。
- 主表类型须与 `business_document.document_type` 匹配，状态使用通用登记的当前状态。
- 退货提交人和实际发现人均保留，最终审批实例须属于本退货当前版本且最终审批用户不等于 submitted_by。

<a id="table-return_line"></a>

### `return_line`：采购退货明细表

所属模块：`inventory`；类型：单据明细表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | --------------- | ---- | ------ | ---------- |
| `id` | `uuid` | 否 | `无` | 主键；应用生成 UUID；PK |
| `legal_entity_id` | `uuid` | 否 | `无` | 所属法人；FK → `legal_entity.id` |
| `return_order_id` | `uuid` | 否 | `无` | 所属主表；FK → `return_order.id` |
| `revision` | `integer` | 否 | `1` | 所属内容版本 |
| `line_no` | `integer` | 否 | `无` | 当前内容版本内行号 |
| `receipt_line_id` | `uuid` | 否 | `无` | 原收货行；FK → `receipt_line.id` |
| `return_quantity` | `numeric(20,6)` | 否 | `无` | 不补货退货量 |
| `released_execution_amount` | `numeric(24,6)` | 否 | `0` | 冲减预算已执行金额 |
| `created_at` | `timestamptz` | 否 | `CURRENT_TIMESTAMP` | 创建时间 |

数据库约束与索引：

- PK：`id`。
- UQ：`(return_order_id, revision, line_no)`。
- UQ：`(legal_entity_id, id)`。
- FK：`(legal_entity_id) → legal_entity(id)`。
- FK：`(legal_entity_id, return_order_id) → return_order(legal_entity_id, id)`。
- FK：`(legal_entity_id, receipt_line_id) → receipt_line(legal_entity_id, id)`。
- CHECK：`revision > 0`。
- CHECK：`line_no > 0`。
- CHECK：`return_quantity > 0`。
- CHECK：`released_execution_amount >= 0`。
- 普通索引：`(legal_entity_id, return_order_id)`。
- 普通索引：`(legal_entity_id, receipt_line_id)`。

补充约束与执行规则：

- 复合外键 (return_order_id, revision) → document_revision(document_id, revision)。修改冻结版本时建立新行 ID，旧行保留供下游引用。
- 原收货行、订单和仓库一致；不能退已分配发票量；退货量以已过账且未冲销数量为准。

<a id="table-stock_issue"></a>

### `stock_issue`：库存领用主表

所属模块：`inventory`；类型：单据主表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | --------------- | ---- | ------ | ---------- |
| `id` | `uuid` | 否 | `无` | 主键；沿用 business_document.id；FK → `business_document.id`；PK |
| `legal_entity_id` | `uuid` | 否 | `无` | 所属法人；FK → `legal_entity.id` |
| `warehouse_id` | `uuid` | 否 | `无` | 出库仓库；FK → `warehouse.id` |
| `receiving_department_id` | `uuid` | 否 | `无` | 领用部门；FK → `department.id` |
| `recipient_id` | `uuid` | 否 | `无` | 领用人；FK → `app_user.id` |
| `issue_date` | `date` | 否 | `无` | 领用日期 |
| `purpose` | `text` | 否 | `无` | 领用用途 |
| `posted_at` | `timestamptz` | 是 | `无` | 过账时间 |
| `created_at` | `timestamptz` | 否 | `CURRENT_TIMESTAMP` | 创建时间 |

数据库约束与索引：

- PK：`id`。
- UQ：`(legal_entity_id, id)`。
- FK：`(legal_entity_id, id) → business_document(legal_entity_id, id)`。
- FK：`(legal_entity_id) → legal_entity(id)`。
- FK：`(legal_entity_id, warehouse_id) → warehouse(legal_entity_id, id)`。
- FK：`(legal_entity_id, receiving_department_id) → department(legal_entity_id, id)`。
- FK：`(legal_entity_id, recipient_id) → app_user(legal_entity_id, id)`。
- 普通索引：`(legal_entity_id, warehouse_id)`。
- 普通索引：`(legal_entity_id, receiving_department_id)`。
- 普通索引：`(legal_entity_id, recipient_id)`。
- 主表类型须与 `business_document.document_type` 匹配，状态使用通用登记的当前状态。

<a id="table-stock_issue_line"></a>

### `stock_issue_line`：库存领用明细表

所属模块：`inventory`；类型：单据明细表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | --------------- | ---- | ------ | ---------- |
| `id` | `uuid` | 否 | `无` | 主键；应用生成 UUID；PK |
| `legal_entity_id` | `uuid` | 否 | `无` | 所属法人；FK → `legal_entity.id` |
| `stock_issue_id` | `uuid` | 否 | `无` | 所属主表；FK → `stock_issue.id` |
| `revision` | `integer` | 否 | `1` | 所属内容版本 |
| `line_no` | `integer` | 否 | `无` | 当前内容版本内行号 |
| `material_id` | `uuid` | 否 | `无` | 领用物料；FK → `material.id` |
| `issue_quantity` | `numeric(20,6)` | 否 | `无` | 领用数量 |
| `created_at` | `timestamptz` | 否 | `CURRENT_TIMESTAMP` | 创建时间 |

数据库约束与索引：

- PK：`id`。
- UQ：`(stock_issue_id, revision, line_no)`。
- UQ：`(stock_issue_id, revision, material_id)`。
- UQ：`(legal_entity_id, id)`。
- FK：`(legal_entity_id) → legal_entity(id)`。
- FK：`(legal_entity_id, stock_issue_id) → stock_issue(legal_entity_id, id)`。
- FK：`(legal_entity_id, material_id) → material(legal_entity_id, id)`。
- CHECK：`revision > 0`。
- CHECK：`line_no > 0`。
- CHECK：`issue_quantity > 0`。
- 普通索引：`(legal_entity_id, stock_issue_id)`。
- 普通索引：`(legal_entity_id, material_id)`。

补充约束与执行规则：

- 复合外键 (stock_issue_id, revision) → document_revision(document_id, revision)。修改冻结版本时建立新行 ID，旧行保留供下游引用。

<a id="table-stock_adjustment"></a>

### `stock_adjustment`：库存调整主表

所属模块：`inventory`；类型：单据主表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | --------------- | ---- | ------ | ---------- |
| `id` | `uuid` | 否 | `无` | 主键；沿用 business_document.id；FK → `business_document.id`；PK |
| `legal_entity_id` | `uuid` | 否 | `无` | 所属法人；FK → `legal_entity.id` |
| `warehouse_id` | `uuid` | 否 | `无` | 仓库；FK → `warehouse.id` |
| `adjustment_type` | `varchar(32)` | 否 | `'COUNT'` | 允许值：OPENING, COUNT, CORRECTION |
| `business_date` | `date` | 否 | `无` | 业务日期 |
| `reason` | `text` | 否 | `无` | 调整依据 |
| `posted_at` | `timestamptz` | 是 | `无` | 过账时间 |
| `created_at` | `timestamptz` | 否 | `CURRENT_TIMESTAMP` | 创建时间 |

数据库约束与索引：

- PK：`id`。
- UQ：`(legal_entity_id, id)`。
- FK：`(legal_entity_id, id) → business_document(legal_entity_id, id)`。
- FK：`(legal_entity_id) → legal_entity(id)`。
- FK：`(legal_entity_id, warehouse_id) → warehouse(legal_entity_id, id)`。
- 普通索引：`(legal_entity_id, warehouse_id)`。
- 主表类型须与 `business_document.document_type` 匹配，状态使用通用登记的当前状态。

<a id="table-stock_adjustment_line"></a>

### `stock_adjustment_line`：库存调整明细表

所属模块：`inventory`；类型：单据明细表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | --------------- | ---- | ------ | ---------- |
| `id` | `uuid` | 否 | `无` | 主键；应用生成 UUID；PK |
| `legal_entity_id` | `uuid` | 否 | `无` | 所属法人；FK → `legal_entity.id` |
| `stock_adjustment_id` | `uuid` | 否 | `无` | 所属主表；FK → `stock_adjustment.id` |
| `revision` | `integer` | 否 | `1` | 所属内容版本 |
| `line_no` | `integer` | 否 | `无` | 当前内容版本内行号 |
| `material_id` | `uuid` | 否 | `无` | 物料；FK → `material.id` |
| `observed_quantity` | `numeric(20,6)` | 否 | `无` | 观察时账面库存 |
| `counted_quantity` | `numeric(20,6)` | 否 | `无` | 盘点/期初确认数量 |
| `delta_quantity` | `numeric(20,6)` | 否 | `无` | 盘点数量减观察数量 |
| `expected_balance_version` | `integer` | 否 | `无` | 观察时余额版本，未建余额为 0 |
| `created_at` | `timestamptz` | 否 | `CURRENT_TIMESTAMP` | 创建时间 |

数据库约束与索引：

- PK：`id`。
- UQ：`(stock_adjustment_id, revision, line_no)`。
- UQ：`(stock_adjustment_id, revision, material_id)`。
- UQ：`(legal_entity_id, id)`。
- FK：`(legal_entity_id) → legal_entity(id)`。
- FK：`(legal_entity_id, stock_adjustment_id) → stock_adjustment(legal_entity_id, id)`。
- FK：`(legal_entity_id, material_id) → material(legal_entity_id, id)`。
- CHECK：`revision > 0`。
- CHECK：`line_no > 0`。
- CHECK：`observed_quantity >= 0`。
- CHECK：`counted_quantity >= 0`。
- CHECK：`delta_quantity = counted_quantity - observed_quantity`。
- CHECK：`expected_balance_version >= 0`。
- 普通索引：`(legal_entity_id, stock_adjustment_id)`。
- 普通索引：`(legal_entity_id, material_id)`。

补充约束与执行规则：

- 复合外键 (stock_adjustment_id, revision) → document_revision(document_id, revision)。修改冻结版本时建立新行 ID，旧行保留供下游引用。
- 账面版本变化拒绝过账；OPENING 仅用于尚无库存流水的余额键。

<a id="table-stock_balance"></a>

### `stock_balance`：库存余额

所属模块：`inventory`；类型：汇总表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | --------------- | ---- | ------ | ---------- |
| `id` | `uuid` | 否 | `无` | 主键；应用生成 UUID；PK |
| `legal_entity_id` | `uuid` | 否 | `无` | 所属法人；FK → `legal_entity.id` |
| `warehouse_id` | `uuid` | 否 | `无` | 仓库；FK → `warehouse.id` |
| `material_id` | `uuid` | 否 | `无` | 物料；FK → `material.id` |
| `quantity` | `numeric(20,6)` | 否 | `0` | 当前数量 |
| `lock_version` | `integer` | 否 | `1` | 余额版本 |
| `updated_at` | `timestamptz` | 否 | `无` | 更新时间 |
| `created_at` | `timestamptz` | 否 | `CURRENT_TIMESTAMP` | 创建时间 |

数据库约束与索引：

- PK：`id`。
- UQ：`(legal_entity_id, warehouse_id, material_id)`。
- UQ：`(legal_entity_id, id)`。
- FK：`(legal_entity_id) → legal_entity(id)`。
- FK：`(legal_entity_id, warehouse_id) → warehouse(legal_entity_id, id)`。
- FK：`(legal_entity_id, material_id) → material(legal_entity_id, id)`。
- CHECK：`quantity >= 0`。
- CHECK：`lock_version > 0`。
- 普通索引：`(legal_entity_id, material_id)`。

补充约束与执行规则：

- 唯一余额键，所有增减持锁并同时写流水；不可直接通过通用编辑接口修改。

<a id="table-stock_ledger"></a>

### `stock_ledger`：库存流水

所属模块：`inventory`；类型：追加账本表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | --------------- | ---- | ------ | ---------- |
| `id` | `uuid` | 否 | `无` | 主键；应用生成 UUID；PK |
| `legal_entity_id` | `uuid` | 否 | `无` | 所属法人；FK → `legal_entity.id` |
| `warehouse_id` | `uuid` | 否 | `无` | 仓库；FK → `warehouse.id` |
| `material_id` | `uuid` | 否 | `无` | 物料；FK → `material.id` |
| `receipt_line_id` | `uuid` | 是 | `无` | 收货来源；FK → `receipt_line.id` |
| `return_line_id` | `uuid` | 是 | `无` | 退货来源；FK → `return_line.id` |
| `issue_line_id` | `uuid` | 是 | `无` | 领用来源；FK → `stock_issue_line.id` |
| `adjustment_line_id` | `uuid` | 是 | `无` | 盘点/期初来源；FK → `stock_adjustment_line.id` |
| `correction_request_id` | `uuid` | 是 | `无` | 冲销申请；FK → `correction_request.id` |
| `reversal_of_id` | `uuid` | 是 | `无` | 被冲销原流水；FK → `stock_ledger.id` |
| `posting_key` | `varchar(160)` | 否 | `无` | 按来源行与动作规范生成的记账键 |
| `entry_type` | `varchar(32)` | 否 | `'RECEIPT'` | 允许值：RECEIPT, RETURN, ISSUE, ADJUSTMENT, REVERSAL |
| `delta_quantity` | `numeric(20,6)` | 否 | `无` | 带正负号数量 |
| `balance_after` | `numeric(20,6)` | 否 | `无` | 过账后余额 |
| `business_date` | `date` | 否 | `无` | 业务日期 |
| `created_at` | `timestamptz` | 否 | `CURRENT_TIMESTAMP` | 创建时间 |

数据库约束与索引：

- PK：`id`。
- UQ：`(legal_entity_id, posting_key)`。
- UQ：`(reversal_of_id)`。
- UQ：`(legal_entity_id, id)`。
- FK：`(legal_entity_id) → legal_entity(id)`。
- FK：`(legal_entity_id, warehouse_id) → warehouse(legal_entity_id, id)`。
- FK：`(legal_entity_id, material_id) → material(legal_entity_id, id)`。
- FK：`(legal_entity_id, receipt_line_id) → receipt_line(legal_entity_id, id)`。
- FK：`(legal_entity_id, return_line_id) → return_line(legal_entity_id, id)`。
- FK：`(legal_entity_id, issue_line_id) → stock_issue_line(legal_entity_id, id)`。
- FK：`(legal_entity_id, adjustment_line_id) → stock_adjustment_line(legal_entity_id, id)`。
- FK：`(legal_entity_id, correction_request_id) → correction_request(legal_entity_id, id)`。
- FK：`(legal_entity_id, reversal_of_id) → stock_ledger(legal_entity_id, id)`。
- CHECK：`num_nonnulls(receipt_line_id, return_line_id, issue_line_id, adjustment_line_id) = 1`。
- CHECK：`delta_quantity <> 0`。
- CHECK：`balance_after >= 0`。
- CHECK：`(entry_type = 'REVERSAL') = (reversal_of_id IS NOT NULL AND correction_request_id IS NOT NULL)`。
- CHECK：`(reversal_of_id IS NULL) = (correction_request_id IS NULL)`。
- CHECK：`reversal_of_id IS NULL OR reversal_of_id <> id`。
- CHECK：`entry_type = 'REVERSAL' OR (entry_type = 'RECEIPT' AND receipt_line_id IS NOT NULL AND delta_quantity > 0) OR (entry_type = 'RETURN' AND return_line_id IS NOT NULL AND delta_quantity < 0) OR (entry_type = 'ISSUE' AND issue_line_id IS NOT NULL AND delta_quantity < 0) OR (entry_type = 'ADJUSTMENT' AND adjustment_line_id IS NOT NULL)`。
- 普通索引：`(warehouse_id, material_id, created_at, id)`。
- 普通索引：`(legal_entity_id, warehouse_id)`。
- 普通索引：`(legal_entity_id, material_id)`。
- 普通索引：`(legal_entity_id, receipt_line_id)`。
- 普通索引：`(legal_entity_id, return_line_id)`。
- 普通索引：`(legal_entity_id, issue_line_id)`。
- 普通索引：`(legal_entity_id, adjustment_line_id)`。
- 普通索引：`(legal_entity_id, correction_request_id)`。
- 普通索引：`(legal_entity_id, reversal_of_id)`。

补充约束与执行规则：

- 来源外键必须与 entry_type 一致；REVERSAL 沿用原来源键，数量反向。每条原流水首期最多一次完整冲销；每种非冲销来源行建部分唯一索引防止不同 posting_key 重复入账。禁止 UPDATE/DELETE。

## 9. 财务模块

<a id="table-budget_account"></a>

### `budget_account`：预算账户

所属模块：`finance`；类型：汇总表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | --------------- | ---- | ------ | ---------- |
| `id` | `uuid` | 否 | `无` | 主键；应用生成 UUID；PK |
| `legal_entity_id` | `uuid` | 否 | `无` | 所属法人；FK → `legal_entity.id` |
| `department_id` | `uuid` | 否 | `无` | 预算部门；FK → `department.id` |
| `period_start` | `date` | 否 | `无` | 期间起日 |
| `period_end` | `date` | 否 | `无` | 期间止日 |
| `subject_code` | `varchar(64)` | 否 | `无` | 预算科目编码 |
| `currency` | `char(3)` | 否 | `无` | 币种；FK → `currency.code` |
| `approved_amount` | `numeric(24,6)` | 否 | `0` | 批准额度 |
| `reserved_amount` | `numeric(24,6)` | 否 | `0` | 剩余占用 |
| `executed_amount` | `numeric(24,6)` | 否 | `0` | 已执行金额 |
| `status` | `varchar(32)` | 否 | `'OPEN'` | 允许值：OPEN, CLOSED |
| `lock_version` | `integer` | 否 | `1` | 余额版本 |
| `updated_at` | `timestamptz` | 否 | `无` | 更新时间 |
| `created_at` | `timestamptz` | 否 | `CURRENT_TIMESTAMP` | 创建时间 |
| `preencumbered_amount` | `numeric(24,6)` | 否 | `0` | 申请预占汇总 |

数据库约束与索引：

- PK：`id`。
- UQ：`(legal_entity_id, department_id, period_start, period_end, subject_code, currency)`。
- UQ：`(legal_entity_id, id)`。
- FK：`(legal_entity_id) → legal_entity(id)`。
- FK：`(legal_entity_id, department_id) → department(legal_entity_id, id)`。
- CHECK：`period_end >= period_start`。
- CHECK：`reserved_amount >= 0`。
- CHECK：`executed_amount >= 0`。
- CHECK：`approved_amount >= preencumbered_amount + reserved_amount + executed_amount`。
- CHECK：`lock_version > 0`。

补充约束与执行规则：

- 可用额查询计算，不另存一列。相同科目预算期间不得重叠，由配置服务在法人/部门范围串行校验；首期可统一年度期间。
- FK：`(currency) → currency(code)`；金额依币种 minor_units 校验舍入，禁止跨币种直接合计。
- CHECK：`preencumbered_amount >= 0`；可用额 = approved_amount − preencumbered_amount − reserved_amount − executed_amount。额度调减必须覆盖三项总和。

<a id="table-budget_adjustment"></a>

### `budget_adjustment`：预算调整申请

所属模块：`finance`；类型：单据主表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | --------------- | ---- | ------ | ---------- |
| `id` | `uuid` | 否 | `无` | 主键；沿用 business_document.id；FK → `business_document.id`；PK |
| `legal_entity_id` | `uuid` | 否 | `无` | 所属法人；FK → `legal_entity.id` |
| `budget_account_id` | `uuid` | 否 | `无` | 预算账户；FK → `budget_account.id` |
| `delta_amount` | `numeric(24,6)` | 否 | `0` | 带符号额度调整金额 |
| `reason` | `text` | 否 | `无` | 调整原因 |
| `expected_budget_version` | `integer` | 否 | `无` | 提交时预算版本 |
| `created_at` | `timestamptz` | 否 | `CURRENT_TIMESTAMP` | 创建时间 |

数据库约束与索引：

- PK：`id`。
- UQ：`(legal_entity_id, id)`。
- FK：`(legal_entity_id, id) → business_document(legal_entity_id, id)`。
- FK：`(legal_entity_id) → legal_entity(id)`。
- FK：`(legal_entity_id, budget_account_id) → budget_account(legal_entity_id, id)`。
- CHECK：`delta_amount <> 0`。
- 普通索引：`(legal_entity_id, budget_account_id)`。
- 主表类型须与 `business_document.document_type` 匹配，状态使用通用登记的当前状态。

补充约束与执行规则：

- 初始建账户额度为 0，正向调整经审批建立初始预算；额度变更均写流水。

<a id="table-budget_reservation"></a>

### `budget_reservation`：订单行预算占用

所属模块：`finance`；类型：余额明细表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | --------------- | ---- | ------ | ---------- |
| `id` | `uuid` | 否 | `无` | 主键；应用生成 UUID；PK |
| `legal_entity_id` | `uuid` | 否 | `无` | 所属法人；FK → `legal_entity.id` |
| `budget_account_id` | `uuid` | 否 | `无` | 预算账户；FK → `budget_account.id` |
| `initial_amount` | `numeric(24,6)` | 否 | `0` | 首次批准占用金额 |
| `remaining_amount` | `numeric(24,6)` | 否 | `0` | 未转执行或释放金额 |
| `lock_version` | `integer` | 否 | `1` | 版本 |
| `created_at` | `timestamptz` | 否 | `CURRENT_TIMESTAMP` | 创建时间 |
| `order_budget_allocation_id` | `uuid` | 否 | `无` | ；FK → `order_budget_allocation.id` |
| `current_amount` | `numeric(24,6)` | 否 | `0` | 当前有效承诺总额，批准变更后更新 |

数据库约束与索引：

- PK：`id`。
- UQ：`(legal_entity_id, id)`。
- FK：`(legal_entity_id) → legal_entity(id)`。
- FK：`(legal_entity_id, budget_account_id) → budget_account(legal_entity_id, id)`。
- CHECK：`initial_amount >= 0`。
- CHECK：`remaining_amount BETWEEN 0 AND current_amount`。
- CHECK：`lock_version > 0`。
- 普通索引：`(legal_entity_id, budget_account_id)`。

补充约束与执行规则：

- 每个订单预算分配一个占用，账户必须与分配相同；一个订单行可对应多个账户及来源。initial_amount 不改写，批准变更按流水调整 current_amount。
- FK：`(legal_entity_id, order_budget_allocation_id) → order_budget_allocation(legal_entity_id, id)`。
- UQ：`(order_budget_allocation_id)`。CHECK：`current_amount >= 0`。

<a id="table-budget_ledger"></a>

### `budget_ledger`：预算流水

所属模块：`finance`；类型：追加账本表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | --------------- | ---- | ------ | ---------- |
| `id` | `uuid` | 否 | `无` | 主键；应用生成 UUID；PK |
| `legal_entity_id` | `uuid` | 否 | `无` | 所属法人；FK → `legal_entity.id` |
| `budget_account_id` | `uuid` | 否 | `无` | 账户；FK → `budget_account.id` |
| `reservation_id` | `uuid` | 是 | `无` | 订单占用来源；FK → `budget_reservation.id` |
| `adjustment_id` | `uuid` | 是 | `无` | 额度调整来源；FK → `budget_adjustment.id` |
| `receipt_line_id` | `uuid` | 是 | `无` | 收货转执行来源；FK → `receipt_line.id` |
| `return_line_id` | `uuid` | 是 | `无` | 退货来源；FK → `return_line.id` |
| `close_line_id` | `uuid` | 是 | `无` | 取消余量来源；FK → `order_close_line.id` |
| `correction_request_id` | `uuid` | 是 | `无` | 冲销申请；FK → `correction_request.id` |
| `reversal_of_id` | `uuid` | 是 | `无` | 被冲销流水；FK → `budget_ledger.id` |
| `posting_key` | `varchar(160)` | 否 | `无` | 规范来源动作键 |
| `entry_type` | `varchar(32)` | 否 | `无` | 允许值：ADJUST, PRE_RESERVE, PRE_RELEASE, TRANSFER, RESERVE_CHANGE, EXECUTE, RELEASE, RETURN, REVERSAL |
| `quota_delta` | `numeric(24,6)` | 否 | `0` | 额度变化 |
| `reserved_delta` | `numeric(24,6)` | 否 | `0` | 占用变化 |
| `executed_delta` | `numeric(24,6)` | 否 | `0` | 已执行变化 |
| `business_date` | `date` | 否 | `无` | 发生日期 |
| `created_at` | `timestamptz` | 否 | `CURRENT_TIMESTAMP` | 创建时间 |
| `preencumbrance_id` | `uuid` | 是 | `无` | ；FK → `budget_preencumbrance.id` |
| `request_change_id` | `uuid` | 是 | `无` | ；FK → `request_change_request.id` |
| `source_document_id` | `uuid` | 否 | `无` | 批准、取消、关闭或纠错依据；FK → `business_document.id` |
| `source_revision` | `integer` | 否 | `无` | 动作依据内容版本 |
| `preencumbered_delta` | `numeric(24,6)` | 否 | `0` | 申请预占有符号变化 |
| `scope_delta` | `numeric(24,6)` | 否 | `0` | 本预占剩余范围变化；仅预占相关动作使用 |
| `acceptance_line_id` | `uuid` | 是 | `无` | 非实物执行依据；FK → `acceptance_line.id` |

数据库约束与索引：

- PK：`id`。
- UQ：`(legal_entity_id, posting_key)`。
- UQ：`(reversal_of_id)`。
- UQ：`(legal_entity_id, id)`。
- FK：`(legal_entity_id) → legal_entity(id)`。
- FK：`(legal_entity_id, budget_account_id) → budget_account(legal_entity_id, id)`。
- FK：`(legal_entity_id, reservation_id) → budget_reservation(legal_entity_id, id)`。
- FK：`(legal_entity_id, adjustment_id) → budget_adjustment(legal_entity_id, id)`。
- FK：`(legal_entity_id, receipt_line_id) → receipt_line(legal_entity_id, id)`。
- FK：`(legal_entity_id, return_line_id) → return_line(legal_entity_id, id)`。
- FK：`(legal_entity_id, close_line_id) → order_close_line(legal_entity_id, id)`。
- FK：`(legal_entity_id, correction_request_id) → correction_request(legal_entity_id, id)`。
- FK：`(legal_entity_id, reversal_of_id) → budget_ledger(legal_entity_id, id)`。
- 普通索引：`(budget_account_id, created_at, id)`。
- 普通索引：`(legal_entity_id, budget_account_id)`。
- 普通索引：`(legal_entity_id, reservation_id)`。
- 普通索引：`(legal_entity_id, adjustment_id)`。
- 普通索引：`(legal_entity_id, receipt_line_id)`。
- 普通索引：`(legal_entity_id, return_line_id)`。
- 普通索引：`(legal_entity_id, close_line_id)`。
- 普通索引：`(legal_entity_id, correction_request_id)`。
- 普通索引：`(legal_entity_id, reversal_of_id)`。

补充约束与执行规则：

- CHECK：`entry_type <> 'ADJUST' OR adjustment_id IS NOT NULL`；ADJUST 的 source_document_id 必须等于 adjustment_id，来源版本已批准且账户一致，由约束触发器检查。
- 零金额订单行可创建零额 reservation，但不写零金额预算流水；金额为零不影响真实库存收货流水。
- FK：`(legal_entity_id, preencumbrance_id) → budget_preencumbrance(legal_entity_id, id)`。
- FK：`(legal_entity_id, request_change_id) → request_change_request(legal_entity_id, id)`。
- FK：`(legal_entity_id, source_document_id) → business_document(legal_entity_id, id)`。
- FK：`(source_document_id, source_revision) → document_revision(document_id, revision)`。source 文档和账户公司一致。
- CHECK：quota_delta、preencumbered_delta、reserved_delta、executed_delta、scope_delta 至少一项非零。
- CHECK 按 entry_type 生成：ADJUST 仅 quota_delta 非零；PRE_RESERVE 要求预占来源、preencumbered_delta>=0、scope_delta>0；PRE_RELEASE 要求预占来源、preencumbered_delta<=0、scope_delta<0；TRANSFER 要求预占及 reservation 来源、preencumbered_delta<=0、scope_delta<0、reserved_delta>=0；这三种动作 quota/executed 均为0，前两种 reserved 为0。
- RESERVE_CHANGE 仅 reserved_delta 非零；EXECUTE 要求 reserved_delta<0 且 executed_delta=-reserved_delta；RELEASE 仅 reserved_delta<0；RETURN 仅 executed_delta<0；这些动作要求 reservation，preencumbered_delta/scope_delta/quota_delta 均0。REVERSAL 须有 reversal_of_id 及 correction_request_id，所有 delta 精确取原流水反号，账户一致且一对一。
- TRANSFER 在同一流水行记录释放的对应预占与实际承诺，不要求两者相等；差额重算账户可用额，预算不足全部回滚。posted key=动作/来源版本/分配/事件序号，以同公司 UQ 防重复。零价格仍可用非零 scope_delta 记录消费范围。
- FK：`(legal_entity_id, acceptance_line_id) → acceptance_line(legal_entity_id, id)`。
- EXECUTE 必须 receipt_line_id / acceptance_line_id 恰好一个非空；RETURN 引用 return_line_id，RELEASE 引用批准关闭的 close_line_id；其余记账来源按动作严格校验。
- CHECK：`entry_type IN ('ADJUST','PRE_RESERVE','PRE_RELEASE','TRANSFER','RESERVE_CHANGE','EXECUTE','RELEASE','RETURN','REVERSAL')`。
- CHECK：`(entry_type IN ('PRE_RESERVE','PRE_RELEASE','TRANSFER') AND preencumbrance_id IS NOT NULL) OR (entry_type NOT IN ('PRE_RESERVE','PRE_RELEASE','TRANSFER') AND preencumbrance_id IS NULL) OR entry_type = 'REVERSAL'`。同账户和来源链须由真实 FK/约束触发器及事务校验。

<a id="table-invoice"></a>

### `invoice`：发票主表

所属模块：`finance`；类型：单据主表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | --------------- | ---- | ------ | ---------- |
| `id` | `uuid` | 否 | `无` | 主键；沿用 business_document.id；FK → `business_document.id`；PK |
| `legal_entity_id` | `uuid` | 否 | `无` | 所属法人；FK → `legal_entity.id` |
| `supplier_id` | `uuid` | 否 | `无` | 开票供应商；FK → `supplier.id` |
| `invoice_type` | `varchar(32)` | 否 | `无` | 发票标识类型 |
| `invoice_identifier` | `varchar(100)` | 否 | `无` | 规范化完整票据标识 |
| `invoice_date` | `date` | 否 | `无` | 开票日期 |
| `currency` | `char(3)` | 否 | `无` | 币种；FK → `currency.code` |
| `net_amount` | `numeric(24,6)` | 否 | `0` | 未税合计 |
| `tax_amount` | `numeric(24,6)` | 否 | `0` | 税额合计 |
| `gross_amount` | `numeric(24,6)` | 否 | `0` | 含税应付 |
| `source_attachment_id` | `uuid` | 否 | `无` | 发票凭证；FK → `attachment.id` |
| `match_status` | `varchar(32)` | 否 | `'NOT_RUN'` | 允许值：NOT_RUN, PASSED, FAILED, STALE |
| `confirmed_at` | `timestamptz` | 是 | `无` | 复核确认时间 |
| `created_at` | `timestamptz` | 否 | `CURRENT_TIMESTAMP` | 创建时间 |

数据库约束与索引：

- PK：`id`。
- UQ：`(legal_entity_id, supplier_id, invoice_type, invoice_identifier)`。
- UQ：`(legal_entity_id, id)`。
- FK：`(legal_entity_id, id) → business_document(legal_entity_id, id)`。
- FK：`(legal_entity_id) → legal_entity(id)`。
- FK：`(legal_entity_id, supplier_id) → supplier(legal_entity_id, id)`。
- FK：`(legal_entity_id, source_attachment_id) → attachment(legal_entity_id, id)`。
- CHECK：`gross_amount = net_amount + tax_amount`。
- CHECK：`gross_amount >= 0`。
- 普通索引：`(legal_entity_id, source_attachment_id)`。
- 主表类型须与 `business_document.document_type` 匹配，状态使用通用登记的当前状态。

补充约束与执行规则：

- 作废保留唯一票据标识；同票录入纠错创建 document_revision，不复制新的 invoice 主表。每个旧版本的金额以冻结 snapshot 为准。
- FK：`(currency) → currency(code)`；金额依币种 minor_units 校验舍入，禁止跨币种直接合计。

<a id="table-invoice_line"></a>

### `invoice_line`：发票明细表

所属模块：`finance`；类型：单据明细表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | --------------- | ---- | ------ | ---------- |
| `id` | `uuid` | 否 | `无` | 主键；应用生成 UUID；PK |
| `legal_entity_id` | `uuid` | 否 | `无` | 所属法人；FK → `legal_entity.id` |
| `invoice_id` | `uuid` | 否 | `无` | 所属主表；FK → `invoice.id` |
| `revision` | `integer` | 否 | `1` | 所属内容版本 |
| `line_no` | `integer` | 否 | `无` | 当前内容版本内行号 |
| `order_line_id` | `uuid` | 是 | `无` | 明确对应订单行；FK → `purchase_order_line.id` |
| `quantity` | `numeric(20,6)` | 是 | `无` | 发票数量 |
| `unit_price` | `numeric(20,6)` | 是 | `无` | 未税单价 |
| `tax_rate` | `numeric(9,6)` | 否 | `无` | 税率，如 0.130000 |
| `net_amount` | `numeric(24,6)` | 否 | `0` | 未税行金额 |
| `tax_amount` | `numeric(24,6)` | 否 | `0` | 税额 |
| `gross_amount` | `numeric(24,6)` | 否 | `0` | 含税行金额 |
| `created_at` | `timestamptz` | 否 | `CURRENT_TIMESTAMP` | 创建时间 |
| `purchase_type` | `varchar(32)` | 否 | `无` | CHECK 允许值：GOODS, SERVICE, LICENSE, SUBSCRIPTION |
| `measurement_basis` | `varchar(32)` | 否 | `无` | CHECK 允许值：QUANTITY, AMOUNT |
| `category_id` | `uuid` | 否 | `无` | 采购类别；FK → `category.id` |
| `acceptance_criteria` | `text` | 是 | `无` | 非实物成果/期间/里程碑标准 |
| `service_start` | `date` | 是 | `无` | 服务或订阅起日 |
| `service_end` | `date` | 是 | `无` | 服务或订阅止日 |

数据库约束与索引：

- PK：`id`。
- UQ：`(invoice_id, revision, line_no)`。
- UQ：`(legal_entity_id, id)`。
- FK：`(legal_entity_id) → legal_entity(id)`。
- FK：`(legal_entity_id, invoice_id) → invoice(legal_entity_id, id)`。
- FK：`(legal_entity_id, order_line_id) → purchase_order_line(legal_entity_id, id)`。
- CHECK：`revision > 0`。
- CHECK：`line_no > 0`。
- CHECK：`quantity > 0`。
- CHECK：`unit_price >= 0`。
- CHECK：`tax_rate BETWEEN 0 AND 1`。
- CHECK：`gross_amount = net_amount + tax_amount`。
- 普通索引：`(legal_entity_id, invoice_id)`。
- 普通索引：`(legal_entity_id, order_line_id)`。

补充约束与执行规则：

- 复合外键 (invoice_id, revision) → document_revision(document_id, revision)。修改冻结版本时建立新行 ID，旧行保留供下游引用。
- 申请匹配前须确定订单来源，无法关联保持录入异常草稿；冻结行不可覆盖，新版本新行 ID。
- order_line_id 仅草稿可空；提交匹配或复核前必须非空。
- FK：`(legal_entity_id, category_id) → category(legal_entity_id, id)`。
- CHECK：`purchase_type <> 'GOODS' OR measurement_basis = 'QUANTITY'`；有 material_id 字段的实物行还要求 material_id IS NOT NULL。
- CHECK：`(measurement_basis = 'QUANTITY' AND quantity IS NOT NULL AND quantity > 0) OR (measurement_basis = 'AMOUNT' AND quantity IS NULL)`。
- CHECK：`(measurement_basis = 'QUANTITY' AND unit_price IS NOT NULL) OR (measurement_basis = 'AMOUNT' AND unit_price IS NULL)`。
- CHECK：`purchase_type = 'GOODS' OR acceptance_criteria IS NOT NULL`；CHECK：`(service_start IS NULL AND service_end IS NULL) OR (service_start IS NOT NULL AND service_end IS NOT NULL AND service_end >= service_start)`。

<a id="table-invoice_match_run"></a>

### `invoice_match_run`：发票匹配批次

所属模块：`finance`；类型：分析快照表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | --------------- | ---- | ------ | ---------- |
| `id` | `uuid` | 否 | `无` | 主键；应用生成 UUID；PK |
| `legal_entity_id` | `uuid` | 否 | `无` | 所属法人；FK → `legal_entity.id` |
| `invoice_id` | `uuid` | 否 | `无` | 发票；FK → `invoice.id` |
| `invoice_revision` | `integer` | 否 | `无` | 发票内容版本 |
| `invoice_lock_version` | `integer` | 否 | `无` | 试算时编辑版本 |
| `policy_version_id` | `uuid` | 否 | `无` | 匹配规则版本；FK → `business_policy_version.id` |
| `result` | `varchar(32)` | 否 | `'FAILED'` | 允许值：PASSED, FAILED |
| `source_versions` | `jsonb` | 否 | `'{}'::jsonb` | 订单/收货/退货版本 |
| `differences` | `jsonb` | 否 | `'[]'::jsonb` | 逐字段差异数组 |
| `proposed_allocations` | `jsonb` | 否 | `'[]'::jsonb` | 建议分配数组 |
| `created_at` | `timestamptz` | 否 | `CURRENT_TIMESTAMP` | 创建时间 |

数据库约束与索引：

- PK：`id`。
- UQ：`(legal_entity_id, id)`。
- FK：`(legal_entity_id) → legal_entity(id)`。
- FK：`(legal_entity_id, invoice_id) → invoice(legal_entity_id, id)`。
- FK：`(legal_entity_id, policy_version_id) → business_policy_version(legal_entity_id, id)`。
- 普通索引：`(legal_entity_id, invoice_id)`。
- 普通索引：`(legal_entity_id, policy_version_id)`。

补充约束与执行规则：

- 复合外键 (invoice_id, invoice_revision) → document_revision(document_id, revision)。试算不占数量，submit 重新匹配后建立真实分配。

<a id="table-invoice_allocation"></a>

### `invoice_allocation`：发票收货分配

所属模块：`finance`；类型：分配明细表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | --------------- | ---- | ------ | ---------- |
| `id` | `uuid` | 否 | `无` | 主键；应用生成 UUID；PK |
| `legal_entity_id` | `uuid` | 否 | `无` | 所属法人；FK → `legal_entity.id` |
| `invoice_line_id` | `uuid` | 否 | `无` | 准确发票版本明细；FK → `invoice_line.id` |
| `receipt_line_id` | `uuid` | 是 | `无` | 收货来源行；FK → `receipt_line.id` |
| `match_run_id` | `uuid` | 否 | `无` | 建立分配的核对批次；FK → `invoice_match_run.id` |
| `allocated_scope` | `numeric(24,6)` | 否 | `无` | 分配数量 |
| `allocated_gross_amount` | `numeric(24,6)` | 否 | `0` | 分配发票含税额 |
| `status` | `varchar(32)` | 否 | `'HELD'` | 允许值：HELD, ACTIVE, RELEASED |
| `released_at` | `timestamptz` | 是 | `无` | 释放时间 |
| `created_at` | `timestamptz` | 否 | `CURRENT_TIMESTAMP` | 创建时间 |
| `acceptance_line_id` | `uuid` | 是 | `无` | 非实物履约来源；FK → `acceptance_line.id` |

数据库约束与索引：

- PK：`id`。
- UQ：`(legal_entity_id, id)`。
- FK：`(legal_entity_id) → legal_entity(id)`。
- FK：`(legal_entity_id, invoice_line_id) → invoice_line(legal_entity_id, id)`。
- FK：`(legal_entity_id, receipt_line_id) → receipt_line(legal_entity_id, id)`。
- FK：`(legal_entity_id, match_run_id) → invoice_match_run(legal_entity_id, id)`。
- CHECK：`allocated_scope > 0`。
- CHECK：`allocated_gross_amount >= 0`。
- 普通索引：`(legal_entity_id, invoice_line_id)`。
- 普通索引：`(legal_entity_id, receipt_line_id)`。
- 普通索引：`(legal_entity_id, match_run_id)`。

补充约束与执行规则：

- 部分唯一索引 (invoice_line_id, receipt_line_id) WHERE status IN (HELD, ACTIVE)。未释放分配之和不得超净收货，此跨行规则需锁收货行后校验，不用单行 CHECK 假装保证。发票各行分配金额之和必须与当前版本一致。
- FK：`(legal_entity_id, acceptance_line_id) → acceptance_line(legal_entity_id, id)`。
- CHECK：`num_nonnulls(receipt_line_id, acceptance_line_id) = 1`；非实物按已确认范围和金额占用，来源订单行必须等于 invoice_line.order_line_id。两种履约来源分别建立同状态 HELD/ACTIVE 活跃配对唯一索引。
- allocated_scope 使用订单行计量方式，金额型服务使用订单币种范围金额；不能用 NULL 数量乘单价推算验收金额。匹配数量/金额和账本汇总同步使用 allocated_scope。

<a id="table-payment_record"></a>

### `payment_record`：付款登记主表

所属模块：`finance`；类型：单据主表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | --------------- | ---- | ------ | ---------- |
| `id` | `uuid` | 否 | `无` | 主键；沿用 business_document.id；FK → `business_document.id`；PK |
| `legal_entity_id` | `uuid` | 否 | `无` | 所属法人；FK → `legal_entity.id` |
| `supplier_id` | `uuid` | 是 | `无` | 收款供应商；FK → `supplier.id` |
| `paid_date` | `date` | 否 | `无` | 实际付款日期 |
| `currency` | `char(3)` | 否 | `无` | 币种；FK → `currency.code` |
| `amount` | `numeric(24,6)` | 否 | `0` | 实际付款金额 |
| `payment_channel` | `varchar(32)` | 否 | `无` | 付款渠道 |
| `external_reference` | `varchar(128)` | 否 | `无` | 规范化外部流水号 |
| `proof_attachment_id` | `uuid` | 否 | `无` | 付款凭证；FK → `attachment.id` |
| `confirmed_at` | `timestamptz` | 是 | `无` | 复核确认时间 |
| `created_at` | `timestamptz` | 否 | `CURRENT_TIMESTAMP` | 创建时间 |
| `direction` | `varchar(32)` | 否 | `无` | CHECK 允许值：OUTGOING, INCOMING |
| `payer_account_key` | `varchar(160)` | 否 | `无` |  |
| `counterparty_name` | `text` | 否 | `无` |  |
| `recorded_by` | `uuid` | 否 | `无` | FK → `app_user.id` |
| `reviewed_by` | `uuid` | 是 | `无` | FK → `app_user.id` |
| `refund_of_id` | `uuid` | 是 | `无` | FK → `payment_record.id` |

数据库约束与索引：

- PK：`id`。
- UQ：`(legal_entity_id, payer_account_key, payment_channel, external_reference)`。
- UQ：`(legal_entity_id, id)`。
- FK：`(legal_entity_id, id) → business_document(legal_entity_id, id)`。
- FK：`(legal_entity_id) → legal_entity(id)`。
- FK：`(legal_entity_id, supplier_id) → supplier(legal_entity_id, id)`。
- FK：`(legal_entity_id, proof_attachment_id) → attachment(legal_entity_id, id)`。
- CHECK：`amount > 0`。
- 普通索引：`(legal_entity_id, supplier_id)`。
- 普通索引：`(legal_entity_id, proof_attachment_id)`。
- 主表类型须与 `business_document.document_type` 匹配，状态使用通用登记的当前状态。

补充约束与执行规则：

- 实际出款原件与流水号保留；复核只可确认、补正或转待核实。更正通过 correction_request.original_document_id 及 original_revision 关联原付款登记，生成有审批依据的新版本及更正关系，调整分配但不抹去资金事实；未知去向持续冻结，银行实际退款使用独立凭证。
- FK：`(currency) → currency(code)`；金额依币种 minor_units 校验舍入，禁止跨币种直接合计。

- CHECK：reviewed_by IS NULL OR reviewed_by <> recorded_by；退款是 INCOMING 新事实并引用 refund_of_id，不冲销真实出款。未匹配供应商可空但对方名称和凭据必存；确认结算必须匹配主体。INCOMING 的完整贷项自动结算仍属 P4，当前仅记录、独立核实并保留未决阻断。


<a id="table-payment_allocation"></a>

### `payment_allocation`：实付执行分配

所属模块：`finance`；类型：业务记录表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | ---- | ---- | ---- | ---- |
| `id` | `uuid` | 否 | `无` | 主键 |
| `legal_entity_id` | `uuid` | 否 | `无` | FK → `legal_entity.id` |
| `payment_record_id` | `uuid` | 否 | `无` | FK → `payment_record.id` |
| `revision` | `integer` | 否 | `无` |  |
| `line_no` | `integer` | 否 | `无` |  |
| `execution_id` | `uuid` | 否 | `无` | FK → `payment_execution.id` |
| `allocated_amount` | `numeric(24,6)` | 否 | `无` |  |
| `status` | `varchar(32)` | 否 | `无` | CHECK 允许值：HELD, ACTIVE, RELEASED |
| `released_at` | `timestamptz` | 是 | `无` |  |
| `created_at` | `timestamptz` | 否 | `无` | 创建时间；默认 CURRENT_TIMESTAMP |

数据库约束与执行规则：

- PK：`id`；UQ：`(legal_entity_id, id)`。所有法人范围引用按 1 节生成同公司复合 FK；每个 FK 的来源列建立索引。
- UQ：(payment_record_id, revision, line_no)；CHECK：allocated_amount > 0；FK：`(payment_record_id, revision) → document_revision(document_id, revision)`。
- 来源发票或预付计划唯一沿 execution → authorization → application_line 获取，不保留第二套可编辑来源。公司、供应商、币种一致；已确认分配合计等于对应有效出款，超额/未匹配部分进未决台账。
- HELD 复用执行冻结，ACTIVE 转为实付；同执行所有有效分配合计不超执行金额。确认同时维护授权、申请占用和预付余额。补正不释放；RELEASED 只由独立更正原子转移到正确来源或确认重复事实，不消除实际款项。
<a id="table-request_budget_allocation"></a>

### `request_budget_allocation`：申请行预算分配

所属模块：`finance`；类型：业务明细表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | --------------- | ---- | ------ | ---------- |
| `id` | `uuid` | 否 | `无` | 主键；应用生成 UUID |
| `legal_entity_id` | `uuid` | 否 | `无` | 所属公司；FK → `legal_entity.id` |
| `request_line_id` | `uuid` | 否 | `无` | ；FK → `purchase_request_line.id` |
| `budget_account_id` | `uuid` | 否 | `无` | ；FK → `budget_account.id` |
| `fx_snapshot_id` | `uuid` | 否 | `无` | ；FK → `exchange_rate_snapshot.id` |
| `estimated_transaction_amount` | `numeric(24,6)` | 否 | `无` | 本预算分配承担的申请原币金额 |
| `estimated_budget_amount` | `numeric(24,6)` | 否 | `无` | 按快照换算的预算币种金额 |
| `created_at` | `timestamptz` | 否 | `CURRENT_TIMESTAMP` | 创建时间 |
| `scope_amount` | `numeric(24,6)` | 否 | `无` | 本版计划采购的未下单范围；新预占 remaining_scope 初始值 |

数据库约束与索引：

- PK：`id`。
- UQ：`(legal_entity_id, id)`。
- FK：`(legal_entity_id) → legal_entity(id)`。
- FK：`(legal_entity_id, request_line_id) → purchase_request_line(legal_entity_id, id)`。
- FK：`(legal_entity_id, budget_account_id) → budget_account(legal_entity_id, id)`。
- FK：`(legal_entity_id, fx_snapshot_id) → exchange_rate_snapshot(legal_entity_id, id)`。
- UQ：`(request_line_id, budget_account_id)`。
- CHECK：`estimated_transaction_amount >= 0 AND estimated_budget_amount >= 0`。
- 同申请行分配原币额之和等于该行 estimated_remaining_amount；快照原币必须等于申请币种、目标币必须等于预算币种。
- CHECK：`scope_amount > 0`；同申请行所有预算分配的 scope_amount 均等于该稳定需求项批准总范围减跨版本有效订单范围。不同预算分配按相同范围比例分担成本。

<a id="table-budget_preencumbrance"></a>

### `budget_preencumbrance`：申请预算预占余额

所属模块：`finance`；类型：业务明细表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | --------------- | ---- | ------ | ---------- |
| `id` | `uuid` | 否 | `无` | 主键；应用生成 UUID |
| `legal_entity_id` | `uuid` | 否 | `无` | 所属公司；FK → `legal_entity.id` |
| `request_allocation_id` | `uuid` | 否 | `无` | ；FK → `request_budget_allocation.id` |
| `budget_account_id` | `uuid` | 否 | `无` | ；FK → `budget_account.id` |
| `remaining_scope` | `numeric(24,6)` | 否 | `0` | 该批准分配仍未下单的需求范围单位 |
| `remaining_amount` | `numeric(24,6)` | 否 | `0` | 剩余预算币预占 |
| `lock_version` | `integer` | 否 | `1` | 并发版本 |
| `created_at` | `timestamptz` | 否 | `CURRENT_TIMESTAMP` | 创建时间 |

数据库约束与索引：

- PK：`id`。
- UQ：`(legal_entity_id, id)`。
- FK：`(legal_entity_id) → legal_entity(id)`。
- FK：`(legal_entity_id, request_allocation_id) → request_budget_allocation(legal_entity_id, id)`。
- FK：`(legal_entity_id, budget_account_id) → budget_account(legal_entity_id, id)`。
- UQ：`(request_allocation_id)`。
- CHECK：`remaining_scope >= 0 AND remaining_amount >= 0 AND lock_version > 0`。
- 账户与来源预算分配账户必须相同。旧版本已消耗范围不搬迁；批准修订仅把未消耗余额以流水释放到新版预占。

<a id="table-order_budget_allocation"></a>

### `order_budget_allocation`：订单来源预算分配

所属模块：`finance`；类型：业务明细表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | --------------- | ---- | ------ | ---------- |
| `id` | `uuid` | 否 | `无` | 主键；应用生成 UUID |
| `legal_entity_id` | `uuid` | 否 | `无` | 所属公司；FK → `legal_entity.id` |
| `source_allocation_id` | `uuid` | 否 | `无` | ；FK → `order_source_allocation.id` |
| `request_allocation_id` | `uuid` | 否 | `无` | ；FK → `request_budget_allocation.id` |
| `budget_account_id` | `uuid` | 否 | `无` | ；FK → `budget_account.id` |
| `preencumbrance_id` | `uuid` | 否 | `无` | ；FK → `budget_preencumbrance.id` |
| `fx_snapshot_id` | `uuid` | 否 | `无` | ；FK → `exchange_rate_snapshot.id` |
| `scope_consumed` | `numeric(24,6)` | 否 | `无` | 本笔消费的需求范围单位 |
| `preencumbrance_consumed` | `numeric(24,6)` | 否 | `无` | 从原预占消耗的预算币金额 |
| `transaction_amount` | `numeric(24,6)` | 否 | `无` | 订单币种分配金额 |
| `budget_amount` | `numeric(24,6)` | 否 | `无` | 正式预算币金额 |
| `created_at` | `timestamptz` | 否 | `CURRENT_TIMESTAMP` | 创建时间 |

数据库约束与索引：

- PK：`id`。
- UQ：`(legal_entity_id, id)`。
- FK：`(legal_entity_id) → legal_entity(id)`。
- FK：`(legal_entity_id, source_allocation_id) → order_source_allocation(legal_entity_id, id)`。
- FK：`(legal_entity_id, request_allocation_id) → request_budget_allocation(legal_entity_id, id)`。
- FK：`(legal_entity_id, budget_account_id) → budget_account(legal_entity_id, id)`。
- FK：`(legal_entity_id, preencumbrance_id) → budget_preencumbrance(legal_entity_id, id)`。
- FK：`(legal_entity_id, fx_snapshot_id) → exchange_rate_snapshot(legal_entity_id, id)`。
- UQ：`(source_allocation_id, request_allocation_id)`。
- CHECK：`scope_consumed > 0 AND preencumbrance_consumed >= 0 AND transaction_amount >= 0 AND budget_amount >= 0`。
- 来源申请行、预算分配、预占、账户须一致；FK 存在性之外用复合 FK/延迟约束触发器验证完整链。快照 from 为订单币种，to 为预算币种。
- 同一订单行所有 transaction_amount 合计等于订单行含税金额；同一来源范围在每个预算分配按同一比例消耗，最后一笔吸收币种尾差；除对应份额外不得释放其余预占。

## 10. 跨模块流程模块

<a id="table-correction_request"></a>

### `correction_request`：跨模块纠错申请

所属模块：`workflows`；类型：单据主表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | --------------- | ---- | ------ | ---------- |
| `id` | `uuid` | 否 | `无` | 主键；沿用 business_document.id；FK → `business_document.id`；PK |
| `legal_entity_id` | `uuid` | 否 | `无` | 所属法人；FK → `legal_entity.id` |
| `original_document_id` | `uuid` | 否 | `无` | 原单据；FK → `business_document.id` |
| `original_revision` | `integer` | 否 | `无` | 被纠错版本 |
| `correction_type` | `varchar(32)` | 否 | `'RECEIPT_REVERSE'` | 允许值：RECEIPT_REVERSE, STOCK_REVERSE, INVOICE_ENTRY_VOID, INVOICE_REAL_VOID, PAYMENT_ENTRY_CORRECT |
| `reason` | `text` | 否 | `无` | 纠错原因 |
| `source_manifest` | `jsonb` | 否 | `'{}'::jsonb` | 具体来源流水 ID 和预期版本清单 |
| `executed_at` | `timestamptz` | 是 | `无` | 获批原子执行时间 |
| `created_at` | `timestamptz` | 否 | `CURRENT_TIMESTAMP` | 创建时间 |

数据库约束与索引：

- PK：`id`。
- UQ：`(legal_entity_id, id)`。
- FK：`(legal_entity_id, id) → business_document(legal_entity_id, id)`。
- FK：`(legal_entity_id) → legal_entity(id)`。
- FK：`(legal_entity_id, original_document_id) → business_document(legal_entity_id, id)`。
- 普通索引：`(legal_entity_id, original_document_id)`。
- 主表类型须与 `business_document.document_type` 匹配，状态使用通用登记的当前状态。

补充约束与执行规则：

- 复合外键 (original_document_id, original_revision) → document_revision(document_id, revision)。首期只支持整笔来源流水完整冲销；申请可选多笔完整流水，不做单条流水的部分冲销。manifest 只作执行输入，实际反向流水的原记录关联使用真实外键。

<a id="table-exception_case"></a>

### `exception_case`：异常工单

所属模块：`workflows`；类型：单据主表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | --------------- | ---- | ------ | ---------- |
| `id` | `uuid` | 否 | `无` | 主键；沿用 business_document.id；FK → `business_document.id`；PK |
| `legal_entity_id` | `uuid` | 否 | `无` | 所属法人；FK → `legal_entity.id` |
| `related_document_id` | `uuid` | 是 | `无` | 关联单据；FK → `business_document.id` |
| `case_type` | `varchar(64)` | 否 | `无` | 异常类别 |
| `assignee_id` | `uuid` | 否 | `无` | 负责人；FK → `app_user.id` |
| `description` | `text` | 否 | `无` | 事实描述 |
| `resolution` | `text` | 是 | `无` | 处理结论 |
| `proof_attachment_id` | `uuid` | 是 | `无` | 处理凭证；FK → `attachment.id` |
| `resolved_at` | `timestamptz` | 是 | `无` | 解决时间 |
| `created_at` | `timestamptz` | 否 | `CURRENT_TIMESTAMP` | 创建时间 |

数据库约束与索引：

- PK：`id`。
- UQ：`(legal_entity_id, id)`。
- FK：`(legal_entity_id, id) → business_document(legal_entity_id, id)`。
- FK：`(legal_entity_id) → legal_entity(id)`。
- FK：`(legal_entity_id, related_document_id) → business_document(legal_entity_id, id)`。
- FK：`(legal_entity_id, assignee_id) → app_user(legal_entity_id, id)`。
- FK：`(legal_entity_id, proof_attachment_id) → attachment(legal_entity_id, id)`。
- 普通索引：`(legal_entity_id, related_document_id)`。
- 普通索引：`(legal_entity_id, assignee_id)`。
- 普通索引：`(legal_entity_id, proof_attachment_id)`。
- 主表类型须与 `business_document.document_type` 匹配，状态使用通用登记的当前状态。

补充约束与执行规则：

- 状态 OPEN/IN_PROGRESS/RESOLVED/CANCELLED 存 business_document；关闭工单不自动修改库存、预算或付款账本。

### 10.1 第二轮补齐的履约、资金与治理实体

<a id="table-return_review"></a>

### `return_review`：退货分工核实记录

所属模块：`inventory`；类型：业务记录表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | ---- | ---- | ---- | ---- |
| `id` | `uuid` | 否 | `无` | 主键 |
| `legal_entity_id` | `uuid` | 否 | `无` | FK → `legal_entity.id` |
| `return_order_id` | `uuid` | 否 | `无` | FK → `return_order.id` |
| `return_revision` | `integer` | 否 | `无` |  |
| `review_type` | `varchar(32)` | 否 | `无` | CHECK 允许值：COMMERCIAL, FINANCE |
| `reviewer_id` | `uuid` | 否 | `无` | FK → `app_user.id` |
| `result` | `varchar(32)` | 否 | `无` | CHECK 允许值：READY, BLOCKED, NOT_APPLICABLE |
| `reason` | `text` | 否 | `无` |  |
| `evidence_id` | `uuid` | 是 | `无` | FK → `attachment.id` |
| `reviewed_at` | `timestamptz` | 否 | `无` |  |
| `created_at` | `timestamptz` | 否 | `无` | 创建时间；默认 CURRENT_TIMESTAMP |

数据库约束与执行规则：

- PK：`id`；UQ：`(legal_entity_id, id)`。所有法人范围引用按 1 节生成同公司复合 FK；每个 FK 的来源列建立索引。
- FK：`(return_order_id, return_revision) → document_revision(document_id, revision)`；核实记录追加不可覆盖。
- 审批检查当前版本商务 READY；有票款关联必须 FINANCE READY 且有依据，无关联可由授权财务确认 NOT_APPLICABLE。过账重新持锁检查分配，不因旧 READY 绕过新增发票占用。

<a id="table-arrival"></a>

### `arrival`：到货事实

所属模块：`inventory`；类型：业务记录表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | ---- | ---- | ---- | ---- |
| `id` | `uuid` | 否 | `无` | 主键 |
| `legal_entity_id` | `uuid` | 否 | `无` | FK → `legal_entity.id` |
| `purchase_order_id` | `uuid` | 是 | `无` | FK → `purchase_order.id` |
| `warehouse_id` | `uuid` | 否 | `无` | FK → `warehouse.id` |
| `recorded_by` | `uuid` | 否 | `无` | FK → `app_user.id` |
| `arrived_at` | `timestamptz` | 否 | `无` |  |
| `external_reference` | `varchar(160)` | 否 | `无` |  |
| `evidence_id` | `uuid` | 否 | `无` | FK → `attachment.id` |
| `status` | `varchar(32)` | 否 | `无` | CHECK 允许值：UNRESOLVED, MATCHED, CLOSED |
| `created_at` | `timestamptz` | 否 | `无` | 创建时间；默认 CURRENT_TIMESTAMP |

数据库约束与执行规则：

- PK：`id`；UQ：`(legal_entity_id, id)`。所有法人范围引用按 1 节生成同公司复合 FK；每个 FK 的来源列建立索引。
- UQ：(legal_entity_id, warehouse_id, external_reference)。无有效订单也保存实到事实为 UNRESOLVED，补关联审计；未匹配不得入采购库存。

<a id="table-arrival_line"></a>

### `arrival_line`：到货明细及保管余额

所属模块：`inventory`；类型：业务记录表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | ---- | ---- | ---- | ---- |
| `id` | `uuid` | 否 | `无` | 主键 |
| `legal_entity_id` | `uuid` | 否 | `无` | FK → `legal_entity.id` |
| `arrival_id` | `uuid` | 否 | `无` | FK → `arrival.id` |
| `line_no` | `integer` | 否 | `无` |  |
| `order_line_id` | `uuid` | 是 | `无` | FK → `purchase_order_line.id` |
| `material_id` | `uuid` | 否 | `无` | FK → `material.id` |
| `arrived_quantity` | `numeric(20,6)` | 否 | `无` |  |
| `pending_quantity` | `numeric(20,6)` | 否 | `无` |  |
| `qualified_quantity` | `numeric(20,6)` | 否 | `无` |  |
| `unqualified_quantity` | `numeric(20,6)` | 否 | `无` |  |
| `rejected_quantity` | `numeric(20,6)` | 否 | `无` |  |
| `returned_quantity` | `numeric(20,6)` | 否 | `无` |  |
| `posted_quantity` | `numeric(20,6)` | 否 | `无` |  |
| `lock_version` | `bigint` | 否 | `无` |  |
| `created_at` | `timestamptz` | 否 | `无` | 创建时间；默认 CURRENT_TIMESTAMP |

数据库约束与执行规则：

- PK：`id`；UQ：`(legal_entity_id, id)`。所有法人范围引用按 1 节生成同公司复合 FK；每个 FK 的来源列建立索引。
- UQ：(arrival_id, line_no)；CHECK：line_no > 0 AND arrived_quantity > 0；CHECK：pending_quantity >= 0 AND qualified_quantity >= 0 AND unqualified_quantity >= 0 AND rejected_quantity >= 0 AND returned_quantity >= 0 AND posted_quantity >= 0。
- CHECK：arrived_quantity = pending_quantity + qualified_quantity + unqualified_quantity + rejected_quantity + returned_quantity + posted_quantity。初次拒收也由交接流水产生；余额只由 arrival_custody_movement 与检验、交接、收货同事务维护。
- 订单匹配后公司、订单、物料、单位一致；来源订单无效时保管事实继续记录但禁止采购收货过账。

<a id="table-arrival_inspection"></a>

### `arrival_inspection`：分批检验及复检事实

所属模块：`inventory`；类型：业务记录表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | ---- | ---- | ---- | ---- |
| `id` | `uuid` | 否 | `无` | 主键 |
| `legal_entity_id` | `uuid` | 否 | `无` | FK → `legal_entity.id` |
| `arrival_line_id` | `uuid` | 否 | `无` | FK → `arrival_line.id` |
| `supersedes_id` | `uuid` | 是 | `无` | FK → `arrival_inspection.id` |
| `inspector_id` | `uuid` | 否 | `无` | FK → `app_user.id` |
| `policy_id` | `uuid` | 否 | `无` | FK → `business_policy_version.id` |
| `quantity` | `numeric(20,6)` | 否 | `无` |  |
| `from_bucket` | `varchar(32)` | 否 | `无` | CHECK 允许值：PENDING, QUALIFIED, UNQUALIFIED |
| `result` | `varchar(32)` | 否 | `无` | CHECK 允许值：QUALIFIED, UNQUALIFIED |
| `evidence_id` | `uuid` | 否 | `无` | FK → `attachment.id` |
| `inspected_at` | `timestamptz` | 否 | `无` |  |
| `posting_key` | `varchar(160)` | 否 | `无` |  |
| `created_at` | `timestamptz` | 否 | `无` | 创建时间；默认 CURRENT_TIMESTAMP |

数据库约束与执行规则：

- PK：`id`；UQ：`(legal_entity_id, id)`。所有法人范围引用按 1 节生成同公司复合 FK；每个 FK 的来源列建立索引。
- CHECK：quantity > 0；UQ：(legal_entity_id, posting_key)；复检引用原检验，不覆盖历史；可多次分批复检，累计不能超过该原检验尚未入库/退回的数量。
- 锁 arrival_line 后消耗来源桶，增加结论桶；初检从 PENDING，复检须有 supersedes_id 且来源一致；相同结论复检仅保存证据、不迁移桶或重复增加余额。免专职检验也须仓管员依规则保存接收检查与规则版本。

<a id="table-arrival_handoff"></a>

### `arrival_handoff`：拒收与未入库退回交接

所属模块：`inventory`；类型：业务记录表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | ---- | ---- | ---- | ---- |
| `id` | `uuid` | 否 | `无` | 主键 |
| `legal_entity_id` | `uuid` | 否 | `无` | FK → `legal_entity.id` |
| `arrival_line_id` | `uuid` | 否 | `无` | FK → `arrival_line.id` |
| `handoff_type` | `varchar(32)` | 否 | `无` | CHECK 允许值：REJECT_AT_ARRIVAL, RETURN_UNPOSTED |
| `from_bucket` | `varchar(32)` | 否 | `无` | CHECK 允许值：PENDING, QUALIFIED, UNQUALIFIED |
| `quantity` | `numeric(20,6)` | 否 | `无` |  |
| `handled_by` | `uuid` | 否 | `无` | FK → `app_user.id` |
| `counterparty` | `text` | 否 | `无` |  |
| `evidence_id` | `uuid` | 否 | `无` | FK → `attachment.id` |
| `handed_at` | `timestamptz` | 否 | `无` |  |
| `posting_key` | `varchar(160)` | 否 | `无` |  |
| `created_at` | `timestamptz` | 否 | `无` | 创建时间；默认 CURRENT_TIMESTAMP |

数据库约束与执行规则：

- PK：`id`；UQ：`(legal_entity_id, id)`。所有法人范围引用按 1 节生成同公司复合 FK；每个 FK 的来源列建立索引。
- CHECK：quantity > 0；UQ：(legal_entity_id, posting_key)；持锁从来源保管桶扣减并转入 rejected_quantity 或 returned_quantity。
- REJECT_AT_ARRIVAL 仅初始交接且来源 PENDING；不写库存或预算流水；不能消耗已入库量。

<a id="table-contract_signature_event"></a>

### `contract_signature_event`：合同签署及确认历史

所属模块：`procurement`；类型：业务记录表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | ---- | ---- | ---- | ---- |
| `id` | `uuid` | 否 | `无` | 主键 |
| `legal_entity_id` | `uuid` | 否 | `无` | FK → `legal_entity.id` |
| `contract_id` | `uuid` | 否 | `无` | FK → `purchase_contract.id` |
| `contract_revision` | `integer` | 否 | `无` |  |
| `change_package_id` | `uuid` | 是 | `无` | FK → `change_package.id` |
| `event_type` | `varchar(32)` | 否 | `无` | CHECK 允许值：SIGNED, COUNTERPARTY_CONFIRMED, EFFECTIVE, TERMINATED |
| `recorded_by` | `uuid` | 否 | `无` | FK → `app_user.id` |
| `counterparty` | `text` | 否 | `无` |  |
| `evidence_id` | `uuid` | 否 | `无` | FK → `attachment.id` |
| `occurred_at` | `timestamptz` | 否 | `无` |  |
| `event_key` | `varchar(160)` | 否 | `无` |  |
| `created_at` | `timestamptz` | 否 | `无` | 创建时间；默认 CURRENT_TIMESTAMP |

数据库约束与执行规则：

- PK：`id`；UQ：`(legal_entity_id, id)`。所有法人范围引用按 1 节生成同公司复合 FK；每个 FK 的来源列建立索引。
- FK：`(contract_id, contract_revision) → document_revision(document_id, revision)`；UQ：(legal_entity_id, event_key)；仅追加，纠错用有审批依据的新事件保留原件。
- 签署不等于生效；激活须批准版本及签署/条件证据齐备，锁合同及派生订单后只改变执行资格，不重复记预算。

<a id="table-change_package"></a>

### `change_package`：合同与订单联动变更包

所属模块：`procurement`；类型：单据主表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | ---- | ---- | ---- | ---- |
| `id` | `uuid` | 否 | `无` | 主键；FK → `business_document.id` |
| `legal_entity_id` | `uuid` | 否 | `无` | FK → `legal_entity.id` |
| `submitted_by` | `uuid` | 否 | `无` | FK → `app_user.id` |
| `approval_instance_id` | `uuid` | 是 | `无` | FK → `approval_instance.id` |
| `reason` | `text` | 否 | `无` |  |
| `requires_confirmation` | `boolean` | 否 | `无` |  |
| `confirmation_evidence_id` | `uuid` | 是 | `无` | FK → `attachment.id` |
| `applied_at` | `timestamptz` | 是 | `无` |  |
| `created_at` | `timestamptz` | 否 | `无` | 创建时间；默认 CURRENT_TIMESTAMP |

数据库约束与执行规则：

- PK：`id`；UQ：`(legal_entity_id, id)`。所有法人范围引用按 1 节生成同公司复合 FK；每个 FK 的来源列建立索引。
- 主状态、内容版本及审批沿用通用单据；主表 ID 等于 business_document.id，类型必须匹配。
- 主状态：DRAFT, IN_REVIEW, APPROVED, WAITING_CONFIRMATION, APPLIED, RETURNED, REJECTED, CANCELLED。
- 提交冻结包清单，审批引用包版本及全部目标版本；要求双方确认时应用前证据非空。每目标只能有一个活跃变更包，由目标文档锁和约束触发器保证。
- 应用按 DESIGN 11.1 锁全部目标及来源、预算；重查原有效版本、已履约下限和资金占用。任何增额不足全部回滚；应用时间与版本切换/预算差额同事务，仅一次。

<a id="table-change_package_item"></a>

### `change_package_item`：变更包版本清单

所属模块：`procurement`；类型：业务记录表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | ---- | ---- | ---- | ---- |
| `id` | `uuid` | 否 | `无` | 主键 |
| `legal_entity_id` | `uuid` | 否 | `无` | FK → `legal_entity.id` |
| `change_package_id` | `uuid` | 否 | `无` | FK → `change_package.id` |
| `package_revision` | `integer` | 否 | `无` |  |
| `target_document_id` | `uuid` | 否 | `无` | FK → `business_document.id` |
| `base_revision` | `integer` | 否 | `无` |  |
| `proposed_revision` | `integer` | 否 | `无` |  |
| `reason` | `text` | 否 | `无` |  |
| `created_at` | `timestamptz` | 否 | `无` | 创建时间；默认 CURRENT_TIMESTAMP |

数据库约束与执行规则：

- PK：`id`；UQ：`(legal_entity_id, id)`。所有法人范围引用按 1 节生成同公司复合 FK；每个 FK 的来源列建立索引。
- UQ：(change_package_id, package_revision, target_document_id)；CHECK：base_revision > 0 AND proposed_revision > base_revision。
- FK：`(change_package_id, package_revision) → document_revision(document_id, revision)`；FK：`(target_document_id, base_revision) → document_revision(document_id, revision)`；FK：`(target_document_id, proposed_revision) → document_revision(document_id, revision)`。
- 目标仅合同或订单；新旧明细及支付计划差异取精确版本，不能只保存自由 JSON 差额。固定合同包须包含全部受影响派生订单；已履约明细不覆盖，新行继承来源关系并校验累计范围。

<a id="table-payment_plan"></a>

### `payment_plan`：订单支付计划

所属模块：`finance`；类型：单据主表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | ---- | ---- | ---- | ---- |
| `id` | `uuid` | 否 | `无` | 主键；FK → `business_document.id` |
| `legal_entity_id` | `uuid` | 否 | `无` | FK → `legal_entity.id` |
| `purchase_order_id` | `uuid` | 否 | `无` | FK → `purchase_order.id` |
| `order_revision` | `integer` | 否 | `无` |  |
| `approval_instance_id` | `uuid` | 是 | `无` | FK → `approval_instance.id` |
| `submitted_by` | `uuid` | 否 | `无` | FK → `app_user.id` |
| `created_at` | `timestamptz` | 否 | `无` | 创建时间；默认 CURRENT_TIMESTAMP |

数据库约束与执行规则：

- PK：`id`；UQ：`(legal_entity_id, id)`。所有法人范围引用按 1 节生成同公司复合 FK；每个 FK 的来源列建立索引。
- 主状态、内容版本及审批沿用通用单据；主表 ID 等于 business_document.id，类型必须匹配。
- 主状态：DRAFT, IN_REVIEW, APPROVED, RETURNED, REJECTED, CLOSED；FK：`(purchase_order_id, order_revision) → document_revision(document_id, revision)`。
- 计划行币种取订单；批准的各阶段金额合计不得超过订单有效承诺，变更计划走受控审批并保留已付事实；存在占用不能删除旧行。

<a id="table-payment_plan_line"></a>

### `payment_plan_line`：支付计划阶段

所属模块：`finance`；类型：业务记录表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | ---- | ---- | ---- | ---- |
| `id` | `uuid` | 否 | `无` | 主键 |
| `legal_entity_id` | `uuid` | 否 | `无` | FK → `legal_entity.id` |
| `payment_plan_id` | `uuid` | 否 | `无` | FK → `payment_plan.id` |
| `revision` | `integer` | 否 | `无` |  |
| `line_no` | `integer` | 否 | `无` |  |
| `order_line_id` | `uuid` | 否 | `无` | FK → `purchase_order_line.id` |
| `payment_kind` | `varchar(32)` | 否 | `无` | CHECK 允许值：PREPAYMENT, AFTER_ACCEPTANCE |
| `amount` | `numeric(24,6)` | 否 | `无` |  |
| `due_date` | `date` | 是 | `无` |  |
| `trigger_condition` | `text` | 否 | `无` |  |
| `created_at` | `timestamptz` | 否 | `无` | 创建时间；默认 CURRENT_TIMESTAMP |

数据库约束与执行规则：

- PK：`id`；UQ：`(legal_entity_id, id)`。所有法人范围引用按 1 节生成同公司复合 FK；每个 FK 的来源列建立索引。
- UQ：(payment_plan_id, revision, line_no)；CHECK：amount > 0 AND line_no > 0；FK：`(payment_plan_id, revision) → document_revision(document_id, revision)`。
- 订单行属于计划订单版本；一期一行归属一个订单行，多行分配不得重复。计划批准不代表触发条件满足。新版本行不得重置可预付上限：按同一订单行聚合所有历史计划行的已付、未结清申请及授权；变更时先关闭旧行未使用额度，再批准新行剩余额度，累计不得超当前订单行预付上限。

<a id="table-payment_application"></a>

### `payment_application`：付款申请

所属模块：`finance`；类型：单据主表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | ---- | ---- | ---- | ---- |
| `id` | `uuid` | 否 | `无` | 主键；FK → `business_document.id` |
| `legal_entity_id` | `uuid` | 否 | `无` | FK → `legal_entity.id` |
| `supplier_id` | `uuid` | 否 | `无` | FK → `supplier.id` |
| `currency` | `char(3)` | 否 | `无` | FK → `currency.code` |
| `submitted_by` | `uuid` | 否 | `无` | FK → `app_user.id` |
| `approval_instance_id` | `uuid` | 是 | `无` | FK → `approval_instance.id` |
| `condition_evidence_id` | `uuid` | 否 | `无` | FK → `attachment.id` |
| `created_at` | `timestamptz` | 否 | `无` | 创建时间；默认 CURRENT_TIMESTAMP |

数据库约束与执行规则：

- PK：`id`；UQ：`(legal_entity_id, id)`。所有法人范围引用按 1 节生成同公司复合 FK；每个 FK 的来源列建立索引。
- 主状态、内容版本及审批沿用通用单据；主表 ID 等于 business_document.id，类型必须匹配。
- 主状态：DRAFT, IN_REVIEW, APPROVED, SETTLED, RETURNED, REJECTED, CANCELLED；登记人不能最终审批。
- 提交持锁校验来源可申请额，行转 HELD；批准原子生成授权，申请占用不再次累加。返回/拒绝只释放未执行占用；获批撤销走授权撤销单。

<a id="table-payment_application_line"></a>

### `payment_application_line`：付款申请来源占用

所属模块：`finance`；类型：业务记录表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | ---- | ---- | ---- | ---- |
| `id` | `uuid` | 否 | `无` | 主键 |
| `legal_entity_id` | `uuid` | 否 | `无` | FK → `legal_entity.id` |
| `payment_application_id` | `uuid` | 否 | `无` | FK → `payment_application.id` |
| `revision` | `integer` | 否 | `无` |  |
| `line_no` | `integer` | 否 | `无` |  |
| `payment_kind` | `varchar(32)` | 否 | `无` | CHECK 允许值：PREPAYMENT, INVOICE |
| `plan_line_id` | `uuid` | 是 | `无` | FK → `payment_plan_line.id` |
| `invoice_line_id` | `uuid` | 是 | `无` | FK → `invoice_line.id` |
| `amount` | `numeric(24,6)` | 否 | `无` |  |
| `outstanding_amount` | `numeric(24,6)` | 否 | `无` |  |
| `status` | `varchar(32)` | 否 | `无` | CHECK 允许值：DRAFT, HELD, AUTHORIZED, SETTLED, RELEASED |
| `created_at` | `timestamptz` | 否 | `无` | 创建时间；默认 CURRENT_TIMESTAMP |

数据库约束与执行规则：

- PK：`id`；UQ：`(legal_entity_id, id)`。所有法人范围引用按 1 节生成同公司复合 FK；每个 FK 的来源列建立索引。
- UQ：(payment_application_id, revision, line_no)；CHECK：amount > 0 AND outstanding_amount BETWEEN 0 AND amount；FK：`(payment_application_id, revision) → document_revision(document_id, revision)`。
- CHECK：(payment_kind = 'PREPAYMENT' AND plan_line_id IS NOT NULL AND invoice_line_id IS NULL) OR (payment_kind = 'INVOICE' AND invoice_line_id IS NOT NULL AND plan_line_id IS NULL)。
- 同公司/供应商/币种及订单范围；预付来源计划已批准且 PREPAYMENT，普通来源发票已确认。outstanding_amount 是尚未结清申请占用，含内部执行冻结；确认实付或批准撤销才减少。

<a id="table-payment_authorization"></a>

### `payment_authorization`：逐申请行支付授权

所属模块：`finance`；类型：业务记录表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | ---- | ---- | ---- | ---- |
| `id` | `uuid` | 否 | `无` | 主键 |
| `legal_entity_id` | `uuid` | 否 | `无` | FK → `legal_entity.id` |
| `application_line_id` | `uuid` | 否 | `无` | FK → `payment_application_line.id` |
| `authorized_amount` | `numeric(24,6)` | 否 | `无` |  |
| `revoked_amount` | `numeric(24,6)` | 否 | `无` |  |
| `confirmed_amount` | `numeric(24,6)` | 否 | `无` |  |
| `frozen_amount` | `numeric(24,6)` | 否 | `无` |  |
| `lock_version` | `bigint` | 否 | `无` |  |
| `created_at` | `timestamptz` | 否 | `无` | 创建时间；默认 CURRENT_TIMESTAMP |

数据库约束与执行规则：

- PK：`id`；UQ：`(legal_entity_id, id)`。所有法人范围引用按 1 节生成同公司复合 FK；每个 FK 的来源列建立索引。
- UQ：(application_line_id)；CHECK：revoked_amount >= 0 AND confirmed_amount >= 0 AND frozen_amount >= 0；CHECK：authorized_amount > 0 AND revoked_amount + confirmed_amount + frozen_amount <= authorized_amount。
- 原始授权等于批准申请行金额，后续保留撤销单、执行核实及实际分配事件并维护汇总；冻结是申请占用子集。关闭授权不得修改实际支出。

<a id="table-payment_execution"></a>

### `payment_execution`：支付执行任务

所属模块：`finance`；类型：业务记录表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | ---- | ---- | ---- | ---- |
| `id` | `uuid` | 否 | `无` | 主键 |
| `legal_entity_id` | `uuid` | 否 | `无` | FK → `legal_entity.id` |
| `authorization_id` | `uuid` | 否 | `无` | FK → `payment_authorization.id` |
| `claimed_by` | `uuid` | 否 | `无` | FK → `app_user.id` |
| `amount` | `numeric(24,6)` | 否 | `无` |  |
| `status` | `varchar(32)` | 否 | `无` | CHECK 允许值：CLAIMED, EXECUTING, UNKNOWN, PENDING_REVIEW, NEEDS_CORRECTION, CONFIRMED, FAILED |
| `idempotency_key` | `varchar(160)` | 否 | `无` |  |
| `finished_at` | `timestamptz` | 是 | `无` |  |
| `created_at` | `timestamptz` | 否 | `无` | 创建时间；默认 CURRENT_TIMESTAMP |

数据库约束与执行规则：

- PK：`id`；UQ：`(legal_entity_id, id)`。所有法人范围引用按 1 节生成同公司复合 FK；每个 FK 的来源列建立索引。
- CHECK：amount > 0；UQ：(legal_entity_id, idempotency_key)。认领时锁授权并冻结，未知/补正不释放；只有独立确认失败或确认实付才能结束冻结。
- 同一授权可分次执行；实付关联从 payment_allocation 汇总，每次确认原子将冻结转确认实付，并减少来源申请占用；超额进入异常阻断，不强塞入合法授权余额。

<a id="table-payment_execution_review"></a>

### `payment_execution_review`：支付失败与重复核实

所属模块：`finance`；类型：业务记录表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | ---- | ---- | ---- | ---- |
| `id` | `uuid` | 否 | `无` | 主键 |
| `legal_entity_id` | `uuid` | 否 | `无` | FK → `legal_entity.id` |
| `execution_id` | `uuid` | 否 | `无` | FK → `payment_execution.id` |
| `submitted_by` | `uuid` | 否 | `无` | FK → `app_user.id` |
| `reviewed_by` | `uuid` | 是 | `无` | FK → `app_user.id` |
| `conclusion` | `varchar(32)` | 否 | `无` | CHECK 允许值：PENDING, UNKNOWN, FAILED, DUPLICATE |
| `canonical_execution_id` | `uuid` | 是 | `无` | FK → `payment_execution.id` |
| `evidence_id` | `uuid` | 否 | `无` | FK → `attachment.id` |
| `reason` | `text` | 否 | `无` |  |
| `reviewed_at` | `timestamptz` | 是 | `无` |  |
| `created_at` | `timestamptz` | 否 | `无` | 创建时间；默认 CURRENT_TIMESTAMP |

数据库约束与执行规则：

- PK：`id`；UQ：`(legal_entity_id, id)`。所有法人范围引用按 1 节生成同公司复合 FK；每个 FK 的来源列建立索引。
- CHECK：reviewed_by IS NULL OR reviewed_by <> submitted_by；失败/重复结论需复核人、时间和证据；DUPLICATE 必有不同 canonical_execution_id；reviewed_by 还必须不同于 execution.claimed_by。
- FAILED 仅经证明未出款，无真实出款关联；持锁释放该执行冻结但不撤销授权。DUPLICATE 锁两执行及实际流水，合并到唯一真实事实后仅释放冗余占用；UNKNOWN 不释放。

<a id="table-payment_authorization_revocation"></a>

### `payment_authorization_revocation`：未执行授权撤销

所属模块：`finance`；类型：单据主表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | ---- | ---- | ---- | ---- |
| `id` | `uuid` | 否 | `无` | 主键；FK → `business_document.id` |
| `legal_entity_id` | `uuid` | 否 | `无` | FK → `legal_entity.id` |
| `authorization_id` | `uuid` | 否 | `无` | FK → `payment_authorization.id` |
| `submitted_by` | `uuid` | 否 | `无` | FK → `app_user.id` |
| `approval_instance_id` | `uuid` | 是 | `无` | FK → `approval_instance.id` |
| `amount` | `numeric(24,6)` | 否 | `无` |  |
| `reason` | `text` | 否 | `无` |  |
| `applied_at` | `timestamptz` | 是 | `无` |  |
| `created_at` | `timestamptz` | 否 | `无` | 创建时间；默认 CURRENT_TIMESTAMP |

数据库约束与执行规则：

- PK：`id`；UQ：`(legal_entity_id, id)`。所有法人范围引用按 1 节生成同公司复合 FK；每个 FK 的来源列建立索引。
- 主状态、内容版本及审批沿用通用单据；主表 ID 等于 business_document.id，类型必须匹配。
- 主状态：DRAFT, IN_REVIEW, APPROVED, RETURNED, REJECTED, CANCELLED；CHECK：amount > 0。
- 批准锁授权，仅 amount <= authorized_amount - revoked_amount - confirmed_amount - frozen_amount；同事务增加 revoked_amount、减少申请 outstanding_amount，释放来源占用。执行认领与撤销互斥，重复批准不再释放。

<a id="table-prepayment_balance"></a>

### `prepayment_balance`：逐笔预付余额

所属模块：`finance`；类型：业务记录表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | ---- | ---- | ---- | ---- |
| `id` | `uuid` | 否 | `无` | 主键 |
| `legal_entity_id` | `uuid` | 否 | `无` | FK → `legal_entity.id` |
| `payment_allocation_id` | `uuid` | 否 | `无` | FK → `payment_allocation.id` |
| `plan_line_id` | `uuid` | 否 | `无` | FK → `payment_plan_line.id` |
| `paid_amount` | `numeric(24,6)` | 否 | `无` |  |
| `applied_amount` | `numeric(24,6)` | 否 | `无` |  |
| `held_amount` | `numeric(24,6)` | 否 | `无` |  |
| `refund_frozen_amount` | `numeric(24,6)` | 否 | `无` |  |
| `refunded_amount` | `numeric(24,6)` | 否 | `无` |  |
| `lock_version` | `bigint` | 否 | `无` |  |
| `created_at` | `timestamptz` | 否 | `无` | 创建时间；默认 CURRENT_TIMESTAMP |

数据库约束与执行规则：

- PK：`id`；UQ：`(legal_entity_id, id)`。所有法人范围引用按 1 节生成同公司复合 FK；每个 FK 的来源列建立索引。
- UQ：(payment_allocation_id)；CHECK：paid_amount > 0 AND applied_amount >= 0 AND held_amount >= 0 AND refund_frozen_amount >= 0 AND refunded_amount >= 0；CHECK：applied_amount + held_amount + refund_frozen_amount + refunded_amount <= paid_amount。
- 仅预付实际分配独立确认后创建；paid_amount 等于确认分配金额，不因核销恢复支付计划额度。退款未支持自动结算时冻结，已退款必须新流入事实及复核依据。

<a id="table-prepayment_application"></a>

### `prepayment_application`：预付核销单

所属模块：`finance`；类型：单据主表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | ---- | ---- | ---- | ---- |
| `id` | `uuid` | 否 | `无` | 主键；FK → `business_document.id` |
| `legal_entity_id` | `uuid` | 否 | `无` | FK → `legal_entity.id` |
| `submitted_by` | `uuid` | 否 | `无` | FK → `app_user.id` |
| `reviewed_by` | `uuid` | 是 | `无` | FK → `app_user.id` |
| `confirmed_at` | `timestamptz` | 是 | `无` |  |
| `reversal_of_id` | `uuid` | 是 | `无` | FK → `prepayment_application.id` |
| `approval_instance_id` | `uuid` | 是 | `无` | FK → `approval_instance.id` |
| `created_at` | `timestamptz` | 否 | `无` | 创建时间；默认 CURRENT_TIMESTAMP |

数据库约束与执行规则：

- PK：`id`；UQ：`(legal_entity_id, id)`。所有法人范围引用按 1 节生成同公司复合 FK；每个 FK 的来源列建立索引。
- 主状态、内容版本及审批沿用通用单据；主表 ID 等于 business_document.id，类型必须匹配。
- 主状态：DRAFT, IN_REVIEW, CONFIRMED, RETURNED, REJECTED, REVERSED；CHECK：reviewed_by IS NULL OR reviewed_by <> submitted_by；UQ：(reversal_of_id)。
- 核销与撤销均独立复核；撤销为独立单据引用原单，锁两端且重新校验发票后续占用，不能使任何余额为负；确认撤销时原单标 REVERSED，历史行不删除。

<a id="table-prepayment_application_line"></a>

### `prepayment_application_line`：核销双边分配

所属模块：`finance`；类型：业务记录表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | ---- | ---- | ---- | ---- |
| `id` | `uuid` | 否 | `无` | 主键 |
| `legal_entity_id` | `uuid` | 否 | `无` | FK → `legal_entity.id` |
| `prepayment_application_id` | `uuid` | 否 | `无` | FK → `prepayment_application.id` |
| `revision` | `integer` | 否 | `无` |  |
| `line_no` | `integer` | 否 | `无` |  |
| `prepayment_balance_id` | `uuid` | 否 | `无` | FK → `prepayment_balance.id` |
| `invoice_line_id` | `uuid` | 否 | `无` | FK → `invoice_line.id` |
| `amount` | `numeric(24,6)` | 否 | `无` |  |
| `status` | `varchar(32)` | 否 | `无` | CHECK 允许值：DRAFT, HELD, ACTIVE, RELEASED |
| `created_at` | `timestamptz` | 否 | `无` | 创建时间；默认 CURRENT_TIMESTAMP |

数据库约束与执行规则：

- PK：`id`；UQ：`(legal_entity_id, id)`。所有法人范围引用按 1 节生成同公司复合 FK；每个 FK 的来源列建立索引。
- CHECK：amount > 0；UQ：(prepayment_application_id, revision, line_no)；FK：`(prepayment_application_id, revision) → document_revision(document_id, revision)`。
- 提交锁预付余额与发票行建立双边 HELD；确认转 ACTIVE 不再次扣款，退回/拒绝释放 HELD。币种、供应商、公司和订单行必须一致；发票已确认且有有效履约。撤销单必须完整复制原单当前有效分配并整单撤销，不支持部分撤销；UQ(reversal_of_id) 保证仅一次。撤销提交不再建立一次正向 HELD，改为冻结原 ACTIVE 记录及两端后续变更；批准释放原核销，退回则仅解除撤销动作锁定。

<a id="table-payment_unresolved_case"></a>

### `payment_unresolved_case`：未决资金与退款阻断

所属模块：`finance`；类型：业务记录表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | ---- | ---- | ---- | ---- |
| `id` | `uuid` | 否 | `无` | 主键 |
| `legal_entity_id` | `uuid` | 否 | `无` | FK → `legal_entity.id` |
| `payment_record_id` | `uuid` | 是 | `无` | FK → `payment_record.id` |
| `authorization_id` | `uuid` | 是 | `无` | FK → `payment_authorization.id` |
| `prepayment_balance_id` | `uuid` | 是 | `无` | FK → `prepayment_balance.id` |
| `order_line_id` | `uuid` | 是 | `无` | FK → `purchase_order_line.id` |
| `exception_case_id` | `uuid` | 否 | `无` | FK → `exception_case.id` |
| `amount` | `numeric(24,6)` | 否 | `无` |  |
| `currency` | `char(3)` | 否 | `无` | FK → `currency.code` |
| `reason_code` | `varchar(32)` | 否 | `无` | CHECK 允许值：UNAUTHORIZED, EXCESS, UNKNOWN_SOURCE, REFUND_PENDING |
| `resolved_by` | `uuid` | 是 | `无` | FK → `app_user.id` |
| `resolution_evidence_id` | `uuid` | 是 | `无` | FK → `attachment.id` |
| `resolved_at` | `timestamptz` | 是 | `无` |  |
| `created_at` | `timestamptz` | 否 | `无` | 创建时间；默认 CURRENT_TIMESTAMP |

数据库约束与执行规则：

- PK：`id`；UQ：`(legal_entity_id, id)`。所有法人范围引用按 1 节生成同公司复合 FK；每个 FK 的来源列建立索引。
- CHECK：amount > 0；无授权/超额付款照实保存，已识别订单或预付范围阻断新支付与收尾；不是在授权 frozen_amount 上硬加超额。
- 无法识别范围时阻断对应供应商币种结算；供应商也未知则公司资金核对待办不可关闭。解除需独立复核、正确分配或实际流入凭证；未决金额不与同执行冻结重复统计。

<a id="table-exception_assignment"></a>

### `exception_assignment`：异常责任改派历史

所属模块：`workflows`；类型：业务记录表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | ---- | ---- | ---- | ---- |
| `id` | `uuid` | 否 | `无` | 主键 |
| `legal_entity_id` | `uuid` | 否 | `无` | FK → `legal_entity.id` |
| `exception_case_id` | `uuid` | 否 | `无` | FK → `exception_case.id` |
| `previous_assignee_id` | `uuid` | 是 | `无` | FK → `app_user.id` |
| `assignee_id` | `uuid` | 否 | `无` | FK → `app_user.id` |
| `assigned_by` | `uuid` | 否 | `无` | FK → `app_user.id` |
| `owner_module` | `varchar(32)` | 否 | `无` |  |
| `policy_id` | `uuid` | 是 | `无` | FK → `business_policy_version.id` |
| `reason` | `text` | 否 | `无` |  |
| `assigned_at` | `timestamptz` | 否 | `无` |  |
| `created_at` | `timestamptz` | 否 | `无` | 创建时间；默认 CURRENT_TIMESTAMP |

数据库约束与执行规则：

- PK：`id`；UQ：`(legal_entity_id, id)`。所有法人范围引用按 1 节生成同公司复合 FK；每个 FK 的来源列建立索引。
- 记录追加；锁异常工单，previous_assignee_id 必须匹配现负责人，再同事务写新负责人。初次指派 previous 可空，规则无匹配由模块负责人明确指派。

<a id="table-supplier_evaluation"></a>

### `supplier_evaluation`：供应商评价批次

所属模块：`suppliers`；类型：单据主表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | ---- | ---- | ---- | ---- |
| `id` | `uuid` | 否 | `无` | 主键；FK → `business_document.id` |
| `legal_entity_id` | `uuid` | 否 | `无` | FK → `legal_entity.id` |
| `supplier_id` | `uuid` | 否 | `无` | FK → `supplier.id` |
| `purchase_order_id` | `uuid` | 否 | `无` | FK → `purchase_order.id` |
| `policy_id` | `uuid` | 否 | `无` | FK → `business_policy_version.id` |
| `published_by` | `uuid` | 是 | `无` | FK → `app_user.id` |
| `published_at` | `timestamptz` | 是 | `无` |  |
| `publication_snapshot` | `jsonb` | 是 | `无` |  |
| `created_at` | `timestamptz` | 否 | `无` | 创建时间；默认 CURRENT_TIMESTAMP |

数据库约束与执行规则：

- PK：`id`；UQ：`(legal_entity_id, id)`。所有法人范围引用按 1 节生成同公司复合 FK；每个 FK 的来源列建立索引。
- 主状态、内容版本及审批沿用通用单据；主表 ID 等于 business_document.id，类型必须匹配。
- 主状态：DRAFT, IN_REVIEW, PUBLISHED, RETURNED；发布快照包含维度、证据、规则、原始分数、N/A、归一权重及客观指标。缺必需项不得发布，评分修订创建新版本重新复核。
- 发布人为有权限供应商管理员，不代替各角色评分；未发布不阻止订单关闭。

<a id="table-supplier_evaluation_dimension"></a>

### `supplier_evaluation_dimension`：角色评价明细

所属模块：`suppliers`；类型：业务记录表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | ---- | ---- | ---- | ---- |
| `id` | `uuid` | 否 | `无` | 主键 |
| `legal_entity_id` | `uuid` | 否 | `无` | FK → `legal_entity.id` |
| `supplier_evaluation_id` | `uuid` | 否 | `无` | FK → `supplier_evaluation.id` |
| `revision` | `integer` | 否 | `无` |  |
| `dimension_code` | `varchar(64)` | 否 | `无` |  |
| `owner_role` | `varchar(32)` | 否 | `无` | CHECK 允许值：WAREHOUSE, QUALITY, ACCEPTANCE, FINANCE, BUYER, SYSTEM |
| `evaluator_id` | `uuid` | 是 | `无` | FK → `app_user.id` |
| `applicability` | `varchar(32)` | 否 | `无` | CHECK 允许值：APPLICABLE, NOT_APPLICABLE |
| `score` | `numeric(5,2)` | 是 | `无` |  |
| `weight` | `numeric(9,6)` | 否 | `无` |  |
| `required` | `boolean` | 否 | `无` |  |
| `reason` | `text` | 否 | `无` |  |
| `evidence_id` | `uuid` | 是 | `无` | FK → `attachment.id` |
| `source_snapshot` | `jsonb` | 是 | `无` |  |
| `created_at` | `timestamptz` | 否 | `无` | 创建时间；默认 CURRENT_TIMESTAMP |

数据库约束与执行规则：

- PK：`id`；UQ：`(legal_entity_id, id)`。所有法人范围引用按 1 节生成同公司复合 FK；每个 FK 的来源列建立索引。
- UQ：(supplier_evaluation_id, revision, dimension_code)；FK：`(supplier_evaluation_id, revision) → document_revision(document_id, revision)`；CHECK：weight >= 0 AND (score IS NULL OR score BETWEEN 0 AND 100)。
- NOT_APPLICABLE 必须 score 空并有理由；APPLICABLE 的空分是未完成而非零。owner_role=SYSTEM 的值来自确定版本事实快照，不允许人工覆盖；其他角色提交必须 evaluator_id 非空并验证角色和来源范围。维度及权重来自不可变规则，实际用户只能改自己有权限的维度。


<a id="table-arrival_custody_movement"></a>

### `arrival_custody_movement`：到货保管数量流水

所属模块：`inventory`；类型：业务记录表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | ---- | ---- | ---- | ---- |
| `id` | `uuid` | 否 | `无` | 主键 |
| `legal_entity_id` | `uuid` | 否 | `无` | FK → `legal_entity.id` |
| `arrival_line_id` | `uuid` | 否 | `无` | FK → `arrival_line.id` |
| `inspection_id` | `uuid` | 是 | `无` | FK → `arrival_inspection.id` |
| `source_inspection_id` | `uuid` | 是 | `无` | FK → `arrival_inspection.id` |
| `handoff_id` | `uuid` | 是 | `无` | FK → `arrival_handoff.id` |
| `receipt_line_id` | `uuid` | 是 | `无` | FK → `receipt_line.id` |
| `correction_request_id` | `uuid` | 是 | `无` | FK → `correction_request.id` |
| `from_bucket` | `varchar(32)` | 否 | `无` | CHECK 允许值：EXTERNAL, PENDING, QUALIFIED, UNQUALIFIED, POSTED |
| `to_bucket` | `varchar(32)` | 否 | `无` | CHECK 允许值：PENDING, QUALIFIED, UNQUALIFIED, REJECTED, RETURNED, POSTED |
| `quantity` | `numeric(20,6)` | 否 | `无` |  |
| `posting_key` | `varchar(160)` | 否 | `无` |  |
| `reversal_of_id` | `uuid` | 是 | `无` | FK → `arrival_custody_movement.id` |
| `created_at` | `timestamptz` | 否 | `无` | 创建时间；默认 CURRENT_TIMESTAMP |

数据库约束与执行规则：

- PK：`id`；UQ：`(legal_entity_id, id)`。所有法人范围引用按 1 节生成同公司复合 FK；每个 FK 的来源列建立索引。
- CHECK：quantity > 0 AND from_bucket <> to_bucket；UQ：(legal_entity_id, posting_key)；UQ：(reversal_of_id)。流水仅追加，与 arrival_line 余额同事务维护。
- EXTERNAL → PENDING 仅原始到货一次；其余恰有一个动作来源 inspection_id/handoff_id/receipt_line_id/correction_request_id，由显式 CHECK 检查 num_nonnulls。
- 消耗 QUALIFIED/UNQUALIFIED 时 source_inspection_id 必填；按来源检验分批拆流水，持锁校验该检验产出减累计消耗非负，复检不能再次使用已入库量。初检从 PENDING，source_inspection_id 空。
- 收货消耗合格量转 POSTED；合法收货冲销引用原 movement 且仅 POSTED → QUALIFIED，保留 source_inspection_id，恢复真实保管；原流水不能重复冲销。

<a id="table-change_package_line"></a>

### `change_package_line`：订单变更明细承接

所属模块：`procurement`；类型：业务记录表。

| 字段 | PostgreSQL 类型 | 可空 | 默认值 | 含义与关联 |
| ---- | ---- | ---- | ---- | ---- |
| `id` | `uuid` | 否 | `无` | 主键 |
| `legal_entity_id` | `uuid` | 否 | `无` | FK → `legal_entity.id` |
| `package_item_id` | `uuid` | 否 | `无` | FK → `change_package_item.id` |
| `old_order_line_id` | `uuid` | 是 | `无` | FK → `purchase_order_line.id` |
| `new_order_line_id` | `uuid` | 是 | `无` | FK → `purchase_order_line.id` |
| `scope_delta` | `numeric(24,6)` | 否 | `无` |  |
| `gross_delta` | `numeric(24,6)` | 否 | `无` |  |
| `reason` | `text` | 否 | `无` |  |
| `created_at` | `timestamptz` | 否 | `无` | 创建时间；默认 CURRENT_TIMESTAMP |

数据库约束与执行规则：

- PK：`id`；UQ：`(legal_entity_id, id)`。所有法人范围引用按 1 节生成同公司复合 FK；每个 FK 的来源列建立索引。
- CHECK：num_nonnulls(old_order_line_id, new_order_line_id) >= 1；旧/新行分别属于包目标订单 base/proposed 版本；同包旧行与新行分别唯一（非空时）。
- 应用保留已履约旧行及其预算/发票/实付来源，新行只承接未执行范围；若仅改未来日期，旧履约仍引用原版本。新行预算重新按 order_source_allocation/order_budget_allocation 分配，旧行未履约占用按差额原子释放，不能再累计一份历史采购范围。
- scope_delta 和 gross_delta 是新旧未执行部分差额，不是覆盖历史总量；跨行承接及累计执行下限在目标订单锁内校验。合同明细版本变化仍由 change_package_item 管理，固定合同校验订单承接总范围与合同一致。

## 11. 关键关系与跨表约束

| 关系              | 基数与引用                                     | 数据库可直接保证            | 事务中必须校验                           |
| ----------------- | ---------------------------------------------- | --------------------------- | ---------------------------------------- |
| 申请 → 申请明细   | 1:N，purchase_request_line.purchase_request_id | FK、当前版本内行号唯一      | 按稳定需求项跨版本累计；新下单只用当前批准且未冻结范围                     |
| 定标 → 订单明细   | 1:N，purchase_order_line.award_line_id         | FK                          | 有效订单量不超过获批分配                 |
| 订单行 → 收货行   | 1:N，receipt_line.order_line_id                | FK                          | 收货与取消量不能超过订单义务             |
| 收货行 → 退货行   | 1:N，return_line.receipt_line_id               | FK                          | 可退量、仓库库存与未分配发票数量         |
| 发票行 ↔ 收货行   | M:N，通过 invoice_allocation                   | 两侧 FK、活跃配对唯一       | HELD+ACTIVE 分配量不超净收货             |
| 付款 ↔ 发票       | M:N，通过 payment_allocation → payment_execution → payment_authorization → payment_application_line                   | 两侧 FK、内容版本内配对唯一 | 申请占用含执行冻结，实付与核销累计不超应付                  |
| 订单行 → 预算占用 | 1:N，经 order_budget_allocation → budget_reservation       | 分配 UQ+FK | 每个订单预算分配各一笔占用，与订单批准原子提交             |
| 单据 → 审批实例   | 1:N，按 revision 区分                          | 版本 FK、活跃实例部分唯一   | 具体审批人、职责分离、最后节点业务生效   |
| 原流水 → 冲销流水 | 1:0..1，自引用 reversal_of_id                  | FK+UQ                       | 同账户/物料、金额/数量完全反向、来源一致 |

对于同法人但不同父单据的关联，还须验证来源链：例如报价行确实属于邀请的询价、收货行确实属于主表订单、发票与付款供应商相同。这些在领域服务持锁后校验；不把只校验单列 ID 存在的外键描述成已校验完整业务关系。

## 12. 账本与汇总核对

| 汇总字段                       | 来源口径                                                                             |
| ------------------------------ | ------------------------------------------------------------------------------------ |
| stock_balance.quantity         | 同法人、仓库、物料的 stock_ledger.delta_quantity 合计                                |
| budget_account.approved_amount | 同账户 budget_ledger.quota_delta 合计                                                |
| budget_account.preencumbered_amount | 同账户 budget_ledger.preencumbered_delta 合计，等于 budget_preencumbrance.remaining_amount 合计 |
| budget_account.reserved_amount | 同账户 budget_ledger.reserved_delta 合计，并与 reservation.remaining_amount 合计相符 |
| budget_account.executed_amount | 同账户 budget_ledger.executed_delta 合计                                             |
| 已收量/正常退货量              | 生效的收货/退货行及对应冲销，不统计草稿或历史替代版本                                |
| 已开票数量                     | invoice_allocation 中 ACTIVE allocated_scope；HELD 另列但同样占可分配量                         |
| 已付款金额                     | payment_allocation 中 ACTIVE 金额；HELD 复用执行冻结，不重复扣来源可付额                           |

逐行金额按业务设计舍入，主表金额等于当前内容版本明细之和。汇总一致性不通过跨表 CHECK 实现：同事务更新、来源唯一约束、定期对账三者共同保证。发现不一致记录异常，不能直接把余额覆盖为期望值。

## 13. 建表顺序、数据保护与验证

1. 先定义全部表与主键/唯一键，再分批加入外键；迁移采用确定顺序处理合法的前向引用。公共主数据先初始化，随后建立业务单据与审批、库存财务、集成记录。
2. 为通用登记/内容版本的创建循环设置指定的延迟外键，增加领域类型与主表存在性、冻结内容保护、流水禁止改写的约束触发器；不是把所有外键都改成延迟检查。
3. 初始化发布计数器、法人、部门、单位、角色、审批与业务规则。账号密码经安全初始化过程设置；不在 SQL 或文档中提供固定生产密码。
4. 工作流事务角色仅通过受控服务更新数据；助手没有业务库权限。迁移账号与运行账号分离，运行账号无 DDL 权限。
5. 首期不分区、不建设向量表；按真实查询计划合并候选索引。审计、变化、流水增长后再评估归档与分区，保留历史业务引用。
6. 本文所有设计约束最终以 Alembic 迁移和 PostgreSQL 集成测试落实。文档校验不等于数据库执行验证；目前尚未建库或运行 SQL 迁移。

建库后的必测项：跨法人外键拒绝、重复业务键拒绝、主明细版本正确、历史明细不可修改、退回重提版本分离、同键重复动作只生效一次、并发预算和收货/发票分配不超量、流水冲销唯一、变化发布晚提交不漏记录。业务端到端场景沿用业务设计中的 A01—A13。
