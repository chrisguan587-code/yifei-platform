# Yifei Shared Platform 提取清单

> 版本：v1.0（2026-07-12 代码审计版）
> 定位：把当前 V3 仓库中的公共资源逐项分类，定义提取方式、公共契约、测试缺口和迁移批次。
> 上位边界：[121-Yifei-Shared-Platform-and-Application-Boundary.md](./121-Yifei-Shared-Platform-and-Application-Boundary.md)。本文是执行清单，不改变 V4 业务架构。

---

## 1. 审计结论

现有仓库具备建设 Shared Platform 的主要原料，但尚不存在可以整体搬迁的“公共层”。当前代码普遍同时承担市场事实、V3 配置、仓库路径和业务输出职责，必须按契约拆分，不能复制目录后改名。

```text
当前代码
  ├─ 中性事实/通用能力        → 提取到 yifei-platform
  ├─ V3 配置/判断/状态         → 留在 yifei-v3
  └─ 旧接口兼容                → V3 adapter，完成迁移后删除
```

开工判断：

1. `TradingCalendar` 最接近直接提取，但仍要冻结降级和日期偏移语义。
2. `MarketDataReader`、`DataQualitySnapshot`、`ReadinessMarker` 当前没有独立契约，需要从多个服务中组合后重建中性接口。
3. `ArtifactEnvelope` 当前只有 writer/index 雏形，不能把现有 V3 payload 版本当平台 schema 版本。
4. `OutcomeCalculator` 的算法已有测试基础，但与 V3 `CanonicalEventLoader`、Evidence Matrix 和本仓路径混在一起，必须拆类。
5. Batch B/C 不能阻塞 V4 对象与 Repository 开发，但对应能力进入真实纵向链路前必须完成。

---

## 2. 分类规则

| 提取类型 | 定义 |
|:--|:--|
| Direct | 业务语义中性，只需改包名、配置和补契约测试 |
| Split | 同一模块混合公共事实与应用判断，按所有权拆开 |
| Adapter | 先保持旧消费者行为，通过薄适配层迁移到平台接口 |
| Remain V3 | 属于 V3 判断、状态、展示或样本语义，不进入平台 |

公共契约统一要求：

```text
显式 as_of / source_version / schema_version
明确 missing / degraded / stale 语义
只读事实接口不泄露数据库内部表结构
确定性输出与幂等行为
平台包不 import v3 或 v4
consumer contract tests 同时保护 V3 与 V4
```

---

## 3. Batch A：V4 开工前最小平台

