# 采购业务平台数据库详细设计

版本：数据库设计草案 v1（待按业务基线 v3 重构）；适用范围：`platform/` 的 PostgreSQL 业务库。本文是字段级草案，尚未生成或执行数据库迁移。

## 当前适用范围与重构清单

当前业务需求以[采购流程与业务动作设计](PROCUREMENT.md)为准。下方 v1 字段目录尚未覆盖合同直接履约、RFP/招标、多币种、非实物验收及付款申请；其中 CNY、固定两位金额、强制物料/仓库/订单来源等限制是待替换的旧设计，不再是产品范围约束。此处保留字段草案用于逐项重构，不提供兼容接口或旧表迁移方案。

| 重构对象 | 必须落实的字段与约束 | 受影响的现有表或关联 |
| -------- | ------------------ | -------------------- |
| 公司与币种 | 公司本位币、币种小数位、汇率方向/来源/日期/精度、单据及预算换算快照 | `legal_entity`、`business_policy_version`、所有金额字段及 CNY CHECK |
| 采购标的 | 实物/服务/许可/订阅类型、非编码描述、按数量或金额计量、成果/期间/里程碑验收依据；仅实物要求物料及仓库 | `purchase_request_line`、征集/报价/授予/订单明细 |
| 预算分配 | 同公司明细按预算项拆分；原币及预算币金额，分配合计校验 | `budget_account`、`budget_reservation`、`budget_ledger`；替换订单头单预算限制 |
| 采购任务与方式 | 申请明细分配、采购员、交接历史、采购包、方式、规则版本、适用协议 | 新增任务及方式对象，调整 `purchase_method_exception` 来源 |
| 征集与响应 | RFQ/RFP/招标类型、征集版本、邀请、响应版本、截止、澄清补遗及评审标准 | 重构 `rfq`、`rfq_line`、`rfq_invitation`、`quotation`、`quotation_line`，不新增旧接口别名 |
| 评审与授予 | 评审人及标准版本、证据、获选响应和范围分配、目标合同或订单 | `award`、`award_line` 及新评审记录 |
| 合同及协议 | 类型、执行模式、公司、供应商、币种、有效期、签署事实、明细及版本、额度与履约计划 | 新增合同、签署及明细对象；订单引用协议明细和版本 |
| 采购承诺与履约来源 | 为直接履约合同或订单的明细建立可约束的唯一履约来源；预算占用不能重复 | 预算、收退货、非实物验收、发票不再强制仅引用 `purchase_order_line` |
| 变更单 | 目标对象、基准版本、差额、前后快照、审批与双方确认、唯一应用结果 | 新增变更对象，调整原“取消余量再新建订单”的唯一变更路径 |
| 交付及非实物验收 | 通知不记账；验收范围、期间、数量或金额、凭证、确认人与冲销链 | 新增通知及验收主明细，扩展履约进度与发票依据 |
| 发票匹配 | 订单或合同来源、收货或非实物验收分配、计量方式、币种与金额口径 | `invoice_line`、`invoice_allocation`、`invoice_match_run` 及可开票额计算 |
| 付款申请与授权 | 应付分配、申请占用、批准版本、收款信息、授权余额和剩余额撤销；同授权实付不重复扣可付额 | 新增付款申请/分配/授权实体；`payment_record`、`payment_allocation` 引用授权 |
| 通用登记及 API | 新单据类型、状态、审批处理器、幂等动作、审计和变化事件 | `business_document` 类型映射、`document_revision`、审批、权限及业务接口 |

下一版字段字典应对每个新增实体逐字段定义类型、可空性、默认值、PK/FK/UQ/CHECK 和索引；多种履约来源不能仅用无法约束的 `source_type + source_id` 字符串组合，需统一承诺明细键或可验证的类型关联。所有引用仍须保证同公司及正确版本。上述清单是重构要求，尚不是已完成的物理表设计。

业务流程和权限要求参见[业务平台设计](DESIGN.md)。本文每个表名代表一张独立物理表，每个字段单独一行；字段定义、约束和生命周期在此维护，业务设计第 4 节保留单表导航。

## 1. 阅读方式与物理约定