| Current Module | Current Owner | Data / Capability | V3 Coupling | Target Owner | Extraction | Public Contract | Consumers | Tests | Risk |
|:--|:--|:--|:--|:--|:--|:--|:--|:--|:--|
| `prod/calendar/trading_calendar.py`、`a_share_calendar_backend.py` | 混合仓库，语义基本中性 | 交易日判断、前后交易日、日期上下文 | 默认 backend、本地 override、可选 XSHG 降级未形成版本契约 | Platform | Direct + Adapter | `TradingCalendarV1`：`is_session`、`resolve_session`、`offset_session`、`context` | V3/V4/未来 | 有调用方测试，缺独立节假日、双源冲突、缺依赖降级和 T+N 契约测试 | 双源取 OR 可能把错误日历放成交易日；必须冻结该语义后再迁移 |
| `prod/services/data_service.py`、`prod/core/data_runtime.py` | V3 runtime | `stock_daily` 读取、latest date、freshness、runtime DB 管理 | `ProjectPaths`、`DEFAULT_PATHS`、`v3_tradable_*` SQL、建表/备份/读取混在一起 | Platform + V3 | Split + Adapter | `MarketDataReaderV1`：按 `as_of` 读取股票/指数/板块事实，返回 rows + source metadata；应用不得传业务筛选 SQL | V3/V4/未来 | `test_data_service_contract`、`test_runtime_data_contract` 可作行为基线；缺 point-in-time、只读权限、分页/批量、缺表/缺列、schema version 测试 | 最大风险是把 V3 Universe 隐式带入公共读取；DB 路径和 schema 漂移次之 |
| `prod/data_gates/eod_gate.py`、`prod/data/integrity.py`、`health_refresh.py`、`prod/services/data_health_service.py` | V3 EOD/health | freshness、coverage、完整性、降级原因 | gate stage/action、V3 表清单和运行顺序与事实质量混合 | Platform + V3 | Split + Adapter | `DataQualitySnapshotV1`：dataset、as_of、observed_at、freshness、coverage、status、reason codes、source refs | V3/V4/未来 | 有 EOD repair、runtime/data service 测试；缺统一状态枚举、每数据集 coverage、stale/degraded 合同和不可变快照测试 | 若把“是否继续 V3 pipeline”放入快照，会污染 V4；质量事实与消费策略必须分离 |
| `prod/data_gates/eod_gate.py`、scheduler stage completion、artifact index | V3 scheduler | 某交易日公共数据是否可消费 | 当前 readiness 隐含在 EOD gate 成功和文件存在中，没有单一标记 | Platform | Split | `ReadinessMarkerV1`：dataset bundle、as_of、status、quality_snapshot_ref、producer_version、completed_at | V3/V4/未来 | 几乎无独立测试；需补原子发布、重复发布、失败不覆盖 ready、过期 marker、消费者只读测试 | 假 ready 会让所有应用读取半成品，是 Batch A 最高运行风险 |
| `prod/delivery/artifacts.py`、`prod/version.py` | V3 delivery | JSON 写入、路径、latest/index | file version、producer、payload schema 和 V3 artifact type 混合；调用方大量依赖路径布局 | Platform + Applications | Split + Adapter | `ArtifactEnvelopeV1`：artifact_id、producer、producer_version、schema_name/version、as_of、created_at、source_refs、payload checksum；payload 归应用 | V3/V4/未来 | `test_version_governance` 等覆盖 writer 基本行为；缺 envelope schema、原子写、checksum、并发 index、跨 producer namespace 和兼容读取测试 | 直接改 writer 会影响大量 V3 调用方；必须先加 adapter，不做全仓同步重写 |
| `attribution/engine.py` | V3 attribution | T+1/T+3/T+5、区间极值、聚合统计 | 接受 `CanonicalEvent`，直接读 V3 runtime DB，混合 V3 loader、lifecycle artifact、Evidence Matrix | Platform + V3 | Split + Adapter | `OutcomeCalculatorV1`：输入 instrument、observation_session、price basis、windows；输出 target sessions、returns、MFE、MAE、max drawdown、missing reasons | V3/V4/未来 | `test_attribution_engine` 已覆盖交易日窗口、零价格和部分极值；缺独立输入 DTO、复权/停牌/缺日、价格口径、批量确定性与 V3 等价测试 | 价格基准和 MFE/MAE 定义若不冻结，会让跨版本归因不可比；严禁迁移 V3 sample loader |

### 3.1 Batch A 明确保留在 V3 的部分

- `v3_tradable_stock_daily` 视图及 `v3_tradable_stock_daily_where_sql`。
- `CanonicalEventLoader`、`strategy_id + code + trade_date` 样本身份。
- Evidence Matrix 中 lifecycle、strategy、family lane 和 V3 artifact 拼接逻辑。
- V3 artifact payload、卡片字段和业务目录名。
- EOD Gate 对 V3 stage 的继续、降级或终止决定。

### 3.2 Batch A 完成定义

1. 六项契约以 `v1` 发布，类型和缺失语义可被测试验证。
2. Platform 对公共市场库只拥有规定写入口；应用 reader 使用 SQLite read-only URI 或等价权限。
3. V3 通过 adapter 跑过行为等价测试；V4 consumer contract test 不 import `prod.*`。
4. Readiness 只有在数据写入、质量快照和 schema 校验全部成功后原子发布。
5. Outcome Calculator 对固定 fixture 的 T+1/T+3/T+5 与极值输出和迁移前一致。

---

## 4. Batch B：首条真实纵向链路

| Current Module | Data / Capability | V3 Coupling | Target Owner | Extraction | Public Contract | Tests / Missing | Risk |
|:--|:--|:--|:--|:--|:--|:--|:--|
| `prod/market_rules/rules.py`、`tradable_universe.py` | 代码标准化、市场板块、ST/退市、涨跌停、流动性事实 | `MarketRules.min_daily_amount` 是应用阈值；supported segment 和所有 `v3_*` SQL 是 V3 Universe | Platform primitives + V3 policy | Split | `EligibilityFactsV1` 返回 segment、ST/退市、交易状态、原始流动性字段；V3/V4 各自决定 eligibility | `test_tradable_universe` 可保 V3 等价；缺市场制度版本、生效日期、未知字段和平台事实测试 | 市场规则随制度时间变化，不能用无日期常量永久解释历史 |
| `prod/boards/*`、公共板块/映射表 | 板块日事实、成员映射、freshness、资金事实 | `board_regime` 混合 top5、mainline、action、confidence、position coefficient 和 fallback 判断 | Platform facts + V3 interpretation | Split | `BoardFactReaderV1`、`BoardMembershipReaderV1`：原始/基础派生事实 + freshness/source refs | board alias/freshness/regime 测试可拆基线；缺 point-in-time membership、来源冲突、事实与 action 隔离测试 | 直接复用 regime 会把 V3 市场判断注入 V4 Context/Maturity |
| 资金事实表及 `prod/intraday/capital_flow.py` 等读取逻辑 | 个股/板块成交、换手、资金流变化 | 排名、候选池、阈值、artifact 输出常带 V3 策略语义 | Platform facts + Application detector | Split | `CapitalFactReaderV1`：原始值、可复用变化量、window、source/freshness；不返回机会分数 | 现有测试分散；需固定单位、正负方向、窗口、缺失、重复日期和 PIT 测试 | 数据源口径和单位漂移会制造伪异常，必须附 source version |
| `prod/delivery/notifier.py`、`payload.py` | 发送、重试、错误状态、飞书通道 | V3 卡片组装、标题、字段选择和业务降级混在 transport | Platform transport + App renderer | Split + Adapter | `NotificationTransportV1.send(rendered_message, idempotency_key)` 返回 transport receipt；Renderer 归应用 | 有发送失败告警和 preview 类测试；缺 transport fake、幂等、重试边界、renderer 不可见性测试 | 重试若无幂等键会重复推送；平台不得理解 WATCHING/MATURE |

Batch B 原则：Eligibility 只提供资格事实；Board/Capital reader 只提供带来源的事实；NotificationTransport 只发送已渲染内容。Qualification、Context、Maturity 和卡片语义全部留在应用。

---

## 5. Batch C：平台运行与扩展能力

| Current Module | Target Classification | Public Boundary | Migration Trigger | Main Risk |
|:--|:--|:--|:--|:--|
| `prod/data/providers/*` | Platform / Split | provider adapter、rate limit、circuit breaker、raw provenance | Batch A readers 稳定且需要平台独立更新市场库时 | provider fallback 可能混淆来源和修订历史 |
| `prod/data/orchestrator.py`、`update_service.py`、repair | Platform / Split | dataset update job、原子发布、repair audit；不调用应用 | Platform 独立 Runner 建立时 | 当前更新、建表、修复和 V3 EOD 顺序耦合 |
| `prod/health/*`、source probes、fault delivery | Platform + App health / Split | Platform 只报告数据源和自身运行健康；应用各报业务 pipeline health | V4 真实日运行前 | “平台健康”不能等同“应用可决策” |
| `prod/core/runtime.py`、locks/logging/telemetry | Platform primitives / Adapter | run id、lock、structured log、metric、alert transport | 多 Runner 并存前 | 共享锁名或目录会导致 V3/V4 相互阻塞 |
| `prod/features/*` | 逐项判断，不整体提取 | 只有定义稳定、与应用判断无关、可 PIT 回放的基础特征进入平台 | V4 Detector 出现明确重复计算需求后 | 提前公共化会冻结未经验证的 V3 特征语义 |
| `prod/sequence/timeline.py`、板块结构计算 | Platform facts 或 Application interpretation / Split | 原始序列事实可共享；Pattern、anchor、state label 留在应用 | 有两个以上独立消费者且定义一致时 | 最容易把 V3 Pattern 变成 V4 Discovery 入口 |
| `prod/core/paths.py` | Replace | 平台使用显式配置对象，不推断仓库根目录；各应用拥有自己的路径配置 | M0 三仓建立时 | 当前相对仓库路径会制造隐式跨仓读写 |

Batch C 不采用“现有功能全部公共化”。满足以下全部条件才提取：

```text
至少存在明确公共事实或通用运行能力
不包含 Candidate / Setup / Pattern / Score / State / Action 语义
存在稳定输入输出和第二个真实消费者
可以进行 point-in-time 回放
迁移收益高于维护 adapter 的成本
```

---

## 6. 目标依赖与数据流

```text
Data Providers
    ↓
Platform Update + Quality
    ├─ market_data.db
    ├─ DataQualitySnapshot
    └─ ReadinessMarker
            ↓
    Versioned Read Contracts
       ├───────────────┐
       ↓               ↓
    V3 Adapter      V4 Consumer
       ↓               ↓
    V3 App DB       V4 Opportunity DB

Application Renderer
    ↓ rendered message
NotificationTransport

Application Sample Builder
    ↓ observation identity
OutcomeCalculator
    ↓ neutral outcomes
Application Attribution
```