- **主表**保存一张单据的共同信息；**明细表**保存该单据的多条物料或分配记录；`xxx_line` 是明细表的命名习惯，不是列名或主表的内嵌 JSON。
- **关联表**表达用户与角色、供应商与品类等多对多关系；**流水表**追加记录一次数量或金额变化；**余额表**是同事务维护的汇总。
- 表名使用单数 `snake_case`，首期统一在业务库 `public` schema，按模块确定代码所有者。表名 `app_user` 对应概念设计中的用户实体。助手不获得此库账号。
- 字段列“可空”为“否”表示 NOT NULL；默认值“无”表示必须由用例提供（可空字段省略则为 NULL）。UUID 由应用生成；领域主表的 ID 沿用通用单据 ID。所有表的字段都已展开，未省略隐式公共列。
- 字符串状态使用 `varchar + CHECK`，允许值以字段说明为准；通用单据状态按类型验证。布尔值、金额和数量不得用字符串状态列代替。
- 金额 `numeric(20,2)`，单价/数量 `numeric(20,6)`，税率 `numeric(9,6)`；JSON API 用十进制字符串，服务使用 Decimal。PostgreSQL `numeric` 可用于精确数值计算。[数值类型说明](https://www.postgresql.org/docs/current/datatype-numeric.html)
- 时间用 `timestamptz`，日期用 `date`；新增记录 `created_at` 默认数据库当前时间，`updated_at` 由应用显式更新，不假定 PostgreSQL 自动更新时间。
- `PK` 表示主键，`UQ` 表示唯一约束，`FK` 表示外键。UUID `id` 默认主键。所有外键采用 ON DELETE RESTRICT、ON UPDATE RESTRICT；不用级联删除正式数据。
- 有 `legal_entity_id` 的表额外建立 `UQ(legal_entity_id, id)`。其引用其他法人范围表的字段使用复合 FK `(legal_entity_id, target_id)`，指向 `(legal_entity_id, id)`，使“同法人”由数据库保证。可空关联只允许 target_id 空，法人列始终非空。
- 唯一约束自动提供索引；本文普通索引是候选实现索引，重复前缀在实施时合并，不重复创建主键/唯一索引。外键字段索引按查询与删除检查需求列出。
- 跨行总量、树无环、领域状态迁移不能用普通单行 CHECK 代替，须按业务设计在事务中锁定来源再验证。FK/唯一约束和 CHECK 的能力边界见 [PostgreSQL 约束文档](https://www.postgresql.org/docs/current/ddl-constraints.html)。

### 1.1 主表与明细的具体例子

| 表                      | 记录              | 主要值                                                |
| ----------------------- | ----------------- | ----------------------------------------------------- |
| `business_document`     | 采购申请登记 1 条 | 单号 PR-000001、申请人、部门、当前 revision=1         |
| `purchase_request`      | 申请主表 1 条     | id 与上述登记相同，保存采购用途、预算项               |
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
| `RFQ`                       | `rfq`                       | DRAFT, OPEN, CLOSED, UNSEALED, CANCELLED                                    |
| `PURCHASE_METHOD_EXCEPTION` | `purchase_method_exception` | DRAFT, IN_REVIEW, APPROVED, RETURNED, REJECTED, CANCELLED                   |
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
| `PAYMENT_RECORD`            | `payment_record`            | DRAFT, IN_REVIEW, CONFIRMED, RETURNED, REJECTED, REVERSED                   |
| `CORRECTION_REQUEST`        | `correction_request`        | DRAFT, IN_REVIEW, APPROVED, RETURNED, REJECTED, CANCELLED                   |
| `EXCEPTION_CASE`            | `exception_case`            | OPEN, IN_PROGRESS, RESOLVED, CANCELLED                                      |

报价 SEALED 表示截止前盲收登记且明细加密封存的状态；RECORDED 表示截止后集中开标解密并冻结的有效报价版本，SUPERSEDED 表示被新版本替代但历史定标引用仍保留。已过账库存单据全部来源流水完成反向冲销后才变为 REVERSED；正常退货不把原收货单变为 REVERSED。仅冲销部分完整来源流水时原主状态保持 POSTED，净数量按行和流水派生。

## 2. 单表目录

| 所属模块       | 物理表                                                                | 中文名称           | 类型         |
| -------------- | --------------------------------------------------------------------- | ------------------ | ------------ |
| `foundation`   | [legal_entity](#table-legal_entity)                                   | 法人主体           | 主数据表     |
| `foundation`   | [department](#table-department)                                       | 部门               | 主数据表     |
| `foundation`   | [app_user](#table-app_user)                                           | 用户               | 身份表       |
| `foundation`   | [role](#table-role)                                                   | 角色               | 权限表       |
| `foundation`   | [user_role](#table-user_role)                                         | 用户角色关系       | 关联表       |
| `foundation`   | [role_permission](#table-role_permission)                             | 角色动作权限       | 关联表       |
| `foundation`   | [user_scope](#table-user_scope)                                       | 用户数据范围       | 权限表       |
| `foundation`   | [auth_session](#table-auth_session)                                   | 登录会话           | 身份表       |
| `foundation`   | [service_client](#table-service_client)                               | 服务客户端         | 身份表       |
| `foundation`   | [delegation](#table-delegation)                                       | 助手委托           | 身份表       |
| `foundation`   | [unit](#table-unit)                                                   | 计量单位           | 主数据表     |
| `foundation`   | [category](#table-category)                                           | 物料品类           | 主数据表     |
| `foundation`   | [material](#table-material)                                           | 物料               | 主数据表     |
| `foundation`   | [warehouse](#table-warehouse)                                         | 仓库               | 主数据表     |
| `foundation`   | [business_document](#table-business_document)                         | 通用业务单据登记   | 单据登记表   |
| `foundation`   | [document_revision](#table-document_revision)                         | 单据内容版本       | 版本表       |
| `foundation`   | [attachment](#table-attachment)                                       | 附件               | 文件元数据表 |
| `foundation`   | [audit_log](#table-audit_log)                                         | 业务审计           | 追加记录表   |
| `foundation`   | [business_policy_version](#table-business_policy_version)             | 业务规则配置版本   | 规则表       |
| `approvals`    | [approval_rule_version](#table-approval_rule_version)                 | 审批规则版本       | 规则表       |
| `approvals`    | [approval_instance](#table-approval_instance)                         | 审批实例           | 审批运行表   |
| `approvals`    | [approval_step](#table-approval_step)                                 | 审批节点           | 审批明细表   |
| `approvals`    | [approval_decision](#table-approval_decision)                         | 审批决定与改派记录 | 追加记录表   |
| `integrations` | [idempotency_record](#table-idempotency_record)                       | 接口幂等结果       | 集成记录表   |
| `integrations` | [business_change](#table-business_change)                             | 业务变化           | 追加记录表   |
| `integrations` | [publication_counter](#table-publication_counter)                     | 变化发布计数器     | 单例协调表   |
| `integrations` | [change_publication](#table-change_publication)                       | 已发布变化         | 追加记录表   |
| `suppliers`    | [supplier](#table-supplier)                                           | 供应商档案         | 主数据表     |
| `suppliers`    | [supplier_category](#table-supplier_category)                         | 供应商品类关系     | 关联表       |
| `suppliers`    | [supplier_qualification](#table-supplier_qualification)               | 供应商资质         | 业务明细表   |
| `suppliers`    | [supplier_admission](#table-supplier_admission)                       | 供应商准入申请     | 单据主表     |
| `suppliers`    | [supplier_status_request](#table-supplier_status_request)             | 供应商状态变更申请 | 单据主表     |
| `suppliers`    | [supplier_performance_snapshot](#table-supplier_performance_snapshot) | 供应商绩效快照     | 统计快照表   |
| `procurement`  | [purchase_request](#table-purchase_request)                           | 采购申请主表       | 单据主表     |
| `procurement`  | [purchase_request_line](#table-purchase_request_line)                 | 采购申请明细表     | 单据明细表   |
| `procurement`  | [rfq](#table-rfq)                                                     | 询价主表           | 单据主表     |
| `procurement`  | [purchase_method_exception](#table-purchase_method_exception)         | 采购方式例外申请   | 单据主表     |
| `procurement`  | [rfq_line](#table-rfq_line)                                           | 询价明细表         | 单据明细表   |
| `procurement`  | [rfq_invitation](#table-rfq_invitation)                               | 询价邀请关系       | 关联表       |
| `procurement`  | [quotation](#table-quotation)                                         | 供应商报价主表     | 单据主表     |
| `procurement`  | [quotation_line](#table-quotation_line)                               | 报价明细表         | 单据明细表   |
| `procurement`  | [award](#table-award)                                                 | 定标主表           | 单据主表     |
| `procurement`  | [award_line](#table-award_line)                                       | 定标明细表         | 单据明细表   |
| `procurement`  | [purchase_order](#table-purchase_order)                               | 采购订单主表       | 单据主表     |
| `procurement`  | [purchase_order_line](#table-purchase_order_line)                     | 采购订单明细表     | 单据明细表   |
| `procurement`  | [order_close_request](#table-order_close_request)                     | 订单余量关闭主表   | 单据主表     |
| `procurement`  | [order_close_line](#table-order_close_line)                           | 订单余量关闭明细表 | 单据明细表   |
| `inventory`    | [receipt](#table-receipt)                                             | 采购收货主表       | 单据主表     |
| `inventory`    | [receipt_line](#table-receipt_line)                                   | 采购收货明细表     | 单据明细表   |
| `inventory`    | [return_order](#table-return_order)                                   | 采购退货主表       | 单据主表     |
| `inventory`    | [return_line](#table-return_line)                                     | 采购退货明细表     | 单据明细表   |
| `inventory`    | [stock_issue](#table-stock_issue)                                     | 库存领用主表       | 单据主表     |
| `inventory`    | [stock_issue_line](#table-stock_issue_line)                           | 库存领用明细表     | 单据明细表   |
| `inventory`    | [stock_adjustment](#table-stock_adjustment)                           | 库存调整主表       | 单据主表     |
| `inventory`    | [stock_adjustment_line](#table-stock_adjustment_line)                 | 库存调整明细表     | 单据明细表   |
| `inventory`    | [stock_balance](#table-stock_balance)                                 | 库存余额           | 汇总表       |
| `inventory`    | [stock_ledger](#table-stock_ledger)                                   | 库存流水           | 追加账本表   |
| `finance`      | [budget_account](#table-budget_account)                               | 预算账户           | 汇总表       |
| `finance`      | [budget_adjustment](#table-budget_adjustment)                         | 预算调整申请       | 单据主表     |
| `finance`      | [budget_reservation](#table-budget_reservation)                       | 订单行预算占用     | 余额明细表   |
| `finance`      | [budget_ledger](#table-budget_ledger)                                 | 预算流水           | 追加账本表   |
| `finance`      | [invoice](#table-invoice)                                             | 发票主表           | 单据主表     |
| `finance`      | [invoice_line](#table-invoice_line)                                   | 发票明细表         | 单据明细表   |
| `finance`      | [invoice_match_run](#table-invoice_match_run)                         | 发票匹配批次       | 分析快照表   |
| `finance`      | [invoice_allocation](#table-invoice_allocation)                       | 发票收货分配       | 分配明细表   |
| `finance`      | [payment_record](#table-payment_record)                               | 付款登记主表       | 单据主表     |
| `finance`      | [payment_allocation](#table-payment_allocation)                       | 付款发票分配明细   | 单据明细表   |
| `workflows`    | [correction_request](#table-correction_request)                       | 跨模块纠错申请     | 单据主表     |
| `workflows`    | [exception_case](#table-exception_case)                               | 异常工单           | 单据主表     |

## 3. 公共基础模块

<a id="table-legal_entity"></a>

### `legal_entity`：法人主体

所属模块：`foundation`；类型：主数据表。

| 字段         | PostgreSQL 类型 | 可空 | 默认值              | 含义与关联               |
| ------------ | --------------- | ---- | ------------------- | ------------------------ |
| `id`         | `uuid`          | 否   | `无`                | 主键；应用生成 UUID；PK  |
| `code`       | `varchar(32)`   | 否   | `无`                | 法人编码                 |
| `name`       | `varchar(200)`  | 否   | `无`                | 法人名称                 |
| `status`     | `varchar(32)`   | 否   | `'ACTIVE'`          | 允许值：ACTIVE, INACTIVE |
| `created_at` | `timestamptz`   | 否   | `CURRENT_TIMESTAMP` | 创建时间                 |

数据库约束与索引：

- PK：`id`。
- UQ：`(code)`。

<a id="table-department"></a>

### `department`：部门

所属模块：`foundation`；类型：主数据表。

| 字段              | PostgreSQL 类型 | 可空 | 默认值              | 含义与关联                       |
| ----------------- | --------------- | ---- | ------------------- | -------------------------------- |
| `id`              | `uuid`          | 否   | `无`                | 主键；应用生成 UUID；PK          |
| `legal_entity_id` | `uuid`          | 否   | `无`                | 所属法人；FK → `legal_entity.id` |
| `parent_id`       | `uuid`          | 是   | `无`                | 上级部门；FK → `department.id`   |
| `code`            | `varchar(32)`   | 否   | `无`                | 部门编码                         |
| `name`            | `varchar(100)`  | 否   | `无`                | 部门名称                         |
| `status`          | `varchar(32)`   | 否   | `'ACTIVE'`          | 允许值：ACTIVE, INACTIVE         |
| `created_at`      | `timestamptz`   | 否   | `CURRENT_TIMESTAMP` | 创建时间                         |

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

| 字段              | PostgreSQL 类型 | 可空 | 默认值              | 含义与关联                       |
| ----------------- | --------------- | ---- | ------------------- | -------------------------------- |
| `id`              | `uuid`          | 否   | `无`                | 主键；应用生成 UUID；PK          |
| `legal_entity_id` | `uuid`          | 否   | `无`                | 所属法人；FK → `legal_entity.id` |
| `department_id`   | `uuid`          | 否   | `无`                | 默认部门；FK → `department.id`   |
| `login_name`      | `varchar(100)`  | 否   | `无`                | 规范化小写登录名                 |
| `display_name`    | `varchar(100)`  | 否   | `无`                | 显示名称                         |
| `password_hash`   | `text`          | 否   | `无`                | 密码散列                         |
| `status`          | `varchar(32)`   | 否   | `'ACTIVE'`          | 允许值：ACTIVE, DISABLED         |
| `auth_version`    | `integer`       | 否   | `1`                 | 权限/会话撤销版本                |
| `updated_at`      | `timestamptz`   | 否   | `无`                | 最后修改时间                     |
| `created_at`      | `timestamptz`   | 否   | `CURRENT_TIMESTAMP` | 创建时间                         |

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

| 字段              | PostgreSQL 类型 | 可空 | 默认值              | 含义与关联                       |
| ----------------- | --------------- | ---- | ------------------- | -------------------------------- |
| `id`              | `uuid`          | 否   | `无`                | 主键；应用生成 UUID；PK          |
| `legal_entity_id` | `uuid`          | 否   | `无`                | 所属法人；FK → `legal_entity.id` |
| `code`            | `varchar(32)`   | 否   | `无`                | 角色编码                         |
| `name`            | `varchar(100)`  | 否   | `无`                | 角色名称                         |
| `status`          | `varchar(32)`   | 否   | `'ACTIVE'`          | 允许值：ACTIVE, INACTIVE         |
| `created_at`      | `timestamptz`   | 否   | `CURRENT_TIMESTAMP` | 创建时间                         |

数据库约束与索引：

- PK：`id`。
- UQ：`(legal_entity_id, code)`。
- UQ：`(legal_entity_id, id)`。
- FK：`(legal_entity_id) → legal_entity(id)`。

<a id="table-user_role"></a>

### `user_role`：用户角色关系

所属模块：`foundation`；类型：关联表。

| 字段              | PostgreSQL 类型 | 可空 | 默认值              | 含义与关联                       |
| ----------------- | --------------- | ---- | ------------------- | -------------------------------- |
| `id`              | `uuid`          | 否   | `无`                | 主键；应用生成 UUID；PK          |
| `legal_entity_id` | `uuid`          | 否   | `无`                | 所属法人；FK → `legal_entity.id` |
| `user_id`         | `uuid`          | 否   | `无`                | 用户；FK → `app_user.id`         |
| `role_id`         | `uuid`          | 否   | `无`                | 角色；FK → `role.id`             |
| `created_at`      | `timestamptz`   | 否   | `CURRENT_TIMESTAMP` | 创建时间                         |

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

| 字段              | PostgreSQL 类型 | 可空 | 默认值              | 含义与关联                       |
| ----------------- | --------------- | ---- | ------------------- | -------------------------------- |
| `id`              | `uuid`          | 否   | `无`                | 主键；应用生成 UUID；PK          |
| `legal_entity_id` | `uuid`          | 否   | `无`                | 所属法人；FK → `legal_entity.id` |
| `role_id`         | `uuid`          | 否   | `无`                | 角色；FK → `role.id`             |
| `permission_code` | `varchar(120)`  | 否   | `无`                | 动作权限编码                     |
| `created_at`      | `timestamptz`   | 否   | `CURRENT_TIMESTAMP` | 创建时间                         |

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

| 字段               | PostgreSQL 类型 | 可空 | 默认值              | 含义与关联                                  |
| ------------------ | --------------- | ---- | ------------------- | ------------------------------------------- |
| `id`               | `uuid`          | 否   | `无`                | 主键；应用生成 UUID；PK                     |
| `legal_entity_id`  | `uuid`          | 否   | `无`                | 所属法人；FK → `legal_entity.id`            |
| `user_id`          | `uuid`          | 否   | `无`                | 用户；FK → `app_user.id`                    |
| `scope_type`       | `varchar(32)`   | 否   | `'SELF'`            | 允许值：ENTITY, DEPARTMENT, WAREHOUSE, SELF |
| `department_id`    | `uuid`          | 是   | `无`                | 授权部门；FK → `department.id`              |
| `warehouse_id`     | `uuid`          | 是   | `无`                | 授权仓库；FK → `warehouse.id`               |
| `include_children` | `boolean`       | 否   | `false`             | 是否包含子部门                              |
| `created_at`       | `timestamptz`   | 否   | `CURRENT_TIMESTAMP` | 创建时间                                    |

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

| 字段              | PostgreSQL 类型 | 可空 | 默认值              | 含义与关联                       |
| ----------------- | --------------- | ---- | ------------------- | -------------------------------- |
| `id`              | `uuid`          | 否   | `无`                | 主键；应用生成 UUID；PK          |
| `legal_entity_id` | `uuid`          | 否   | `无`                | 所属法人；FK → `legal_entity.id` |
| `user_id`         | `uuid`          | 否   | `无`                | 用户；FK → `app_user.id`         |
| `token_hash`      | `varchar(128)`  | 否   | `无`                | 会话令牌摘要                     |
| `csrf_hash`       | `varchar(128)`  | 否   | `无`                | CSRF 秘密摘要                    |
| `auth_version`    | `integer`       | 否   | `无`                | 签发时用户授权版本               |
| `expires_at`      | `timestamptz`   | 否   | `无`                | 失效时间                         |
| `revoked_at`      | `timestamptz`   | 是   | `无`                | 撤销时间                         |
| `last_seen_at`    | `timestamptz`   | 是   | `无`                | 最近访问时间                     |
| `created_at`      | `timestamptz`   | 否   | `CURRENT_TIMESTAMP` | 创建时间                         |

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

| 字段              | PostgreSQL 类型 | 可空 | 默认值              | 含义与关联                       |
| ----------------- | --------------- | ---- | ------------------- | -------------------------------- |
| `id`              | `uuid`          | 否   | `无`                | 主键；应用生成 UUID；PK          |
| `legal_entity_id` | `uuid`          | 否   | `无`                | 所属法人；FK → `legal_entity.id` |
| `client_code`     | `varchar(32)`   | 否   | `无`                | 服务编码                         |
| `credential_hash` | `text`          | 否   | `无`                | 服务凭证摘要                     |
| `allowed_actions` | `jsonb`         | 否   | `'[]'::jsonb`       | 可委托动作数组                   |
| `status`          | `varchar(32)`   | 否   | `'ACTIVE'`          | 允许值：ACTIVE, DISABLED         |
| `created_at`      | `timestamptz`   | 否   | `CURRENT_TIMESTAMP` | 创建时间                         |

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

| 字段                 | PostgreSQL 类型 | 可空 | 默认值              | 含义与关联                             |
| -------------------- | --------------- | ---- | ------------------- | -------------------------------------- |
| `id`                 | `uuid`          | 否   | `无`                | 主键；应用生成 UUID；PK                |
| `legal_entity_id`    | `uuid`          | 否   | `无`                | 所属法人；FK → `legal_entity.id`       |
| `user_id`            | `uuid`          | 否   | `无`                | 授权用户；FK → `app_user.id`           |
| `service_client_id`  | `uuid`          | 否   | `无`                | 获授权客户端；FK → `service_client.id` |
| `session_id`         | `uuid`          | 否   | `无`                | 授权来源会话；FK → `auth_session.id`   |
| `target_document_id` | `uuid`          | 是   | `无`                | 限定单据；FK → `business_document.id`  |
| `token_hash`         | `varchar(128)`  | 否   | `无`                | 不透明委托令牌摘要                     |
| `actions`            | `jsonb`         | 否   | `'[]'::jsonb`       | 委托动作数组                           |
| `scope_snapshot`     | `jsonb`         | 否   | `'{}'::jsonb`       | 组织/仓库范围快照                      |
| `expires_at`         | `timestamptz`   | 否   | `无`                | 到期时间                               |
| `revoked_at`         | `timestamptz`   | 是   | `无`                | 撤销时间                               |
| `created_at`         | `timestamptz`   | 否   | `CURRENT_TIMESTAMP` | 创建时间                               |

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

| 字段              | PostgreSQL 类型 | 可空 | 默认值              | 含义与关联                       |
| ----------------- | --------------- | ---- | ------------------- | -------------------------------- |
| `id`              | `uuid`          | 否   | `无`                | 主键；应用生成 UUID；PK          |
| `legal_entity_id` | `uuid`          | 否   | `无`                | 所属法人；FK → `legal_entity.id` |
| `code`            | `varchar(32)`   | 否   | `无`                | 编码                             |
| `name`            | `varchar(100)`  | 否   | `无`                | 名称                             |
| `status`          | `varchar(32)`   | 否   | `'ACTIVE'`          | 允许值：ACTIVE, INACTIVE         |
| `created_at`      | `timestamptz`   | 否   | `CURRENT_TIMESTAMP` | 创建时间                         |

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

| 字段              | PostgreSQL 类型 | 可空 | 默认值              | 含义与关联                       |
| ----------------- | --------------- | ---- | ------------------- | -------------------------------- |
| `id`              | `uuid`          | 否   | `无`                | 主键；应用生成 UUID；PK          |
| `legal_entity_id` | `uuid`          | 否   | `无`                | 所属法人；FK → `legal_entity.id` |
| `code`            | `varchar(32)`   | 否   | `无`                | 编码                             |
| `name`            | `varchar(100)`  | 否   | `无`                | 名称                             |
| `parent_id`       | `uuid`          | 是   | `无`                | 上级品类；FK → `category.id`     |
| `status`          | `varchar(32)`   | 否   | `'ACTIVE'`          | 允许值：ACTIVE, INACTIVE         |
| `created_at`      | `timestamptz`   | 否   | `CURRENT_TIMESTAMP` | 创建时间                         |

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

| 字段              | PostgreSQL 类型 | 可空 | 默认值              | 含义与关联                       |
| ----------------- | --------------- | ---- | ------------------- | -------------------------------- |
| `id`              | `uuid`          | 否   | `无`                | 主键；应用生成 UUID；PK          |
| `legal_entity_id` | `uuid`          | 否   | `无`                | 所属法人；FK → `legal_entity.id` |
| `code`            | `varchar(64)`   | 否   | `无`                | 物料编码                         |
| `name`            | `varchar(200)`  | 否   | `无`                | 名称                             |
| `specification`   | `text`          | 否   | `无`                | 规格                             |
| `category_id`     | `uuid`          | 否   | `无`                | 所属品类；FK → `category.id`     |
| `unit_id`         | `uuid`          | 否   | `无`                | 唯一基础计量单位；FK → `unit.id` |
| `status`          | `varchar(32)`   | 否   | `'ACTIVE'`          | 允许值：ACTIVE, INACTIVE         |
| `created_at`      | `timestamptz`   | 否   | `CURRENT_TIMESTAMP` | 创建时间                         |

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

| 字段              | PostgreSQL 类型 | 可空 | 默认值              | 含义与关联                       |
| ----------------- | --------------- | ---- | ------------------- | -------------------------------- |
| `id`              | `uuid`          | 否   | `无`                | 主键；应用生成 UUID；PK          |
| `legal_entity_id` | `uuid`          | 否   | `无`                | 所属法人；FK → `legal_entity.id` |
| `code`            | `varchar(32)`   | 否   | `无`                | 仓库编码                         |
| `name`            | `varchar(100)`  | 否   | `无`                | 仓库名称                         |
| `department_id`   | `uuid`          | 否   | `无`                | 管理部门；FK → `department.id`   |
| `address`         | `text`          | 否   | `无`                | 地址                             |
| `status`          | `varchar(32)`   | 否   | `'ACTIVE'`          | 允许值：ACTIVE, INACTIVE         |
| `created_at`      | `timestamptz`   | 否   | `CURRENT_TIMESTAMP` | 创建时间                         |

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

| 字段              | PostgreSQL 类型 | 可空 | 默认值              | 含义与关联                              |
| ----------------- | --------------- | ---- | ------------------- | --------------------------------------- |
| `id`              | `uuid`          | 否   | `无`                | 主键；应用生成 UUID；PK                 |
| `legal_entity_id` | `uuid`          | 否   | `无`                | 所属法人；FK → `legal_entity.id`        |
| `document_type`   | `varchar(40)`   | 否   | `无`                | 领域类型，见领域主表映射                |
| `number`          | `varchar(64)`   | 否   | `无`                | 按类型序列生成的业务编号                |
| `department_id`   | `uuid`          | 否   | `无`                | 归属部门；FK → `department.id`          |
| `owner_id`        | `uuid`          | 否   | `无`                | 业务负责人；FK → `app_user.id`          |
| `created_by`      | `uuid`          | 否   | `无`                | 实际创建人/委托用户；FK → `app_user.id` |
| `revision`        | `integer`       | 否   | `1`                 | 当前内容版本                            |
| `lock_version`    | `integer`       | 否   | `1`                 | 并发控制版本                            |
| `status`          | `varchar(32)`   | 否   | `'DRAFT'`           | 领域业务状态                            |
| `updated_at`      | `timestamptz`   | 否   | `无`                | 更新时由应用写入                        |
| `created_at`      | `timestamptz`   | 否   | `CURRENT_TIMESTAMP` | 创建时间                                |

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

| 字段              | PostgreSQL 类型 | 可空 | 默认值              | 含义与关联                            |
| ----------------- | --------------- | ---- | ------------------- | ------------------------------------- |
| `id`              | `uuid`          | 否   | `无`                | 主键；应用生成 UUID；PK               |
| `legal_entity_id` | `uuid`          | 否   | `无`                | 所属法人；FK → `legal_entity.id`      |
| `document_id`     | `uuid`          | 否   | `无`                | 所属单据；FK → `business_document.id` |
| `revision`        | `integer`       | 否   | `无`                | 内容版本                              |
| `state`           | `varchar(32)`   | 否   | `'WORKING'`         | 允许值：WORKING, FROZEN               |
| `snapshot`        | `jsonb`         | 否   | `'{}'::jsonb`       | 冻结时完整单据头与明细快照            |
| `content_hash`    | `varchar(64)`   | 是   | `无`                | 冻结内容摘要                          |
| `frozen_at`       | `timestamptz`   | 是   | `无`                | 冻结时间                              |
| `created_at`      | `timestamptz`   | 否   | `CURRENT_TIMESTAMP` | 创建时间                              |

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

| 字段              | PostgreSQL 类型 | 可空 | 默认值              | 含义与关联                                              |
| ----------------- | --------------- | ---- | ------------------- | ------------------------------------------------------- |
| `id`              | `uuid`          | 否   | `无`                | 主键；应用生成 UUID；PK                                 |
| `legal_entity_id` | `uuid`          | 否   | `无`                | 所属法人；FK → `legal_entity.id`                        |
| `document_id`     | `uuid`          | 是   | `无`                | 所属单据，未绑定上传可为空；FK → `business_document.id` |
| `uploaded_by`     | `uuid`          | 否   | `无`                | 上传人；FK → `app_user.id`                              |
| `object_key`      | `varchar(512)`  | 否   | `无`                | 受控文件存储键                                          |
| `original_name`   | `varchar(255)`  | 否   | `无`                | 原文件名                                                |
| `content_type`    | `varchar(100)`  | 否   | `无`                | 验证后的内容类型                                        |
| `size_bytes`      | `bigint`        | 否   | `无`                | 字节数                                                  |
| `sha256`          | `varchar(64)`   | 否   | `无`                | 内容摘要                                                |
| `status`          | `varchar(32)`   | 否   | `'STAGED'`          | 允许值：STAGED, BOUND, QUARANTINED                      |
| `created_at`      | `timestamptz`   | 否   | `CURRENT_TIMESTAMP` | 创建时间                                                |

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

| 字段                | PostgreSQL 类型 | 可空 | 默认值              | 含义与关联                            |
| ------------------- | --------------- | ---- | ------------------- | ------------------------------------- |
| `id`                | `uuid`          | 否   | `无`                | 主键；应用生成 UUID；PK               |
| `legal_entity_id`   | `uuid`          | 否   | `无`                | 所属法人；FK → `legal_entity.id`      |
| `actor_user_id`     | `uuid`          | 是   | `无`                | 实际用户；FK → `app_user.id`          |
| `service_client_id` | `uuid`          | 是   | `无`                | 调用服务；FK → `service_client.id`    |
| `document_id`       | `uuid`          | 是   | `无`                | 相关单据；FK → `business_document.id` |
| `action`            | `varchar(120)`  | 否   | `无`                | 业务动作                              |
| `request_id`        | `varchar(64)`   | 否   | `无`                | 请求追踪标识                          |
| `assistant_task_id` | `varchar(128)`  | 是   | `无`                | 助手任务标识                          |
| `change_summary`    | `jsonb`         | 否   | `'{}'::jsonb`       | 必要的变更摘要                        |
| `reason`            | `text`          | 是   | `无`                | 业务原因                              |
| `created_at`        | `timestamptz`   | 否   | `CURRENT_TIMESTAMP` | 创建时间                              |

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

| 字段              | PostgreSQL 类型 | 可空 | 默认值              | 含义与关联                       |
| ----------------- | --------------- | ---- | ------------------- | -------------------------------- |
| `id`              | `uuid`          | 否   | `无`                | 主键；应用生成 UUID；PK          |
| `legal_entity_id` | `uuid`          | 否   | `无`                | 所属法人；FK → `legal_entity.id` |
| `policy_code`     | `varchar(64)`   | 否   | `无`                | 采购方式/匹配容差/提醒等规则编码 |
| `version`         | `integer`       | 否   | `无`                | 版本                             |
| `settings`        | `jsonb`         | 否   | `'{}'::jsonb`       | 经类型化 schema 校验的配置       |
| `approved_by`     | `uuid`          | 否   | `无`                | 配置批准人；FK → `app_user.id`   |
| `effective_at`    | `timestamptz`   | 否   | `无`                | 生效时间                         |
| `retired_at`      | `timestamptz`   | 是   | `无`                | 停用时间                         |
| `created_at`      | `timestamptz`   | 否   | `CURRENT_TIMESTAMP` | 创建时间                         |

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

## 4. 审批模块

<a id="table-approval_rule_version"></a>

### `approval_rule_version`：审批规则版本

所属模块：`approvals`；类型：规则表。

| 字段              | PostgreSQL 类型 | 可空 | 默认值              | 含义与关联                       |
| ----------------- | --------------- | ---- | ------------------- | -------------------------------- |
| `id`              | `uuid`          | 否   | `无`                | 主键；应用生成 UUID；PK          |
| `legal_entity_id` | `uuid`          | 否   | `无`                | 所属法人；FK → `legal_entity.id` |
| `rule_code`       | `varchar(64)`   | 否   | `无`                | 规则标识                         |
| `version`         | `integer`       | 否   | `无`                | 配置版本                         |
| `document_type`   | `varchar(40)`   | 否   | `无`                | 适用单据类型                     |
| `department_id`   | `uuid`          | 是   | `无`                | 限定部门；FK → `department.id`   |
| `min_amount`      | `numeric(20,2)` | 否   | `0`                 | 含税金额下界                     |
| `max_amount`      | `numeric(20,2)` | 是   | `无`                | 上界，空为无上限                 |
| `steps_config`    | `jsonb`         | 否   | `'[]'::jsonb`       | 有序节点与候选用户/角色配置      |
| `effective_at`    | `timestamptz`   | 否   | `无`                | 生效时间                         |
| `retired_at`      | `timestamptz`   | 是   | `无`                | 停用时间                         |
| `created_at`      | `timestamptz`   | 否   | `CURRENT_TIMESTAMP` | 创建时间                         |

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

| 字段              | PostgreSQL 类型 | 可空 | 默认值              | 含义与关联                                                 |
| ----------------- | --------------- | ---- | ------------------- | ---------------------------------------------------------- |
| `id`              | `uuid`          | 否   | `无`                | 主键；应用生成 UUID；PK                                    |
| `legal_entity_id` | `uuid`          | 否   | `无`                | 所属法人；FK → `legal_entity.id`                           |
| `document_id`     | `uuid`          | 否   | `无`                | 目标单据；FK → `business_document.id`                      |
| `revision`        | `integer`       | 否   | `无`                | 绑定冻结版本                                               |
| `rule_version_id` | `uuid`          | 否   | `无`                | 规则版本；FK → `approval_rule_version.id`                  |
| `submitted_by`    | `uuid`          | 否   | `无`                | 提交人；FK → `app_user.id`                                 |
| `status`          | `varchar(32)`   | 否   | `'IN_REVIEW'`       | 允许值：IN_REVIEW, APPROVED, RETURNED, REJECTED, WITHDRAWN |
| `current_step_no` | `integer`       | 否   | `1`                 | 当前顺序节点                                               |
| `completed_at`    | `timestamptz`   | 是   | `无`                | 结束时间                                                   |
| `created_at`      | `timestamptz`   | 否   | `CURRENT_TIMESTAMP` | 创建时间                                                   |

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

| 字段                   | PostgreSQL 类型 | 可空 | 默认值              | 含义与关联                                                      |
| ---------------------- | --------------- | ---- | ------------------- | --------------------------------------------------------------- |
| `id`                   | `uuid`          | 否   | `无`                | 主键；应用生成 UUID；PK                                         |
| `legal_entity_id`      | `uuid`          | 否   | `无`                | 所属法人；FK → `legal_entity.id`                                |
| `instance_id`          | `uuid`          | 否   | `无`                | 所属审批实例；FK → `approval_instance.id`                       |
| `step_no`              | `integer`       | 否   | `无`                | 顺序号                                                          |
| `assignee_id`          | `uuid`          | 否   | `无`                | 当前审批人；FK → `app_user.id`                                  |
| `original_assignee_id` | `uuid`          | 否   | `无`                | 首次分配审批人；FK → `app_user.id`                              |
| `status`               | `varchar(32)`   | 否   | `'WAITING'`         | 允许值：WAITING, PENDING, APPROVED, RETURNED, REJECTED, SKIPPED |
| `activated_at`         | `timestamptz`   | 是   | `无`                | 激活时间                                                        |
| `completed_at`         | `timestamptz`   | 是   | `无`                | 完成时间                                                        |
| `created_at`           | `timestamptz`   | 否   | `CURRENT_TIMESTAMP` | 创建时间                                                        |

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

| 字段                    | PostgreSQL 类型 | 可空 | 默认值              | 含义与关联                                |
| ----------------------- | --------------- | ---- | ------------------- | ----------------------------------------- |
| `id`                    | `uuid`          | 否   | `无`                | 主键；应用生成 UUID；PK                   |
| `legal_entity_id`       | `uuid`          | 否   | `无`                | 所属法人；FK → `legal_entity.id`          |
| `step_id`               | `uuid`          | 否   | `无`                | 目标节点；FK → `approval_step.id`         |
| `actor_id`              | `uuid`          | 否   | `无`                | 操作者；FK → `app_user.id`                |
| `decision`              | `varchar(32)`   | 否   | `无`                | 允许值：APPROVE, RETURN, REJECT, REASSIGN |
| `new_assignee_id`       | `uuid`          | 是   | `无`                | 改派后的审批人；FK → `app_user.id`        |
| `comment`               | `text`          | 否   | `无`                | 意见或改派原因                            |
| `expected_lock_version` | `integer`       | 否   | `无`                | 提交时单据并发版本                        |
| `created_at`            | `timestamptz`   | 否   | `CURRENT_TIMESTAMP` | 创建时间                                  |

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

| 字段                | PostgreSQL 类型 | 可空 | 默认值              | 含义与关联                            |
| ------------------- | --------------- | ---- | ------------------- | ------------------------------------- |
| `id`                | `uuid`          | 否   | `无`                | 主键；应用生成 UUID；PK               |
| `legal_entity_id`   | `uuid`          | 否   | `无`                | 所属法人；FK → `legal_entity.id`      |
| `actor_user_id`     | `uuid`          | 是   | `无`                | 用户或委托用户；FK → `app_user.id`    |
| `service_client_id` | `uuid`          | 是   | `无`                | 服务调用者；FK → `service_client.id`  |
| `principal_key`     | `varchar(160)`  | 否   | `无`                | 后端由用户与服务身份组合生成          |
| `action`            | `varchar(160)`  | 否   | `无`                | 含目标资源路径的动作                  |
| `idempotency_key`   | `varchar(128)`  | 否   | `无`                | 客户端幂等键                          |
| `request_hash`      | `varchar(64)`   | 否   | `无`                | 规范化输入摘要                        |
| `http_status`       | `smallint`      | 否   | `无`                | 已提交响应状态                        |
| `response_body`     | `jsonb`         | 否   | `'{}'::jsonb`       | 权限裁剪后的原响应                    |
| `document_id`       | `uuid`          | 是   | `无`                | 结果单据；FK → `business_document.id` |
| `created_at`        | `timestamptz`   | 否   | `CURRENT_TIMESTAMP` | 创建时间                              |

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

| 字段              | PostgreSQL 类型 | 可空 | 默认值              | 含义与关联                            |
| ----------------- | --------------- | ---- | ------------------- | ------------------------------------- |
| `id`              | `uuid`          | 否   | `无`                | 主键；应用生成 UUID；PK               |
| `legal_entity_id` | `uuid`          | 否   | `无`                | 所属法人；FK → `legal_entity.id`      |
| `document_id`     | `uuid`          | 是   | `无`                | 相关单据；FK → `business_document.id` |
| `object_type`     | `varchar(64)`   | 否   | `无`                | 对象类别                              |
| `object_id`       | `uuid`          | 否   | `无`                | 变更对象标识                          |
| `object_version`  | `integer`       | 否   | `无`                | 对象业务或锁版本                      |
| `event_type`      | `varchar(100)`  | 否   | `无`                | 事件类型                              |
| `payload`         | `jsonb`         | 否   | `'{}'::jsonb`       | 最小事件载荷，禁止存令牌              |
| `request_id`      | `varchar(64)`   | 否   | `无`                | 产生变化的请求                        |
| `created_at`      | `timestamptz`   | 否   | `CURRENT_TIMESTAMP` | 创建时间                              |

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

| 字段            | PostgreSQL 类型 | 可空 | 默认值               | 含义与关联              |
| --------------- | --------------- | ---- | -------------------- | ----------------------- |
| `id`            | `uuid`          | 否   | `无`                 | 主键；应用生成 UUID；PK |
| `counter_name`  | `varchar(32)`   | 否   | `'business_changes'` | 固定名称                |
| `last_sequence` | `bigint`        | 否   | `0`                  | 最后已发布序号          |
| `created_at`    | `timestamptz`   | 否   | `CURRENT_TIMESTAMP`  | 创建时间                |

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

| 字段                   | PostgreSQL 类型 | 可空 | 默认值              | 含义与关联                          |
| ---------------------- | --------------- | ---- | ------------------- | ----------------------------------- |
| `id`                   | `uuid`          | 否   | `无`                | 主键；应用生成 UUID；PK             |
| `legal_entity_id`      | `uuid`          | 否   | `无`                | 所属法人；FK → `legal_entity.id`    |
| `change_id`            | `uuid`          | 否   | `无`                | 变化记录；FK → `business_change.id` |
| `publication_sequence` | `bigint`        | 否   | `无`                | 发布器事务分配的顺序号              |
| `published_at`         | `timestamptz`   | 否   | `无`                | 发布时间                            |
| `created_at`           | `timestamptz`   | 否   | `CURRENT_TIMESTAMP` | 创建时间                            |

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

| 字段                | PostgreSQL 类型 | 可空 | 默认值              | 含义与关联                                   |
| ------------------- | --------------- | ---- | ------------------- | -------------------------------------------- |
| `id`                | `uuid`          | 否   | `无`                | 主键；应用生成 UUID；PK                      |
| `legal_entity_id`   | `uuid`          | 否   | `无`                | 所属法人；FK → `legal_entity.id`             |
| `code`              | `varchar(64)`   | 否   | `无`                | 供应商编码                                   |
| `name`              | `varchar(200)`  | 否   | `无`                | 法定/登记名称                                |
| `registration_type` | `varchar(32)`   | 否   | `无`                | 登记标识类型                                 |
| `registration_no`   | `varchar(100)`  | 否   | `无`                | 规范化登记标识                               |
| `contact_name`      | `varchar(100)`  | 否   | `无`                | 联系人                                       |
| `contact_phone`     | `varchar(64)`   | 否   | `无`                | 联系方式                                     |
| `admission_status`  | `varchar(32)`   | 否   | `'PROSPECT'`        | 允许值：PROSPECT, DRAFT, IN_REVIEW, APPROVED, REJECTED |
| `operating_status`  | `varchar(32)`   | 否   | `'ACTIVE'`          | 允许值：ACTIVE, SUSPENDED, ARCHIVED          |
| `lock_version`      | `integer`       | 否   | `1`                 | 档案并发版本                                 |
| `updated_at`        | `timestamptz`   | 否   | `无`                | 更新时间                                     |
| `created_at`        | `timestamptz`   | 否   | `CURRENT_TIMESTAMP` | 创建时间                                     |

数据库约束与索引：

- PK：`id`。
- UQ：`(legal_entity_id, code)`。
- UQ：`(legal_entity_id, registration_type, registration_no)`。
- UQ：`(legal_entity_id, id)`。
- FK：`(legal_entity_id) → legal_entity(id)`。
- CHECK：`lock_version > 0`。

补充约束与执行规则：

- 下单与直接履约合同核准生效必须同时满足 APPROVED 和 ACTIVE；PROSPECT（潜在供应商）仅允许参与受邀寻源、提交密封响应与评审比选，拟定标后必须通过正式准入审批才可进入签约与订单生效。运营状态 ACTIVE 单独不构成准入。

<a id="table-supplier_category"></a>

### `supplier_category`：供应商品类关系

所属模块：`suppliers`；类型：关联表。

| 字段              | PostgreSQL 类型 | 可空 | 默认值              | 含义与关联                       |
| ----------------- | --------------- | ---- | ------------------- | -------------------------------- |
| `id`              | `uuid`          | 否   | `无`                | 主键；应用生成 UUID；PK          |
| `legal_entity_id` | `uuid`          | 否   | `无`                | 所属法人；FK → `legal_entity.id` |
| `supplier_id`     | `uuid`          | 否   | `无`                | 供应商；FK → `supplier.id`       |
| `category_id`     | `uuid`          | 否   | `无`                | 可供货品类；FK → `category.id`   |
| `created_at`      | `timestamptz`   | 否   | `CURRENT_TIMESTAMP` | 创建时间                         |

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

| 字段                 | PostgreSQL 类型 | 可空 | 默认值              | 含义与关联                                 |
| -------------------- | --------------- | ---- | ------------------- | ------------------------------------------ |
| `id`                 | `uuid`          | 否   | `无`                | 主键；应用生成 UUID；PK                    |
| `legal_entity_id`    | `uuid`          | 否   | `无`                | 所属法人；FK → `legal_entity.id`           |
| `supplier_id`        | `uuid`          | 否   | `无`                | 所属供应商；FK → `supplier.id`             |
| `admission_id`       | `uuid`          | 否   | `无`                | 来源准入申请；FK → `supplier_admission.id` |
| `qualification_type` | `varchar(32)`   | 否   | `无`                | 资质类别                                   |
| `certificate_no`     | `varchar(100)`  | 否   | `无`                | 证书号码                                   |
| `attachment_id`      | `uuid`          | 否   | `无`                | 证照附件；FK → `attachment.id`             |
| `valid_from`         | `date`          | 否   | `无`                | 有效起日                                   |
| `valid_to`           | `date`          | 是   | `无`                | 有效止日                                   |
| `status`             | `varchar(32)`   | 否   | `'PENDING'`         | 允许值：PENDING, ACTIVE, REPLACED, REVOKED |
| `created_at`         | `timestamptz`   | 否   | `CURRENT_TIMESTAMP` | 创建时间                                   |

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

| 字段                | PostgreSQL 类型 | 可空 | 默认值              | 含义与关联                                                       |
| ------------------- | --------------- | ---- | ------------------- | ---------------------------------------------------------------- |
| `id`                | `uuid`          | 否   | `无`                | 主键；沿用 business_document.id；FK → `business_document.id`；PK |
| `legal_entity_id`   | `uuid`          | 否   | `无`                | 所属法人；FK → `legal_entity.id`                                 |
| `supplier_id`       | `uuid`          | 否   | `无`                | 申请准入的供应商；FK → `supplier.id`                             |
| `submitted_profile` | `jsonb`         | 否   | `'{}'::jsonb`       | 申请资料与供货范围快照                                           |
| `reason`            | `text`          | 否   | `无`                | 申请原因                                                         |
| `created_at`        | `timestamptz`   | 否   | `CURRENT_TIMESTAMP` | 创建时间                                                         |

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

| 字段                        | PostgreSQL 类型 | 可空 | 默认值              | 含义与关联                                                       |
| --------------------------- | --------------- | ---- | ------------------- | ---------------------------------------------------------------- |
| `id`                        | `uuid`          | 否   | `无`                | 主键；沿用 business_document.id；FK → `business_document.id`；PK |
| `legal_entity_id`           | `uuid`          | 否   | `无`                | 所属法人；FK → `legal_entity.id`                                 |
| `supplier_id`               | `uuid`          | 否   | `无`                | 供应商；FK → `supplier.id`                                       |
| `target_status`             | `varchar(32)`   | 否   | `'SUSPENDED'`       | 允许值：ACTIVE, SUSPENDED, ARCHIVED                              |
| `reason`                    | `text`          | 否   | `无`                | 状态变更理由                                                     |
| `expected_supplier_version` | `integer`       | 否   | `无`                | 提交时档案版本                                                   |
| `created_at`                | `timestamptz`   | 否   | `CURRENT_TIMESTAMP` | 创建时间                                                         |

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

| 字段              | PostgreSQL 类型 | 可空 | 默认值              | 含义与关联                       |
| ----------------- | --------------- | ---- | ------------------- | -------------------------------- |
| `id`              | `uuid`          | 否   | `无`                | 主键；应用生成 UUID；PK          |
| `legal_entity_id` | `uuid`          | 否   | `无`                | 所属法人；FK → `legal_entity.id` |
| `supplier_id`     | `uuid`          | 否   | `无`                | 供应商；FK → `supplier.id`       |
| `period_start`    | `date`          | 否   | `无`                | 统计起日                         |
| `period_end`      | `date`          | 否   | `无`                | 统计止日                         |
| `rule_version`    | `varchar(64)`   | 否   | `无`                | 指标规则版本                     |
| `sample_count`    | `integer`       | 否   | `无`                | 到期订单行样本数                 |
| `on_time_count`   | `integer`       | 否   | `无`                | 准时完成原约定数量的行数         |
| `exception_count` | `integer`       | 否   | `无`                | 取消/退货/改期行数               |
| `source_manifest` | `jsonb`         | 否   | `'{}'::jsonb`       | 样本订单行 ID 与版本清单         |
| `data_cutoff`     | `timestamptz`   | 否   | `无`                | 数据截止时间                     |
| `created_at`      | `timestamptz`   | 否   | `CURRENT_TIMESTAMP` | 创建时间                         |

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

| 字段                     | PostgreSQL 类型 | 可空 | 默认值              | 含义与关联                                                       |
| ------------------------ | --------------- | ---- | ------------------- | ---------------------------------------------------------------- |
| `id`                     | `uuid`          | 否   | `无`                | 主键；沿用 business_document.id；FK → `business_document.id`；PK |
| `legal_entity_id`        | `uuid`          | 否   | `无`                | 所属法人；FK → `legal_entity.id`                                 |
| `requester_id`           | `uuid`          | 否   | `无`                | 申请人；FK → `app_user.id`                                       |
| `budget_account_id`      | `uuid`          | 否   | `无`                | 参考预算项；FK → `budget_account.id`                             |
| `purpose`                | `text`          | 否   | `无`                | 采购用途                                                         |
| `required_date`          | `date`          | 否   | `无`                | 期望到货日期                                                     |
| `estimated_gross_amount` | `numeric(20,2)` | 否   | `0`                 | 预计含税合计                                                     |
| `created_at`             | `timestamptz`   | 否   | `CURRENT_TIMESTAMP` | 创建时间                                                         |

数据库约束与索引：

- PK：`id`。
- UQ：`(legal_entity_id, id)`。
- FK：`(legal_entity_id, id) → business_document(legal_entity_id, id)`。
- FK：`(legal_entity_id) → legal_entity(id)`。
- FK：`(legal_entity_id, requester_id) → app_user(legal_entity_id, id)`。
- FK：`(legal_entity_id, budget_account_id) → budget_account(legal_entity_id, id)`。
- 普通索引：`(legal_entity_id, requester_id)`。
- 普通索引：`(legal_entity_id, budget_account_id)`。
- 主表类型须与 `business_document.document_type` 匹配，状态使用通用登记的当前状态。

补充约束与执行规则：

- 一张申请多条明细；申请批准只校验预算，不占用预算。

<a id="table-purchase_request_line"></a>

### `purchase_request_line`：采购申请明细表

所属模块：`procurement`；类型：单据明细表。

| 字段                     | PostgreSQL 类型 | 可空 | 默认值              | 含义与关联                           |
| ------------------------ | --------------- | ---- | ------------------- | ------------------------------------ |
| `id`                     | `uuid`          | 否   | `无`                | 主键；应用生成 UUID；PK              |
| `legal_entity_id`        | `uuid`          | 否   | `无`                | 所属法人；FK → `legal_entity.id`     |
| `purchase_request_id`    | `uuid`          | 否   | `无`                | 所属主表；FK → `purchase_request.id` |
| `revision`               | `integer`       | 否   | `1`                 | 所属内容版本                         |
| `line_no`                | `integer`       | 否   | `无`                | 当前内容版本内行号                   |
| `material_id`            | `uuid`          | 否   | `无`                | 物料；FK → `material.id`             |
| `material_name`          | `varchar(200)`  | 否   | `无`                | 物料名称快照                         |
| `specification`          | `text`          | 否   | `无`                | 规格快照                             |
| `unit_id`                | `uuid`          | 否   | `无`                | 基础单位；FK → `unit.id`             |
| `unit_code`              | `varchar(32)`   | 否   | `无`                | 单位编码快照                         |
| `quantity`               | `numeric(20,6)` | 否   | `无`                | 申请数量                             |
| `estimated_unit_price`   | `numeric(20,6)` | 否   | `无`                | 预估未税单价                         |
| `estimated_tax_rate`     | `numeric(9,6)`  | 否   | `无`                | 预估税率                             |
| `estimated_gross_amount` | `numeric(20,2)` | 否   | `0`                 | 预计含税行金额                       |
| `required_date`          | `date`          | 否   | `无`                | 该行交期                             |
| `created_at`             | `timestamptz`   | 否   | `CURRENT_TIMESTAMP` | 创建时间                             |

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

<a id="table-rfq"></a>

### `rfq`：询价主表

所属模块：`procurement`；类型：单据主表。

| 字段                    | PostgreSQL 类型 | 可空 | 默认值              | 含义与关联                                                       |
| ----------------------- | --------------- | ---- | ------------------- | ---------------------------------------------------------------- |
| `id`                    | `uuid`          | 否   | `无`                | 主键；沿用 business_document.id；FK → `business_document.id`；PK |
| `legal_entity_id`       | `uuid`          | 否   | `无`                | 所属法人；FK → `legal_entity.id`                                 |
| `purchase_request_id`   | `uuid`          | 否   | `无`                | 来源申请；FK → `purchase_request.id`                             |
| `deadline`              | `timestamptz`   | 否   | `无`                | 报价截止时间                                                     |
| `currency`              | `char(3)`       | 否   | `'CNY'`             | 报价币种                                                         |
| `minimum_suppliers`     | `integer`       | 否   | `无`                | 最低报价家数快照                                                 |
| `rule_version`          | `varchar(64)`   | 否   | `无`                | 采购方式规则版本                                                 |
| `exception_document_id` | `uuid`          | 是   | `无`                | 不足家数的已批准例外；FK → `purchase_method_exception.id`        |
| `published_at`          | `timestamptz`   | 是   | `无`                | 发布时间                                                         |
| `closed_at`             | `timestamptz`   | 是   | `无`                | 截止时间                                                         |
| `unsealed_at`           | `timestamptz`   | 是   | `无`                | 集中开标解密时间                                                 |
| `created_at`            | `timestamptz`   | 否   | `CURRENT_TIMESTAMP` | 创建时间                                                         |

数据库约束与索引：

- PK：`id`。
- UQ：`(legal_entity_id, id)`。
- FK：`(legal_entity_id, id) → business_document(legal_entity_id, id)`。
- FK：`(legal_entity_id) → legal_entity(id)`。
- FK：`(legal_entity_id, purchase_request_id) → purchase_request(legal_entity_id, id)`。
- FK：`(legal_entity_id, exception_document_id) → purchase_method_exception(legal_entity_id, id)`。
- CHECK：`currency = 'CNY'`。
- CHECK：`minimum_suppliers > 0`。
- 普通索引：`(legal_entity_id, purchase_request_id)`。
- 普通索引：`(legal_entity_id, exception_document_id)`。
- 主表类型须与 `business_document.document_type` 匹配，状态使用通用登记的当前状态。

补充约束与执行规则：

- 状态流转：OPEN → CLOSED 必须到达 deadline；CLOSED → UNSEALED 必须有效密封响应家数满足 minimum_suppliers 或已审批绑定 exception_document_id。
- 物理补充 purchase_method_exception，用于既有流程中的单一来源等例外审批。

<a id="table-purchase_method_exception"></a>

### `purchase_method_exception`：采购方式例外申请

所属模块：`procurement`；类型：单据主表。

| 字段                      | PostgreSQL 类型 | 可空 | 默认值              | 含义与关联                                                       |
| ------------------------- | --------------- | ---- | ------------------- | ---------------------------------------------------------------- |
| `id`                      | `uuid`          | 否   | `无`                | 主键；沿用 business_document.id；FK → `business_document.id`；PK |
| `legal_entity_id`         | `uuid`          | 否   | `无`                | 所属法人；FK → `legal_entity.id`                                 |
| `purchase_request_id`     | `uuid`          | 否   | `无`                | 申请；FK → `purchase_request.id`                                 |
| `requested_method`        | `varchar(32)`   | 否   | `无`                | 例外采购方式                                                     |
| `proposed_supplier_count` | `integer`       | 否   | `无`                | 拟邀请家数                                                       |
| `reason`                  | `text`          | 否   | `无`                | 例外理由                                                         |
| `created_at`              | `timestamptz`   | 否   | `CURRENT_TIMESTAMP` | 创建时间                                                         |

数据库约束与索引：

- PK：`id`。
- UQ：`(legal_entity_id, id)`。
- FK：`(legal_entity_id, id) → business_document(legal_entity_id, id)`。
- FK：`(legal_entity_id) → legal_entity(id)`。
- FK：`(legal_entity_id, purchase_request_id) → purchase_request(legal_entity_id, id)`。
- CHECK：`proposed_supplier_count > 0`。
- 普通索引：`(legal_entity_id, purchase_request_id)`。
- 主表类型须与 `business_document.document_type` 匹配，状态使用通用登记的当前状态。

<a id="table-rfq_line"></a>

### `rfq_line`：询价明细表

所属模块：`procurement`；类型：单据明细表。

| 字段              | PostgreSQL 类型 | 可空 | 默认值              | 含义与关联                                    |
| ----------------- | --------------- | ---- | ------------------- | --------------------------------------------- |
| `id`              | `uuid`          | 否   | `无`                | 主键；应用生成 UUID；PK                       |
| `legal_entity_id` | `uuid`          | 否   | `无`                | 所属法人；FK → `legal_entity.id`              |
| `rfq_id`          | `uuid`          | 否   | `无`                | 所属主表；FK → `rfq.id`                       |
| `revision`        | `integer`       | 否   | `1`                 | 所属内容版本                                  |
| `line_no`         | `integer`       | 否   | `无`                | 当前内容版本内行号                            |
| `request_line_id` | `uuid`          | 否   | `无`                | 来源申请明细；FK → `purchase_request_line.id` |
| `material_id`     | `uuid`          | 否   | `无`                | 物料；FK → `material.id`                      |
| `material_name`   | `varchar(200)`  | 否   | `无`                | 物料名称快照                                  |
| `specification`   | `text`          | 否   | `无`                | 规格快照                                      |
| `unit_id`         | `uuid`          | 否   | `无`                | 基础单位；FK → `unit.id`                      |
| `unit_code`       | `varchar(32)`   | 否   | `无`                | 单位编码快照                                  |
| `quantity`        | `numeric(20,6)` | 否   | `无`                | 询价数量                                      |
| `required_date`   | `date`          | 否   | `无`                | 要求交期                                      |
| `created_at`      | `timestamptz`   | 否   | `CURRENT_TIMESTAMP` | 创建时间                                      |

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

<a id="table-rfq_invitation"></a>

### `rfq_invitation`：询价邀请关系

所属模块：`procurement`；类型：关联表。

| 字段              | PostgreSQL 类型 | 可空 | 默认值              | 含义与关联                           |
| ----------------- | --------------- | ---- | ------------------- | ------------------------------------ |
| `id`              | `uuid`          | 否   | `无`                | 主键；应用生成 UUID；PK              |
| `legal_entity_id` | `uuid`          | 否   | `无`                | 所属法人；FK → `legal_entity.id`     |
| `rfq_id`          | `uuid`          | 否   | `无`                | 询价；FK → `rfq.id`                  |
| `rfq_revision`    | `integer`       | 否   | `无`                | 发布时询价版本                       |
| `supplier_id`     | `uuid`          | 否   | `无`                | 邀请供应商；FK → `supplier.id`       |
| `invited_at`      | `timestamptz`   | 是   | `无`                | 线下邀请登记时间                     |
| `status`          | `varchar(32)`   | 否   | `'INVITED'`         | 允许值：INVITED, RESPONDED, DECLINED |
| `created_at`      | `timestamptz`   | 否   | `CURRENT_TIMESTAMP` | 创建时间                             |

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

| 字段                   | PostgreSQL 类型 | 可空 | 默认值              | 含义与关联                                                       |
| ---------------------- | --------------- | ---- | ------------------- | ---------------------------------------------------------------- |
| `id`                   | `uuid`          | 否   | `无`                | 主键；沿用 business_document.id；FK → `business_document.id`；PK |
| `legal_entity_id`      | `uuid`          | 否   | `无`                | 所属法人；FK → `legal_entity.id`                                 |
| `invitation_id`        | `uuid`          | 否   | `无`                | 报价对应邀请；FK → `rfq_invitation.id`                           |
| `quote_version`        | `integer`       | 否   | `无`                | 该邀请报价版本                                                   |
| `valid_until`          | `date`          | 否   | `无`                | 报价有效期                                                       |
| `currency`             | `char(3)`       | 否   | `'CNY'`             | 币种                                                             |
| `payment_terms`        | `text`          | 否   | `无`                | 付款条件                                                         |
| `source_attachment_id` | `uuid`          | 否   | `无`                | 报价凭证；FK → `attachment.id`                                   |
| `sealed_hash`          | `varchar(128)`  | 否   | `无`                | 密封凭证（封条或文件数字哈希快照）                               |
| `gross_amount`         | `numeric(20,2)` | 否   | `0`                 | 报价含税合计（开标前为 0，开标后解密写入）                       |
| `sealed_at`            | `timestamptz`   | 否   | `CURRENT_TIMESTAMP` | 盲收登记时间                                                     |
| `unsealed_at`          | `timestamptz`   | 是   | `无`                | 集中开标解密时间                                                 |
| `created_at`           | `timestamptz`   | 否   | `CURRENT_TIMESTAMP` | 创建时间                                                         |

数据库约束与索引：

- PK：`id`。
- UQ：`(invitation_id, quote_version)`。
- UQ：`(legal_entity_id, id)`。
- FK：`(legal_entity_id, id) → business_document(legal_entity_id, id)`。
- FK：`(legal_entity_id) → legal_entity(id)`。
- FK：`(legal_entity_id, invitation_id) → rfq_invitation(legal_entity_id, id)`。
- FK：`(legal_entity_id, source_attachment_id) → attachment(legal_entity_id, id)`。
- CHECK：`quote_version > 0`。
- CHECK：`currency = 'CNY'`。
- 普通索引：`(legal_entity_id, invitation_id)`。
- 普通索引：`(legal_entity_id, source_attachment_id)`。
- 主表类型须与 `business_document.document_type` 匹配，状态使用通用登记的当前状态。

补充约束与执行规则：

- 截标前（状态 SEALED）：盲收登记仅保存 sealed_hash 与 sealed_at，严禁录入或向业务接口暴露报价金额明细；
- 集中开标解密（状态转 RECORDED）：截标后由开标事务核验 sealed_hash 未篡改，原子解密并写入明细行与 gross_amount，记录 unsealed_at；
- 提交一个报价版本后冻结；修订创建新的 quotation 主表和 quote_version，定标引用精确报价行，不覆盖旧报价。

<a id="table-quotation_line"></a>

### `quotation_line`：报价明细表

所属模块：`procurement`；类型：单据明细表。

| 字段              | PostgreSQL 类型 | 可空 | 默认值              | 含义与关联                       |
| ----------------- | --------------- | ---- | ------------------- | -------------------------------- |
| `id`              | `uuid`          | 否   | `无`                | 主键；应用生成 UUID；PK          |
| `legal_entity_id` | `uuid`          | 否   | `无`                | 所属法人；FK → `legal_entity.id` |
| `quotation_id`    | `uuid`          | 否   | `无`                | 所属主表；FK → `quotation.id`    |
| `revision`        | `integer`       | 否   | `1`                 | 所属内容版本                     |
| `line_no`         | `integer`       | 否   | `无`                | 当前内容版本内行号               |
| `rfq_line_id`     | `uuid`          | 否   | `无`                | 对应询价行；FK → `rfq_line.id`   |
| `quantity`        | `numeric(20,6)` | 否   | `无`                | 报价数量                         |
| `unit_price`      | `numeric(20,6)` | 否   | `无`                | 未税单价                         |
| `tax_rate`        | `numeric(9,6)`  | 否   | `无`                | 税率，如 0.130000                |
| `net_amount`      | `numeric(20,2)` | 否   | `0`                 | 未税行金额                       |
| `tax_amount`      | `numeric(20,2)` | 否   | `0`                 | 税额                             |
| `gross_amount`    | `numeric(20,2)` | 否   | `0`                 | 含税行金额                       |
| `promised_date`   | `date`          | 否   | `无`                | 承诺交期                         |
| `created_at`      | `timestamptz`   | 否   | `CURRENT_TIMESTAMP` | 创建时间                         |

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

<a id="table-award"></a>

### `award`：定标主表

所属模块：`procurement`；类型：单据主表。

| 字段                      | PostgreSQL 类型 | 可空 | 默认值              | 含义与关联                                                       |
| ------------------------- | --------------- | ---- | ------------------- | ---------------------------------------------------------------- |
| `id`                      | `uuid`          | 否   | `无`                | 主键；沿用 business_document.id；FK → `business_document.id`；PK |
| `legal_entity_id`         | `uuid`          | 否   | `无`                | 所属法人；FK → `legal_entity.id`                                 |
| `rfq_id`                  | `uuid`          | 否   | `无`                | 对应询价；FK → `rfq.id`                                          |
| `selection_reason`        | `text`          | 否   | `无`                | 选择依据                                                         |
| `comparison_rule_version` | `varchar(64)`   | 否   | `无`                | 比价规则版本                                                     |
| `created_at`              | `timestamptz`   | 否   | `CURRENT_TIMESTAMP` | 创建时间                                                         |

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

| 字段                | PostgreSQL 类型 | 可空 | 默认值              | 含义与关联                           |
| ------------------- | --------------- | ---- | ------------------- | ------------------------------------ |
| `id`                | `uuid`          | 否   | `无`                | 主键；应用生成 UUID；PK              |
| `legal_entity_id`   | `uuid`          | 否   | `无`                | 所属法人；FK → `legal_entity.id`     |
| `award_id`          | `uuid`          | 否   | `无`                | 所属主表；FK → `award.id`            |
| `revision`          | `integer`       | 否   | `1`                 | 所属内容版本                         |
| `line_no`           | `integer`       | 否   | `无`                | 当前内容版本内行号                   |
| `rfq_line_id`       | `uuid`          | 否   | `无`                | 询价行；FK → `rfq_line.id`           |
| `quotation_line_id` | `uuid`          | 否   | `无`                | 获选报价行；FK → `quotation_line.id` |
| `supplier_id`       | `uuid`          | 否   | `无`                | 获选供应商；FK → `supplier.id`       |
| `awarded_quantity`  | `numeric(20,6)` | 否   | `无`                | 批准分配数量                         |
| `reason`            | `text`          | 否   | `无`                | 该行选择理由                         |
| `created_at`        | `timestamptz`   | 否   | `CURRENT_TIMESTAMP` | 创建时间                             |

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
- CHECK：`awarded_quantity > 0`。
- 普通索引：`(legal_entity_id, award_id)`。
- 普通索引：`(legal_entity_id, rfq_line_id)`。
- 普通索引：`(legal_entity_id, quotation_line_id)`。
- 普通索引：`(legal_entity_id, supplier_id)`。

补充约束与执行规则：

- 复合外键 (award_id, revision) → document_revision(document_id, revision)。修改冻结版本时建立新行 ID，旧行保留供下游引用。
- 当前首期每个定标版本同一询价行只选一家；报价行必须来自该询价行且供应商一致。

<a id="table-purchase_order"></a>

### `purchase_order`：采购订单主表

所属模块：`procurement`；类型：单据主表。

| 字段                     | PostgreSQL 类型 | 可空 | 默认值              | 含义与关联                                                       |
| ------------------------ | --------------- | ---- | ------------------- | ---------------------------------------------------------------- |
| `id`                     | `uuid`          | 否   | `无`                | 主键；沿用 business_document.id；FK → `business_document.id`；PK |
| `legal_entity_id`        | `uuid`          | 否   | `无`                | 所属法人；FK → `legal_entity.id`                                 |
| `award_id`               | `uuid`          | 否   | `无`                | 定标来源；FK → `award.id`                                        |
| `supplier_id`            | `uuid`          | 否   | `无`                | 唯一供应商；FK → `supplier.id`                                   |
| `budget_account_id`      | `uuid`          | 否   | `无`                | 唯一预算账户；FK → `budget_account.id`                           |
| `warehouse_id`           | `uuid`          | 否   | `无`                | 唯一收货仓库；FK → `warehouse.id`                                |
| `supplier_name`          | `varchar(200)`  | 否   | `无`                | 供应商名称快照                                                   |
| `currency`               | `char(3)`       | 否   | `'CNY'`             | 币种                                                             |
| `required_date`          | `date`          | 否   | `无`                | 默认交期                                                         |
| `net_amount`             | `numeric(20,2)` | 否   | `0`                 | 未税合计                                                         |
| `tax_amount`             | `numeric(20,2)` | 否   | `0`                 | 税额合计                                                         |
| `gross_amount`           | `numeric(20,2)` | 否   | `0`                 | 含税合计                                                         |
| `issued_at`              | `timestamptz`   | 是   | `无`                | 正式发布时间                                                     |
| `delivery_attachment_id` | `uuid`          | 是   | `无`                | 订单送达凭证；FK → `attachment.id`                               |
| `created_at`             | `timestamptz`   | 否   | `CURRENT_TIMESTAMP` | 创建时间                                                         |

数据库约束与索引：

- PK：`id`。
- UQ：`(legal_entity_id, id)`。
- FK：`(legal_entity_id, id) → business_document(legal_entity_id, id)`。
- FK：`(legal_entity_id) → legal_entity(id)`。
- FK：`(legal_entity_id, award_id) → award(legal_entity_id, id)`。
- FK：`(legal_entity_id, supplier_id) → supplier(legal_entity_id, id)`。
- FK：`(legal_entity_id, budget_account_id) → budget_account(legal_entity_id, id)`。
- FK：`(legal_entity_id, warehouse_id) → warehouse(legal_entity_id, id)`。
- FK：`(legal_entity_id, delivery_attachment_id) → attachment(legal_entity_id, id)`。
- CHECK：`currency = 'CNY'`。
- CHECK：`gross_amount = net_amount + tax_amount`。
- 普通索引：`(legal_entity_id, award_id)`。
- 普通索引：`(legal_entity_id, supplier_id)`。
- 普通索引：`(legal_entity_id, budget_account_id)`。
- 普通索引：`(legal_entity_id, warehouse_id)`。
- 普通索引：`(legal_entity_id, delivery_attachment_id)`。
- 主表类型须与 `business_document.document_type` 匹配，状态使用通用登记的当前状态。

补充约束与执行规则：

- 审批/交付/结算状态不重复存三个可任意编辑字段：主状态在 business_document，交付和结算由明细账本派生。

<a id="table-purchase_order_line"></a>

### `purchase_order_line`：采购订单明细表

所属模块：`procurement`；类型：单据明细表。

| 字段                | PostgreSQL 类型 | 可空 | 默认值              | 含义与关联                                      |
| ------------------- | --------------- | ---- | ------------------- | ----------------------------------------------- |
| `id`                | `uuid`          | 否   | `无`                | 主键；应用生成 UUID；PK                         |
| `legal_entity_id`   | `uuid`          | 否   | `无`                | 所属法人；FK → `legal_entity.id`                |
| `purchase_order_id` | `uuid`          | 否   | `无`                | 所属主表；FK → `purchase_order.id`              |
| `revision`          | `integer`       | 否   | `1`                 | 所属内容版本                                    |
| `line_no`           | `integer`       | 否   | `无`                | 当前内容版本内行号                              |
| `award_line_id`     | `uuid`          | 否   | `无`                | 定标来源行；FK → `award_line.id`                |
| `request_line_id`   | `uuid`          | 否   | `无`                | 追溯申请来源行；FK → `purchase_request_line.id` |
| `material_id`       | `uuid`          | 否   | `无`                | 物料；FK → `material.id`                        |
| `material_name`     | `varchar(200)`  | 否   | `无`                | 物料名称快照                                    |
| `specification`     | `text`          | 否   | `无`                | 规格快照                                        |
| `unit_id`           | `uuid`          | 否   | `无`                | 基础单位；FK → `unit.id`                        |
| `unit_code`         | `varchar(32)`   | 否   | `无`                | 单位编码快照                                    |
| `ordered_quantity`  | `numeric(20,6)` | 否   | `无`                | 获批订购数量                                    |
| `unit_price`        | `numeric(20,6)` | 否   | `无`                | 未税单价                                        |
| `tax_rate`          | `numeric(9,6)`  | 否   | `无`                | 税率，如 0.130000                               |
| `net_amount`        | `numeric(20,2)` | 否   | `0`                 | 未税行金额                                      |
| `tax_amount`        | `numeric(20,2)` | 否   | `0`                 | 税额                                            |
| `gross_amount`      | `numeric(20,2)` | 否   | `0`                 | 含税行金额                                      |
| `required_date`     | `date`          | 否   | `无`                | 该行交期                                        |
| `created_at`        | `timestamptz`   | 否   | `CURRENT_TIMESTAMP` | 创建时间                                        |

数据库约束与索引：

- PK：`id`。
- UQ：`(purchase_order_id, revision, line_no)`。
- UQ：`(legal_entity_id, id)`。
- FK：`(legal_entity_id) → legal_entity(id)`。
- FK：`(legal_entity_id, purchase_order_id) → purchase_order(legal_entity_id, id)`。
- FK：`(legal_entity_id, award_line_id) → award_line(legal_entity_id, id)`。
- FK：`(legal_entity_id, request_line_id) → purchase_request_line(legal_entity_id, id)`。
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
- 普通索引：`(legal_entity_id, request_line_id)`。
- 普通索引：`(legal_entity_id, material_id)`。
- 普通索引：`(legal_entity_id, unit_id)`。

补充约束与执行规则：

- 复合外键 (purchase_order_id, revision) → document_revision(document_id, revision)。修改冻结版本时建立新行 ID，旧行保留供下游引用。
- 批准数量和价格冻结；累计收货、取消、退货通过已生效明细聚合计算，事务锁本行防止超量。来源申请、定标、物料须相互一致。

<a id="table-order_close_request"></a>

### `order_close_request`：订单余量关闭主表

所属模块：`procurement`；类型：单据主表。

| 字段                | PostgreSQL 类型 | 可空 | 默认值              | 含义与关联                                                       |
| ------------------- | --------------- | ---- | ------------------- | ---------------------------------------------------------------- |
| `id`                | `uuid`          | 否   | `无`                | 主键；沿用 business_document.id；FK → `business_document.id`；PK |
| `legal_entity_id`   | `uuid`          | 否   | `无`                | 所属法人；FK → `legal_entity.id`                                 |
| `purchase_order_id` | `uuid`          | 否   | `无`                | 订单；FK → `purchase_order.id`                                   |
| `reason`            | `text`          | 否   | `无`                | 取消原因                                                         |
| `created_at`        | `timestamptz`   | 否   | `CURRENT_TIMESTAMP` | 创建时间                                                         |

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

| 字段                     | PostgreSQL 类型 | 可空 | 默认值              | 含义与关联                              |
| ------------------------ | --------------- | ---- | ------------------- | --------------------------------------- |
| `id`                     | `uuid`          | 否   | `无`                | 主键；应用生成 UUID；PK                 |
| `legal_entity_id`        | `uuid`          | 否   | `无`                | 所属法人；FK → `legal_entity.id`        |
| `order_close_request_id` | `uuid`          | 否   | `无`                | 所属主表；FK → `order_close_request.id` |
| `revision`               | `integer`       | 否   | `1`                 | 所属内容版本                            |
| `line_no`                | `integer`       | 否   | `无`                | 当前内容版本内行号                      |
| `order_line_id`          | `uuid`          | 否   | `无`                | 订单行；FK → `purchase_order_line.id`   |
| `cancel_quantity`        | `numeric(20,6)` | 否   | `无`                | 申请取消的待收量                        |
| `released_amount`        | `numeric(20,2)` | 否   | `0`                 | 批准时计算的预算释放金额                |
| `created_at`             | `timestamptz`   | 否   | `CURRENT_TIMESTAMP` | 创建时间                                |

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
- CHECK：`cancel_quantity > 0`。
- CHECK：`released_amount >= 0`。
- 普通索引：`(legal_entity_id, order_close_request_id)`。
- 普通索引：`(legal_entity_id, order_line_id)`。

补充约束与执行规则：

- 复合外键 (order_close_request_id, revision) → document_revision(document_id, revision)。修改冻结版本时建立新行 ID，旧行保留供下游引用。
- 只有审批通过的关闭明细计入有效取消量；不得超当前待收量。

## 8. 库存与仓储模块

<a id="table-receipt"></a>

### `receipt`：采购收货主表

所属模块：`inventory`；类型：单据主表。

| 字段                | PostgreSQL 类型 | 可空 | 默认值              | 含义与关联                                                       |
| ------------------- | --------------- | ---- | ------------------- | ---------------------------------------------------------------- |
| `id`                | `uuid`          | 否   | `无`                | 主键；沿用 business_document.id；FK → `business_document.id`；PK |
| `legal_entity_id`   | `uuid`          | 否   | `无`                | 所属法人；FK → `legal_entity.id`                                 |
| `purchase_order_id` | `uuid`          | 否   | `无`                | 订单；FK → `purchase_order.id`                                   |
| `warehouse_id`      | `uuid`          | 否   | `无`                | 收货仓库；FK → `warehouse.id`                                    |
| `receipt_date`      | `date`          | 否   | `无`                | 业务收货日期                                                     |
| `received_by`       | `uuid`          | 否   | `无`                | 收货人；FK → `app_user.id`                                       |
| `posted_at`         | `timestamptz`   | 是   | `无`                | 过账时间                                                         |
| `created_at`        | `timestamptz`   | 否   | `CURRENT_TIMESTAMP` | 创建时间                                                         |

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

| 字段                      | PostgreSQL 类型 | 可空 | 默认值              | 含义与关联                                |
| ------------------------- | --------------- | ---- | ------------------- | ----------------------------------------- |
| `id`                      | `uuid`          | 否   | `无`                | 主键；应用生成 UUID；PK                   |
| `legal_entity_id`         | `uuid`          | 否   | `无`                | 所属法人；FK → `legal_entity.id`          |
| `receipt_id`              | `uuid`          | 否   | `无`                | 所属主表；FK → `receipt.id`               |
| `revision`                | `integer`       | 否   | `1`                 | 所属内容版本                              |
| `line_no`                 | `integer`       | 否   | `无`                | 当前内容版本内行号                        |
| `order_line_id`           | `uuid`          | 否   | `无`                | 对应订单行；FK → `purchase_order_line.id` |
| `material_id`             | `uuid`          | 否   | `无`                | 收货物料；FK → `material.id`              |
| `received_quantity`       | `numeric(20,6)` | 否   | `无`                | 验收合格实收数量                          |
| `budget_execution_amount` | `numeric(20,2)` | 否   | `0`                 | 订单口径转执行金额                        |
| `created_at`              | `timestamptz`   | 否   | `CURRENT_TIMESTAMP` | 创建时间                                  |

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

<a id="table-return_order"></a>

### `return_order`：采购退货主表

所属模块：`inventory`；类型：单据主表。

| 字段                | PostgreSQL 类型 | 可空 | 默认值              | 含义与关联                                                       |
| ------------------- | --------------- | ---- | ------------------- | ---------------------------------------------------------------- |
| `id`                | `uuid`          | 否   | `无`                | 主键；沿用 business_document.id；FK → `business_document.id`；PK |
| `legal_entity_id`   | `uuid`          | 否   | `无`                | 所属法人；FK → `legal_entity.id`                                 |
| `purchase_order_id` | `uuid`          | 否   | `无`                | 来源订单；FK → `purchase_order.id`                               |
| `warehouse_id`      | `uuid`          | 否   | `无`                | 退货仓库；FK → `warehouse.id`                                    |
| `return_date`       | `date`          | 否   | `无`                | 退货日期                                                         |
| `reason`            | `text`          | 否   | `无`                | 退货原因                                                         |
| `posted_at`         | `timestamptz`   | 是   | `无`                | 过账时间                                                         |
| `created_at`        | `timestamptz`   | 否   | `CURRENT_TIMESTAMP` | 创建时间                                                         |

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

<a id="table-return_line"></a>

### `return_line`：采购退货明细表

所属模块：`inventory`；类型：单据明细表。

| 字段                        | PostgreSQL 类型 | 可空 | 默认值              | 含义与关联                       |
| --------------------------- | --------------- | ---- | ------------------- | -------------------------------- |
| `id`                        | `uuid`          | 否   | `无`                | 主键；应用生成 UUID；PK          |
| `legal_entity_id`           | `uuid`          | 否   | `无`                | 所属法人；FK → `legal_entity.id` |
| `return_order_id`           | `uuid`          | 否   | `无`                | 所属主表；FK → `return_order.id` |
| `revision`                  | `integer`       | 否   | `1`                 | 所属内容版本                     |
| `line_no`                   | `integer`       | 否   | `无`                | 当前内容版本内行号               |
| `receipt_line_id`           | `uuid`          | 否   | `无`                | 原收货行；FK → `receipt_line.id` |
| `return_quantity`           | `numeric(20,6)` | 否   | `无`                | 不补货退货量                     |
| `released_execution_amount` | `numeric(20,2)` | 否   | `0`                 | 冲减预算已执行金额               |
| `created_at`                | `timestamptz`   | 否   | `CURRENT_TIMESTAMP` | 创建时间                         |

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

| 字段                      | PostgreSQL 类型 | 可空 | 默认值              | 含义与关联                                                       |
| ------------------------- | --------------- | ---- | ------------------- | ---------------------------------------------------------------- |
| `id`                      | `uuid`          | 否   | `无`                | 主键；沿用 business_document.id；FK → `business_document.id`；PK |
| `legal_entity_id`         | `uuid`          | 否   | `无`                | 所属法人；FK → `legal_entity.id`                                 |
| `warehouse_id`            | `uuid`          | 否   | `无`                | 出库仓库；FK → `warehouse.id`                                    |
| `receiving_department_id` | `uuid`          | 否   | `无`                | 领用部门；FK → `department.id`                                   |
| `recipient_id`            | `uuid`          | 否   | `无`                | 领用人；FK → `app_user.id`                                       |
| `issue_date`              | `date`          | 否   | `无`                | 领用日期                                                         |
| `purpose`                 | `text`          | 否   | `无`                | 领用用途                                                         |
| `posted_at`               | `timestamptz`   | 是   | `无`                | 过账时间                                                         |
| `created_at`              | `timestamptz`   | 否   | `CURRENT_TIMESTAMP` | 创建时间                                                         |

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

| 字段              | PostgreSQL 类型 | 可空 | 默认值              | 含义与关联                       |
| ----------------- | --------------- | ---- | ------------------- | -------------------------------- |
| `id`              | `uuid`          | 否   | `无`                | 主键；应用生成 UUID；PK          |
| `legal_entity_id` | `uuid`          | 否   | `无`                | 所属法人；FK → `legal_entity.id` |
| `stock_issue_id`  | `uuid`          | 否   | `无`                | 所属主表；FK → `stock_issue.id`  |
| `revision`        | `integer`       | 否   | `1`                 | 所属内容版本                     |
| `line_no`         | `integer`       | 否   | `无`                | 当前内容版本内行号               |
| `material_id`     | `uuid`          | 否   | `无`                | 领用物料；FK → `material.id`     |
| `issue_quantity`  | `numeric(20,6)` | 否   | `无`                | 领用数量                         |
| `created_at`      | `timestamptz`   | 否   | `CURRENT_TIMESTAMP` | 创建时间                         |

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

| 字段              | PostgreSQL 类型 | 可空 | 默认值              | 含义与关联                                                       |
| ----------------- | --------------- | ---- | ------------------- | ---------------------------------------------------------------- |
| `id`              | `uuid`          | 否   | `无`                | 主键；沿用 business_document.id；FK → `business_document.id`；PK |
| `legal_entity_id` | `uuid`          | 否   | `无`                | 所属法人；FK → `legal_entity.id`                                 |
| `warehouse_id`    | `uuid`          | 否   | `无`                | 仓库；FK → `warehouse.id`                                        |
| `adjustment_type` | `varchar(32)`   | 否   | `'COUNT'`           | 允许值：OPENING, COUNT, CORRECTION                               |
| `business_date`   | `date`          | 否   | `无`                | 业务日期                                                         |
| `reason`          | `text`          | 否   | `无`                | 调整依据                                                         |
| `posted_at`       | `timestamptz`   | 是   | `无`                | 过账时间                                                         |
| `created_at`      | `timestamptz`   | 否   | `CURRENT_TIMESTAMP` | 创建时间                                                         |

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

| 字段                       | PostgreSQL 类型 | 可空 | 默认值              | 含义与关联                           |
| -------------------------- | --------------- | ---- | ------------------- | ------------------------------------ |
| `id`                       | `uuid`          | 否   | `无`                | 主键；应用生成 UUID；PK              |
| `legal_entity_id`          | `uuid`          | 否   | `无`                | 所属法人；FK → `legal_entity.id`     |
| `stock_adjustment_id`      | `uuid`          | 否   | `无`                | 所属主表；FK → `stock_adjustment.id` |
| `revision`                 | `integer`       | 否   | `1`                 | 所属内容版本                         |
| `line_no`                  | `integer`       | 否   | `无`                | 当前内容版本内行号                   |
| `material_id`              | `uuid`          | 否   | `无`                | 物料；FK → `material.id`             |
| `observed_quantity`        | `numeric(20,6)` | 否   | `无`                | 观察时账面库存                       |
| `counted_quantity`         | `numeric(20,6)` | 否   | `无`                | 盘点/期初确认数量                    |
| `delta_quantity`           | `numeric(20,6)` | 否   | `无`                | 盘点数量减观察数量                   |
| `expected_balance_version` | `integer`       | 否   | `无`                | 观察时余额版本，未建余额为 0         |
| `created_at`               | `timestamptz`   | 否   | `CURRENT_TIMESTAMP` | 创建时间                             |

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

| 字段              | PostgreSQL 类型 | 可空 | 默认值              | 含义与关联                       |
| ----------------- | --------------- | ---- | ------------------- | -------------------------------- |
| `id`              | `uuid`          | 否   | `无`                | 主键；应用生成 UUID；PK          |
| `legal_entity_id` | `uuid`          | 否   | `无`                | 所属法人；FK → `legal_entity.id` |
| `warehouse_id`    | `uuid`          | 否   | `无`                | 仓库；FK → `warehouse.id`        |
| `material_id`     | `uuid`          | 否   | `无`                | 物料；FK → `material.id`         |
| `quantity`        | `numeric(20,6)` | 否   | `0`                 | 当前数量                         |
| `lock_version`    | `integer`       | 否   | `1`                 | 余额版本                         |
| `updated_at`      | `timestamptz`   | 否   | `无`                | 更新时间                         |
| `created_at`      | `timestamptz`   | 否   | `CURRENT_TIMESTAMP` | 创建时间                         |

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

| 字段                    | PostgreSQL 类型 | 可空 | 默认值              | 含义与关联                                           |
| ----------------------- | --------------- | ---- | ------------------- | ---------------------------------------------------- |
| `id`                    | `uuid`          | 否   | `无`                | 主键；应用生成 UUID；PK                              |
| `legal_entity_id`       | `uuid`          | 否   | `无`                | 所属法人；FK → `legal_entity.id`                     |
| `warehouse_id`          | `uuid`          | 否   | `无`                | 仓库；FK → `warehouse.id`                            |
| `material_id`           | `uuid`          | 否   | `无`                | 物料；FK → `material.id`                             |
| `receipt_line_id`       | `uuid`          | 是   | `无`                | 收货来源；FK → `receipt_line.id`                     |
| `return_line_id`        | `uuid`          | 是   | `无`                | 退货来源；FK → `return_line.id`                      |
| `issue_line_id`         | `uuid`          | 是   | `无`                | 领用来源；FK → `stock_issue_line.id`                 |
| `adjustment_line_id`    | `uuid`          | 是   | `无`                | 盘点/期初来源；FK → `stock_adjustment_line.id`       |
| `correction_request_id` | `uuid`          | 是   | `无`                | 冲销申请；FK → `correction_request.id`               |
| `reversal_of_id`        | `uuid`          | 是   | `无`                | 被冲销原流水；FK → `stock_ledger.id`                 |
| `posting_key`           | `varchar(160)`  | 否   | `无`                | 按来源行与动作规范生成的记账键                       |
| `entry_type`            | `varchar(32)`   | 否   | `'RECEIPT'`         | 允许值：RECEIPT, RETURN, ISSUE, ADJUSTMENT, REVERSAL |
| `delta_quantity`        | `numeric(20,6)` | 否   | `无`                | 带正负号数量                                         |
| `balance_after`         | `numeric(20,6)` | 否   | `无`                | 过账后余额                                           |
| `business_date`         | `date`          | 否   | `无`                | 业务日期                                             |
| `created_at`            | `timestamptz`   | 否   | `CURRENT_TIMESTAMP` | 创建时间                                             |

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

| 字段              | PostgreSQL 类型 | 可空 | 默认值              | 含义与关联                       |
| ----------------- | --------------- | ---- | ------------------- | -------------------------------- |
| `id`              | `uuid`          | 否   | `无`                | 主键；应用生成 UUID；PK          |
| `legal_entity_id` | `uuid`          | 否   | `无`                | 所属法人；FK → `legal_entity.id` |
| `department_id`   | `uuid`          | 否   | `无`                | 预算部门；FK → `department.id`   |
| `period_start`    | `date`          | 否   | `无`                | 期间起日                         |
| `period_end`      | `date`          | 否   | `无`                | 期间止日                         |
| `subject_code`    | `varchar(64)`   | 否   | `无`                | 预算科目编码                     |
| `currency`        | `char(3)`       | 否   | `'CNY'`             | 币种                             |
| `approved_amount` | `numeric(20,2)` | 否   | `0`                 | 批准额度                         |
| `reserved_amount` | `numeric(20,2)` | 否   | `0`                 | 剩余占用                         |
| `executed_amount` | `numeric(20,2)` | 否   | `0`                 | 已执行金额                       |
| `status`          | `varchar(32)`   | 否   | `'OPEN'`            | 允许值：OPEN, CLOSED             |
| `lock_version`    | `integer`       | 否   | `1`                 | 余额版本                         |
| `updated_at`      | `timestamptz`   | 否   | `无`                | 更新时间                         |
| `created_at`      | `timestamptz`   | 否   | `CURRENT_TIMESTAMP` | 创建时间                         |

数据库约束与索引：

- PK：`id`。
- UQ：`(legal_entity_id, department_id, period_start, period_end, subject_code, currency)`。
- UQ：`(legal_entity_id, id)`。
- FK：`(legal_entity_id) → legal_entity(id)`。
- FK：`(legal_entity_id, department_id) → department(legal_entity_id, id)`。
- CHECK：`period_end >= period_start`。
- CHECK：`currency = 'CNY'`。
- CHECK：`reserved_amount >= 0`。
- CHECK：`executed_amount >= 0`。
- CHECK：`approved_amount >= reserved_amount + executed_amount`。
- CHECK：`lock_version > 0`。

补充约束与执行规则：

- 可用额查询计算，不另存一列。相同科目预算期间不得重叠，由配置服务在法人/部门范围串行校验；首期可统一年度期间。

<a id="table-budget_adjustment"></a>

### `budget_adjustment`：预算调整申请

所属模块：`finance`；类型：单据主表。

| 字段                      | PostgreSQL 类型 | 可空 | 默认值              | 含义与关联                                                       |
| ------------------------- | --------------- | ---- | ------------------- | ---------------------------------------------------------------- |
| `id`                      | `uuid`          | 否   | `无`                | 主键；沿用 business_document.id；FK → `business_document.id`；PK |
| `legal_entity_id`         | `uuid`          | 否   | `无`                | 所属法人；FK → `legal_entity.id`                                 |
| `budget_account_id`       | `uuid`          | 否   | `无`                | 预算账户；FK → `budget_account.id`                               |
| `delta_amount`            | `numeric(20,2)` | 否   | `0`                 | 带符号额度调整金额                                               |
| `reason`                  | `text`          | 否   | `无`                | 调整原因                                                         |
| `expected_budget_version` | `integer`       | 否   | `无`                | 提交时预算版本                                                   |
| `created_at`              | `timestamptz`   | 否   | `CURRENT_TIMESTAMP` | 创建时间                                                         |

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

| 字段                | PostgreSQL 类型 | 可空 | 默认值              | 含义与关联                            |
| ------------------- | --------------- | ---- | ------------------- | ------------------------------------- |
| `id`                | `uuid`          | 否   | `无`                | 主键；应用生成 UUID；PK               |
| `legal_entity_id`   | `uuid`          | 否   | `无`                | 所属法人；FK → `legal_entity.id`      |
| `budget_account_id` | `uuid`          | 否   | `无`                | 预算账户；FK → `budget_account.id`    |
| `order_line_id`     | `uuid`          | 否   | `无`                | 订单行；FK → `purchase_order_line.id` |
| `initial_amount`    | `numeric(20,2)` | 否   | `0`                 | 首次批准占用金额                      |
| `remaining_amount`  | `numeric(20,2)` | 否   | `0`                 | 未转执行或释放金额                    |
| `lock_version`      | `integer`       | 否   | `1`                 | 版本                                  |
| `created_at`        | `timestamptz`   | 否   | `CURRENT_TIMESTAMP` | 创建时间                              |

数据库约束与索引：

- PK：`id`。
- UQ：`(order_line_id)`。
- UQ：`(legal_entity_id, id)`。
- FK：`(legal_entity_id) → legal_entity(id)`。
- FK：`(legal_entity_id, budget_account_id) → budget_account(legal_entity_id, id)`。
- FK：`(legal_entity_id, order_line_id) → purchase_order_line(legal_entity_id, id)`。
- CHECK：`initial_amount >= 0`。
- CHECK：`remaining_amount BETWEEN 0 AND initial_amount`。
- CHECK：`lock_version > 0`。
- 普通索引：`(legal_entity_id, budget_account_id)`。
- 普通索引：`(legal_entity_id, order_line_id)`。

补充约束与执行规则：

- 每个已批准订单行一个占用明细；收货冲销可恢复 remaining_amount，不能高于初始额度。

<a id="table-budget_ledger"></a>

### `budget_ledger`：预算流水

所属模块：`finance`；类型：追加账本表。

| 字段                    | PostgreSQL 类型 | 可空 | 默认值              | 含义与关联                                                  |
| ----------------------- | --------------- | ---- | ------------------- | ----------------------------------------------------------- |
| `id`                    | `uuid`          | 否   | `无`                | 主键；应用生成 UUID；PK                                     |
| `legal_entity_id`       | `uuid`          | 否   | `无`                | 所属法人；FK → `legal_entity.id`                            |
| `budget_account_id`     | `uuid`          | 否   | `无`                | 账户；FK → `budget_account.id`                              |
| `reservation_id`        | `uuid`          | 是   | `无`                | 订单占用来源；FK → `budget_reservation.id`                  |
| `adjustment_id`         | `uuid`          | 是   | `无`                | 额度调整来源；FK → `budget_adjustment.id`                   |
| `receipt_line_id`       | `uuid`          | 是   | `无`                | 收货转执行来源；FK → `receipt_line.id`                      |
| `return_line_id`        | `uuid`          | 是   | `无`                | 退货来源；FK → `return_line.id`                             |
| `close_line_id`         | `uuid`          | 是   | `无`                | 取消余量来源；FK → `order_close_line.id`                    |
| `correction_request_id` | `uuid`          | 是   | `无`                | 冲销申请；FK → `correction_request.id`                      |
| `reversal_of_id`        | `uuid`          | 是   | `无`                | 被冲销流水；FK → `budget_ledger.id`                         |
| `posting_key`           | `varchar(160)`  | 否   | `无`                | 规范来源动作键                                              |
| `entry_type`            | `varchar(32)`   | 否   | `'RESERVE'`         | 允许值：ADJUST, RESERVE, EXECUTE, RELEASE, RETURN, REVERSAL |
| `quota_delta`           | `numeric(20,2)` | 否   | `0`                 | 额度变化                                                    |
| `reserved_delta`        | `numeric(20,2)` | 否   | `0`                 | 占用变化                                                    |
| `executed_delta`        | `numeric(20,2)` | 否   | `0`                 | 已执行变化                                                  |
| `business_date`         | `date`          | 否   | `无`                | 发生日期                                                    |
| `created_at`            | `timestamptz`   | 否   | `CURRENT_TIMESTAMP` | 创建时间                                                    |

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
- CHECK：`quota_delta <> 0 OR reserved_delta <> 0 OR executed_delta <> 0`。
- CHECK：`(entry_type = 'ADJUST' AND quota_delta <> 0 AND reserved_delta = 0 AND executed_delta = 0) OR (entry_type = 'RESERVE' AND quota_delta = 0 AND reserved_delta > 0 AND executed_delta = 0) OR (entry_type = 'EXECUTE' AND quota_delta = 0 AND reserved_delta < 0 AND executed_delta = -reserved_delta) OR (entry_type = 'RELEASE' AND quota_delta = 0 AND reserved_delta < 0 AND executed_delta = 0) OR (entry_type = 'RETURN' AND quota_delta = 0 AND reserved_delta = 0 AND executed_delta < 0) OR entry_type = 'REVERSAL'`。
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

- ADJUST 必须有 adjustment_id；其余均有 reservation_id。EXECUTE/RETURN/RELEASE 分别要求 receipt_line_id/return_line_id/close_line_id；RESERVE 禁止这些动作来源。REVERSAL 必须指原流水和 correction_request。按动作来源建立部分唯一索引，同一来源不可重复记账。符号和与库存/收货的一致性由原子用例校验；禁止 UPDATE/DELETE。
- 零金额订单行可创建零额 reservation，但不写零金额预算流水；金额为零不影响真实库存收货流水。

<a id="table-invoice"></a>

### `invoice`：发票主表

所属模块：`finance`；类型：单据主表。

| 字段                   | PostgreSQL 类型 | 可空 | 默认值              | 含义与关联                                                       |
| ---------------------- | --------------- | ---- | ------------------- | ---------------------------------------------------------------- |
| `id`                   | `uuid`          | 否   | `无`                | 主键；沿用 business_document.id；FK → `business_document.id`；PK |
| `legal_entity_id`      | `uuid`          | 否   | `无`                | 所属法人；FK → `legal_entity.id`                                 |
| `supplier_id`          | `uuid`          | 否   | `无`                | 开票供应商；FK → `supplier.id`                                   |
| `invoice_type`         | `varchar(32)`   | 否   | `无`                | 发票标识类型                                                     |
| `invoice_identifier`   | `varchar(100)`  | 否   | `无`                | 规范化完整票据标识                                               |
| `invoice_date`         | `date`          | 否   | `无`                | 开票日期                                                         |
| `currency`             | `char(3)`       | 否   | `'CNY'`             | 币种                                                             |
| `net_amount`           | `numeric(20,2)` | 否   | `0`                 | 未税合计                                                         |
| `tax_amount`           | `numeric(20,2)` | 否   | `0`                 | 税额合计                                                         |
| `gross_amount`         | `numeric(20,2)` | 否   | `0`                 | 含税应付                                                         |
| `source_attachment_id` | `uuid`          | 否   | `无`                | 发票凭证；FK → `attachment.id`                                   |
| `match_status`         | `varchar(32)`   | 否   | `'NOT_RUN'`         | 允许值：NOT_RUN, PASSED, FAILED, STALE                           |
| `confirmed_at`         | `timestamptz`   | 是   | `无`                | 复核确认时间                                                     |
| `created_at`           | `timestamptz`   | 否   | `CURRENT_TIMESTAMP` | 创建时间                                                         |

数据库约束与索引：

- PK：`id`。
- UQ：`(legal_entity_id, supplier_id, invoice_type, invoice_identifier)`。
- UQ：`(legal_entity_id, id)`。
- FK：`(legal_entity_id, id) → business_document(legal_entity_id, id)`。
- FK：`(legal_entity_id) → legal_entity(id)`。
- FK：`(legal_entity_id, supplier_id) → supplier(legal_entity_id, id)`。
- FK：`(legal_entity_id, source_attachment_id) → attachment(legal_entity_id, id)`。
- CHECK：`currency = 'CNY'`。
- CHECK：`gross_amount = net_amount + tax_amount`。
- CHECK：`gross_amount >= 0`。
- 普通索引：`(legal_entity_id, source_attachment_id)`。
- 主表类型须与 `business_document.document_type` 匹配，状态使用通用登记的当前状态。

补充约束与执行规则：

- 作废保留唯一票据标识；同票录入纠错创建 document_revision，不复制新的 invoice 主表。每个旧版本的金额以冻结 snapshot 为准。

<a id="table-invoice_line"></a>

### `invoice_line`：发票明细表

所属模块：`finance`；类型：单据明细表。

| 字段              | PostgreSQL 类型 | 可空 | 默认值              | 含义与关联                                    |
| ----------------- | --------------- | ---- | ------------------- | --------------------------------------------- |
| `id`              | `uuid`          | 否   | `无`                | 主键；应用生成 UUID；PK                       |
| `legal_entity_id` | `uuid`          | 否   | `无`                | 所属法人；FK → `legal_entity.id`              |
| `invoice_id`      | `uuid`          | 否   | `无`                | 所属主表；FK → `invoice.id`                   |
| `revision`        | `integer`       | 否   | `1`                 | 所属内容版本                                  |
| `line_no`         | `integer`       | 否   | `无`                | 当前内容版本内行号                            |
| `order_line_id`   | `uuid`          | 是   | `无`                | 明确对应订单行；FK → `purchase_order_line.id` |
| `quantity`        | `numeric(20,6)` | 否   | `无`                | 发票数量                                      |
| `unit_price`      | `numeric(20,6)` | 否   | `无`                | 未税单价                                      |
| `tax_rate`        | `numeric(9,6)`  | 否   | `无`                | 税率，如 0.130000                             |
| `net_amount`      | `numeric(20,2)` | 否   | `0`                 | 未税行金额                                    |
| `tax_amount`      | `numeric(20,2)` | 否   | `0`                 | 税额                                          |
| `gross_amount`    | `numeric(20,2)` | 否   | `0`                 | 含税行金额                                    |
| `created_at`      | `timestamptz`   | 否   | `CURRENT_TIMESTAMP` | 创建时间                                      |

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

<a id="table-invoice_match_run"></a>

### `invoice_match_run`：发票匹配批次

所属模块：`finance`；类型：分析快照表。

| 字段                   | PostgreSQL 类型 | 可空 | 默认值              | 含义与关联                                      |
| ---------------------- | --------------- | ---- | ------------------- | ----------------------------------------------- |
| `id`                   | `uuid`          | 否   | `无`                | 主键；应用生成 UUID；PK                         |
| `legal_entity_id`      | `uuid`          | 否   | `无`                | 所属法人；FK → `legal_entity.id`                |
| `invoice_id`           | `uuid`          | 否   | `无`                | 发票；FK → `invoice.id`                         |
| `invoice_revision`     | `integer`       | 否   | `无`                | 发票内容版本                                    |
| `invoice_lock_version` | `integer`       | 否   | `无`                | 试算时编辑版本                                  |
| `policy_version_id`    | `uuid`          | 否   | `无`                | 匹配规则版本；FK → `business_policy_version.id` |
| `result`               | `varchar(32)`   | 否   | `'FAILED'`          | 允许值：PASSED, FAILED                          |
| `source_versions`      | `jsonb`         | 否   | `'{}'::jsonb`       | 订单/收货/退货版本                              |
| `differences`          | `jsonb`         | 否   | `'[]'::jsonb`       | 逐字段差异数组                                  |
| `proposed_allocations` | `jsonb`         | 否   | `'[]'::jsonb`       | 建议分配数组                                    |
| `created_at`           | `timestamptz`   | 否   | `CURRENT_TIMESTAMP` | 创建时间                                        |

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

| 字段                     | PostgreSQL 类型 | 可空 | 默认值              | 含义与关联                                      |
| ------------------------ | --------------- | ---- | ------------------- | ----------------------------------------------- |
| `id`                     | `uuid`          | 否   | `无`                | 主键；应用生成 UUID；PK                         |
| `legal_entity_id`        | `uuid`          | 否   | `无`                | 所属法人；FK → `legal_entity.id`                |
| `invoice_line_id`        | `uuid`          | 否   | `无`                | 准确发票版本明细；FK → `invoice_line.id`        |
| `receipt_line_id`        | `uuid`          | 否   | `无`                | 收货来源行；FK → `receipt_line.id`              |
| `match_run_id`           | `uuid`          | 否   | `无`                | 建立分配的核对批次；FK → `invoice_match_run.id` |
| `allocated_quantity`     | `numeric(20,6)` | 否   | `无`                | 分配数量                                        |
| `allocated_gross_amount` | `numeric(20,2)` | 否   | `0`                 | 分配发票含税额                                  |
| `status`                 | `varchar(32)`   | 否   | `'HELD'`            | 允许值：HELD, ACTIVE, RELEASED                  |
| `released_at`            | `timestamptz`   | 是   | `无`                | 释放时间                                        |
| `created_at`             | `timestamptz`   | 否   | `CURRENT_TIMESTAMP` | 创建时间                                        |

数据库约束与索引：

- PK：`id`。
- UQ：`(legal_entity_id, id)`。
- FK：`(legal_entity_id) → legal_entity(id)`。
- FK：`(legal_entity_id, invoice_line_id) → invoice_line(legal_entity_id, id)`。
- FK：`(legal_entity_id, receipt_line_id) → receipt_line(legal_entity_id, id)`。
- FK：`(legal_entity_id, match_run_id) → invoice_match_run(legal_entity_id, id)`。
- CHECK：`allocated_quantity > 0`。
- CHECK：`allocated_gross_amount >= 0`。
- 普通索引：`(legal_entity_id, invoice_line_id)`。
- 普通索引：`(legal_entity_id, receipt_line_id)`。
- 普通索引：`(legal_entity_id, match_run_id)`。

补充约束与执行规则：

- 部分唯一索引 (invoice_line_id, receipt_line_id) WHERE status IN (HELD, ACTIVE)。未释放分配之和不得超净收货，此跨行规则需锁收货行后校验，不用单行 CHECK 假装保证。发票各行分配金额之和必须与当前版本一致。

<a id="table-payment_record"></a>

### `payment_record`：付款登记主表

所属模块：`finance`；类型：单据主表。

| 字段                  | PostgreSQL 类型 | 可空 | 默认值              | 含义与关联                                                       |
| --------------------- | --------------- | ---- | ------------------- | ---------------------------------------------------------------- |
| `id`                  | `uuid`          | 否   | `无`                | 主键；沿用 business_document.id；FK → `business_document.id`；PK |
| `legal_entity_id`     | `uuid`          | 否   | `无`                | 所属法人；FK → `legal_entity.id`                                 |
| `supplier_id`         | `uuid`          | 否   | `无`                | 收款供应商；FK → `supplier.id`                                   |
| `paid_date`           | `date`          | 否   | `无`                | 实际付款日期                                                     |
| `currency`            | `char(3)`       | 否   | `'CNY'`             | 币种                                                             |
| `amount`              | `numeric(20,2)` | 否   | `0`                 | 实际付款金额                                                     |
| `payment_channel`     | `varchar(32)`   | 否   | `无`                | 付款渠道                                                         |
| `external_reference`  | `varchar(128)`  | 否   | `无`                | 规范化外部流水号                                                 |
| `proof_attachment_id` | `uuid`          | 否   | `无`                | 付款凭证；FK → `attachment.id`                                   |
| `confirmed_at`        | `timestamptz`   | 是   | `无`                | 复核确认时间                                                     |
| `created_at`          | `timestamptz`   | 否   | `CURRENT_TIMESTAMP` | 创建时间                                                         |

数据库约束与索引：

- PK：`id`。
- UQ：`(legal_entity_id, payment_channel, external_reference)`。
- UQ：`(legal_entity_id, id)`。
- FK：`(legal_entity_id, id) → business_document(legal_entity_id, id)`。
- FK：`(legal_entity_id) → legal_entity(id)`。
- FK：`(legal_entity_id, supplier_id) → supplier(legal_entity_id, id)`。
- FK：`(legal_entity_id, proof_attachment_id) → attachment(legal_entity_id, id)`。
- CHECK：`amount > 0`。
- CHECK：`currency = 'CNY'`。
- 普通索引：`(legal_entity_id, supplier_id)`。
- 普通索引：`(legal_entity_id, proof_attachment_id)`。
- 主表类型须与 `business_document.document_type` 匹配，状态使用通用登记的当前状态。

补充约束与执行规则：

- 冲销后保留原付款对象与流水号。录入错误更正使用同一单据新内容版本，旧分配保留；银行实际退款另走人工例外。

<a id="table-payment_allocation"></a>

### `payment_allocation`：付款发票分配明细

所属模块：`finance`；类型：单据明细表。

| 字段                | PostgreSQL 类型 | 可空 | 默认值              | 含义与关联                            |
| ------------------- | --------------- | ---- | ------------------- | ------------------------------------- |
| `id`                | `uuid`          | 否   | `无`                | 主键；应用生成 UUID；PK               |
| `legal_entity_id`   | `uuid`          | 否   | `无`                | 所属法人；FK → `legal_entity.id`      |
| `payment_record_id` | `uuid`          | 否   | `无`                | 所属主表；FK → `payment_record.id`    |
| `revision`          | `integer`       | 否   | `1`                 | 所属内容版本                          |
| `line_no`           | `integer`       | 否   | `无`                | 当前内容版本内行号                    |
| `invoice_id`        | `uuid`          | 否   | `无`                | 对应发票；FK → `invoice.id`           |
| `invoice_revision`  | `integer`       | 否   | `无`                | 确认的发票版本                        |
| `allocated_amount`  | `numeric(20,2)` | 否   | `0`                 | 本次分配付款金额                      |
| `status`            | `varchar(32)`   | 否   | `'DRAFT'`           | 允许值：DRAFT, HELD, ACTIVE, RELEASED |
| `released_at`       | `timestamptz`   | 是   | `无`                | 释放时间                              |
| `created_at`        | `timestamptz`   | 否   | `CURRENT_TIMESTAMP` | 创建时间                              |

数据库约束与索引：

- PK：`id`。
- UQ：`(payment_record_id, revision, line_no)`。
- UQ：`(payment_record_id, revision, invoice_id, invoice_revision)`。
- UQ：`(legal_entity_id, id)`。
- FK：`(legal_entity_id) → legal_entity(id)`。
- FK：`(legal_entity_id, payment_record_id) → payment_record(legal_entity_id, id)`。
- FK：`(legal_entity_id, invoice_id) → invoice(legal_entity_id, id)`。
- CHECK：`revision > 0`。
- CHECK：`line_no > 0`。
- CHECK：`allocated_amount > 0`。
- 普通索引：`(legal_entity_id, payment_record_id)`。
- 普通索引：`(legal_entity_id, invoice_id)`。

补充约束与执行规则：

- 复合外键 (payment_record_id, revision) → document_revision(document_id, revision)。修改冻结版本时建立新行 ID，旧行保留供下游引用。
- 复合外键 (invoice_id, invoice_revision) → document_revision(document_id, revision)。提交后 HELD 占可付额，复核 ACTIVE；退回或冲销 RELEASED。修改状态属于执行元数据，冻结保护数量与金额等内容，不禁止合法状态转换。

## 10. 跨模块流程模块

<a id="table-correction_request"></a>

### `correction_request`：跨模块纠错申请

所属模块：`workflows`；类型：单据主表。

| 字段                   | PostgreSQL 类型 | 可空 | 默认值              | 含义与关联                                                                                           |
| ---------------------- | --------------- | ---- | ------------------- | ---------------------------------------------------------------------------------------------------- |
| `id`                   | `uuid`          | 否   | `无`                | 主键；沿用 business_document.id；FK → `business_document.id`；PK                                     |
| `legal_entity_id`      | `uuid`          | 否   | `无`                | 所属法人；FK → `legal_entity.id`                                                                     |
| `original_document_id` | `uuid`          | 否   | `无`                | 原单据；FK → `business_document.id`                                                                  |
| `original_revision`    | `integer`       | 否   | `无`                | 被纠错版本                                                                                           |
| `correction_type`      | `varchar(32)`   | 否   | `'RECEIPT_REVERSE'` | 允许值：RECEIPT_REVERSE, STOCK_REVERSE, INVOICE_ENTRY_VOID, INVOICE_REAL_VOID, PAYMENT_ENTRY_REVERSE |
| `reason`               | `text`          | 否   | `无`                | 纠错原因                                                                                             |
| `source_manifest`      | `jsonb`         | 否   | `'{}'::jsonb`       | 具体来源流水 ID 和预期版本清单                                                                       |
| `executed_at`          | `timestamptz`   | 是   | `无`                | 获批原子执行时间                                                                                     |
| `created_at`           | `timestamptz`   | 否   | `CURRENT_TIMESTAMP` | 创建时间                                                                                             |

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

| 字段                  | PostgreSQL 类型 | 可空 | 默认值              | 含义与关联                                                       |
| --------------------- | --------------- | ---- | ------------------- | ---------------------------------------------------------------- |
| `id`                  | `uuid`          | 否   | `无`                | 主键；沿用 business_document.id；FK → `business_document.id`；PK |
| `legal_entity_id`     | `uuid`          | 否   | `无`                | 所属法人；FK → `legal_entity.id`                                 |
| `related_document_id` | `uuid`          | 是   | `无`                | 关联单据；FK → `business_document.id`                            |
| `case_type`           | `varchar(64)`   | 否   | `无`                | 异常类别                                                         |
| `assignee_id`         | `uuid`          | 否   | `无`                | 负责人；FK → `app_user.id`                                       |
| `description`         | `text`          | 否   | `无`                | 事实描述                                                         |
| `resolution`          | `text`          | 是   | `无`                | 处理结论                                                         |
| `proof_attachment_id` | `uuid`          | 是   | `无`                | 处理凭证；FK → `attachment.id`                                   |
| `resolved_at`         | `timestamptz`   | 是   | `无`                | 解决时间                                                         |
| `created_at`          | `timestamptz`   | 否   | `CURRENT_TIMESTAMP` | 创建时间                                                         |

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

## 11. 关键关系与跨表约束

| 关系              | 基数与引用                                     | 数据库可直接保证            | 事务中必须校验                           |
| ----------------- | ---------------------------------------------- | --------------------------- | ---------------------------------------- |
| 申请 → 申请明细   | 1:N，purchase_request_line.purchase_request_id | FK、当前版本内行号唯一      | 仅当前批准版本可下单                     |
| 定标 → 订单明细   | 1:N，purchase_order_line.award_line_id         | FK                          | 有效订单量不超过获批分配                 |
| 订单行 → 收货行   | 1:N，receipt_line.order_line_id                | FK                          | 收货与取消量不能超过订单义务             |
| 收货行 → 退货行   | 1:N，return_line.receipt_line_id               | FK                          | 可退量、仓库库存与未分配发票数量         |
| 发票行 ↔ 收货行   | M:N，通过 invoice_allocation                   | 两侧 FK、活跃配对唯一       | HELD+ACTIVE 分配量不超净收货             |
| 付款 ↔ 发票       | M:N，通过 payment_allocation                   | 两侧 FK、内容版本内配对唯一 | 待复核+有效付款不超应付                  |
| 订单行 → 预算占用 | 1:0..1，budget_reservation.order_line_id       | UQ+FK                       | 批准时占用与订单状态原子提交             |
| 单据 → 审批实例   | 1:N，按 revision 区分                          | 版本 FK、活跃实例部分唯一   | 具体审批人、职责分离、最后节点业务生效   |
| 原流水 → 冲销流水 | 1:0..1，自引用 reversal_of_id                  | FK+UQ                       | 同账户/物料、金额/数量完全反向、来源一致 |

对于同法人但不同父单据的关联，还须验证来源链：例如报价行确实属于邀请的询价、收货行确实属于主表订单、发票与付款供应商相同。这些在领域服务持锁后校验；不把只校验单列 ID 存在的外键描述成已校验完整业务关系。

## 12. 账本与汇总核对

| 汇总字段                       | 来源口径                                                                             |
| ------------------------------ | ------------------------------------------------------------------------------------ |
| stock_balance.quantity         | 同法人、仓库、物料的 stock_ledger.delta_quantity 合计                                |
| budget_account.approved_amount | 同账户 budget_ledger.quota_delta 合计                                                |
| budget_account.reserved_amount | 同账户 budget_ledger.reserved_delta 合计，并与 reservation.remaining_amount 合计相符 |
| budget_account.executed_amount | 同账户 budget_ledger.executed_delta 合计                                             |
| 已收量/正常退货量              | 生效的收货/退货行及对应冲销，不统计草稿或历史替代版本                                |
| 已开票数量                     | invoice_allocation 中 ACTIVE 数量；HELD 另列但同样占可分配量                         |
| 已付款金额                     | payment_allocation 中 ACTIVE 金额；HELD 另列但同样占可付额                           |

逐行金额按业务设计舍入，主表金额等于当前内容版本明细之和。汇总一致性不通过跨表 CHECK 实现：同事务更新、来源唯一约束、定期对账三者共同保证。发现不一致记录异常，不能直接把余额覆盖为期望值。

## 13. 建表顺序、数据保护与验证

1. 先定义全部表与主键/唯一键，再分批加入外键；迁移采用确定顺序处理合法的前向引用。公共主数据先初始化，随后建立业务单据与审批、库存财务、集成记录。
2. 为通用登记/内容版本的创建循环设置指定的延迟外键，增加领域类型与主表存在性、冻结内容保护、流水禁止改写的约束触发器；不是把所有外键都改成延迟检查。
3. 初始化发布计数器、法人、部门、单位、角色、审批与业务规则。账号密码经安全初始化过程设置；不在 SQL 或文档中提供固定生产密码。
4. 工作流事务角色仅通过受控服务更新数据；助手没有业务库权限。迁移账号与运行账号分离，运行账号无 DDL 权限。
5. 首期不分区、不建设向量表；按真实查询计划合并候选索引。审计、变化、流水增长后再评估归档与分区，保留历史业务引用。
6. 本文所有设计约束最终以 Alembic 迁移和 PostgreSQL 集成测试落实。文档校验不等于数据库执行验证；目前尚未建库或运行 SQL 迁移。

建库后的必测项：跨法人外键拒绝、重复业务键拒绝、主明细版本正确、历史明细不可修改、退回重提版本分离、同键重复动作只生效一次、并发预算和收货/发票分配不超量、流水冲销唯一、变化发布晚提交不漏记录。业务端到端场景沿用业务设计中的 A01—A13。