禁止反向依赖和旁路：

```text
Platform -X→ V3/V4 package
V4 -X→ V3 adapter / V3 DB / V3 artifact
Application -X→ shared DB internal tables outside reader contract
OutcomeCalculator -X→ application sample selection
```

---

## 7. 迁移执行顺序

### A0：契约 fixture

冻结一组最小历史 fixture：交易日、停牌/缺失、ST/退市、板块映射、资金事实、数据不完整日和后效价格。先记录当前合法行为，不把已知 V3 业务耦合误认作平台预期。

### A1：纯读取与日历

实现 `TradingCalendarV1` 和 `MarketDataReaderV1`，使用显式 DB 配置和只读连接。V3 adapter 保持现有消费者接口。

### A2：质量与发布

实现 `DataQualitySnapshotV1` 和 `ReadinessMarkerV1`。先写数据，再写不可变质量快照，最后原子发布 readiness。

### A3：Artifact 与 Outcome

拆出 `ArtifactEnvelopeV1` 和 `OutcomeCalculatorV1`。先由 V3 adapter 验证等价，再给 V4 使用；不迁移 V3 sample loader。

### B1：Eligibility 与事实 reader

拆出市场制度事实、Board/Capital readers。V4 的 Eligibility、Detector 和 Context 在 V4 仓库消费这些事实，自行保存当时证据快照。

### B2：Transport

拆 Renderer 与 Transport，以幂等 receipt 验证 V3/V4 可独立发送。

### C：运行所有权迁移

最后迁 providers、update orchestration、health 和 telemetry。迁移期间只能有一个 market data writer；完成切换后删除旧 writer 入口，避免双写。

---

## 8. 测试矩阵

| Test Layer | 必须证明 |
|:--|:--|
| Platform unit | 每个契约在 missing、stale、degraded、正常输入下语义确定 |
| Golden fixture | 迁移前后日历、事实读取、质量和后效结果等价 |
| Consumer contract | V3 adapter 与 V4 consumer 分别只依赖公开契约 |
| Dependency scan | Platform 不 import Application；V4 不 import V3；应用不直接写 shared DB |
| Permission test | 应用使用只读凭据/连接，写 shared DB 必须失败 |
| Replay test | 相同 `as_of + source_version` 重放得到相同事实和 outcome |
| Failure test | 半成品数据、provider 失败、artifact 写失败时不得发布 ready |
| Retirement test | 停止 V3 Runner 后 Platform 和 V4 contract/runner 仍正常 |

当前测试不是零基础，但主要保护 V3 行为。提取前不能只把现有测试改 import 后宣称完成；必须新增平台契约测试和跨消费者测试。

---

## 9. 风险与决策

### P0

1. Readiness 当前没有独立真实来源，必须先设计原子发布协议，不能用“文件存在”代替。
2. MarketDataReader 不得内置 V3 Universe，否则 V4 Discovery 召回率分母从入口就被污染。
3. Outcome 价格基准、交易日偏移、MFE/MAE 和停牌缺失语义必须版本化，否则 V3/V4 结果不可比较。

### P1

1. ArtifactWriter 调用面广，采用 adapter 渐进迁移，禁止一次全仓改写。
2. Board Regime 和 Notification 必须按事实/判断、transport/renderer 拆分。
3. 共享 DB 在迁移期只允许单 writer；任何双写方案都必须拒绝。

### 明确不做

- 不在本轮移动代码、创建 V4 业务模块或设计 Detector 参数。
- 不把 V3 的 thresholds、score、state、action、sample identity 公共化。
- 不以“未来也许复用”为理由提取未出现第二消费者的复杂特征。
- 不要求 Batch C 完成后才开始 V4 的对象、Schema 和 Repository 开发。

---

## 10. 下一步执行入口

最稳的下一步不是继续盘点，而是进入 `A0 → A1`：

1. 在独立 `yifei-platform` 仓库建立最小包、版本、CI 和依赖扫描。
2. 从本清单定义 golden fixture 与六项 Batch A contract tests。
3. 先提取 `TradingCalendarV1`、`MarketDataReaderV1`，保留 V3 adapter。
4. 通过行为等价和只读权限测试后，再推进 Quality/Readiness。

在 A1 验收前，V4 可以并行实现纯业务对象与 Repository，但不能接入当前 `prod.*` 作为正式依赖。
<!-- Repository ownership: yifei-platform -->
