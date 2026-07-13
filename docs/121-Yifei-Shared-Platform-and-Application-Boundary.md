# Yifei — Shared Platform and Application Boundary

> 版本：v1.2（2026-07-12 LLM 运行链隔离版）
> 定位：定义 Yifei 公共资源、V3、V4 及后续所有应用之间的所有权、依赖方向、仓库、数据和升级原则。
> 权威边界：凡涉及公共资源归属、跨应用依赖、数据库所有权、共享代码提取和应用退役，以本文为准。
> 提取执行：现有模块审计、迁移批次、公共契约候选与测试缺口见 [122-Yifei-Shared-Platform-Extraction-Inventory.md](./122-Yifei-Shared-Platform-Extraction-Inventory.md)。

---

## 1. 永久原则

```text
公共平台拥有事实与通用能力；应用拥有解释、状态与决策。

依赖只能：Application → Versioned Shared Platform。
禁止：Application → Application。
禁止：Shared Platform → Application。
```

展开为六条：

1. 市场事实只生产一次，由 Shared Platform 负责质量和版本。
2. V3、V4、V5 及后续模块是彼此独立的 Application，不互相 import、不互相写库、不以对方成功运行为前提。
3. 应用可以读取公共事实，只能写自己的业务状态。
4. 公共平台不能出现 Strategy、Setup、MATURE、ACTIONABLE 等应用语义。
5. 跨应用比较只能读取双方版本化输出，不能反向影响任何应用。
6. 任一应用停止或删除后，其他应用仍可在 Shared Platform 上独立运行。

一句话：

```text
共享事实，不共享判断；复用能力，不复用业务状态。
```

---

## 2. 系统边界

```text
                         Yifei Shared Platform
             Market Data / Calendar / Quality / Transport
                    /              |              \
                   ↓               ↓               ↓
             V3 Application   V4 Application   Future Application
                   ↓               ↓               ↓
             V3 State/Attr.   V4 State/Attr.   Own State/Attr.

Optional Comparison Tool
    只读各应用 Evaluation Output，不属于任何应用运行主链

External LLM Analysis Tool
    由人工按需调用，只读版本化输出，不进入任何 Runner，不写任何应用数据库
```

Shared Platform 是产品族底座，不是 V3 的子目录或遗留代码别名。

---

## 3. 仓库与发布

目标仓库：

```text
yifei-platform
yifei-v3
yifei-v4
```

### yifei-platform

拥有：

- 数据源、更新、补丁和完整性检查。
- 交易日历和 `as_of` 时间语义。
- 市场数据库 Schema 与只读访问接口。
- Data Quality / Health / Readiness。
- 基础 Eligibility primitives，不决定应用机会。
- Artifact Envelope 和索引协议。
- Notification Transport，不包含应用卡片渲染。
- Outcome Calculator 和通用统计原语，不包含应用样本加载器。
- 日志、告警、运行锁和 telemetry 原语。

### yifei-v3

拥有：

- Strategy、Candidate、Watch/Trigger。
- Confirmation、Trade Intent、Family Lane。
- ACTIONABLE、daily_top 和 V3 卡片。
- V3 Strategy Attribution Sample Builder。

进入维护和可退役状态，不作为 V4 运行依赖。

### yifei-v4

拥有：

- DiscoveryEvent、Qualification、Routing。
- Opportunity Setup、Understanding、Risk、Maturity、State Machine。
- Daily Review、V4 Renderer。
- V4 Attribution Capture、Setup Matrix、Knowledge。
- V4 只以系统判断和市场后效形成学习闭环，不保存或拟合人工查看、跳过和买卖偏好。

每个仓库独立拥有版本、CI、测试、配置、release、changelog 和运行入口。应用通过明确版本依赖平台，不通过相对路径或复制源码复用。

---

## 4. 数据所有权

```text
data/shared/market_data.db
    owner: yifei-platform
    writers: platform only
    readers: V3 / V4 / future apps

data/v3/application.db
    owner: yifei-v3
    writers/readers: V3 only

data/v4/opportunity.db
    owner: yifei-v4
    writers/readers: V4 only
```

### 公共市场库保存

- stock/index/board daily facts。
- 个股与板块映射。
- 个股和板块资金原始事实。
- 交易日历。
- 可复用基础特征。
- 数据来源、freshness、quality 和修复记录。

### 公共市场库禁止保存

- V3 score、ACTIONABLE、daily_top。
- V4 Setup、Pattern、MATURE、Knowledge。
- 任一应用的推荐、状态或人工决策。

### 应用证据快照

应用不复制完整市场历史，但必须保存当时实际使用的：

```text
as_of
source_ref
source_version
evidence_values
evidence_fingerprint
quality_status
rule_version
```

市场数据后续修复不能覆盖应用当时的证据和判断历史。

---

## 5. 允许与禁止的依赖

### 允许

```text
V4 → platform.market_data_reader
V4 → platform.trading_calendar
V4 → platform.data_quality
V4 → platform.artifact_protocol
V4 → platform.notification_transport
V4 → platform.outcome_calculator
```

### 禁止

```text
V4 → v3.strategies
V4 → v3.lifecycle
V4 → v3.decision
V4 → V3 application.db
V4 → V3 Evidence Matrix 作为自身样本

V3 → v4.*
platform → v3.*
platform → v4.*
```

V3 历史输出若用于迁移或评估，只能通过独立只读 Adapter，且该 Adapter 不得进入 V4 在线决策主链。

---

## 6. 公共契约

V4 开工所需最小公共契约：

| Contract | 责任 | 禁止承担 |
|:--|:--|:--|
| `MarketDataReader` | 按 `as_of` 读取市场事实 | 选股、状态、推荐 |
| `TradingCalendar` | 交易日与 T+N 定位 | 应用生命周期 |
| `DataQualitySnapshot` | freshness、coverage、source、degrade | 自动补业务判断 |
| `EligibilityPrimitive` | 市场范围、ST/退市、基础流动性事实 | 是否建立 Setup |
| `ArtifactEnvelope` | producer、schema、as_of、source refs | 应用 payload 语义 |
| `NotificationTransport` | 发送、重试、transport status | V3/V4 卡片渲染 |
| `OutcomeCalculator` | 同口径 T+N、MFE、MAE | V3/V4 样本选择 |
| `ReadinessMarker` | 公共数据某交易日可消费 | 调用具体应用 |

所有公共契约必须版本化，并定义缺失语义、兼容窗口、弃用流程和 contract tests。

---

## 7. 公共资源加固清单

现有代码不能直接整体命名为 Shared Platform。提取前必须：

1. 删除 `v3_` 命名和 V3 默认策略语义。
2. 将数据事实与 action、score、position coefficient 分离。
3. 将 Artifact Envelope 与 V3 文件版本分离。
4. 将 Notification Transport 与 V3 Renderer 分离。
5. 从 Attribution 中只提取 Outcome Calculator 和统计原语。
6. 为数据库和 artifact 增加 schema version、producer version、`as_of` 和 source refs。
7. 建立只读访问接口，禁止应用直接依赖平台内部表结构扩散。
8. 建立 consumer contract tests，平台升级同时验证 V3/V4 兼容。
9. 配置、secret、日志和数据目录不由应用硬编码。
10. 平台任何 breaking change 必须发布主版本并提供迁移说明。

---

## 8. 提取顺序

禁止一次性重写全部基础设施。按 V4 最小消费链提取：

### Batch A：V4 开工前必须完成

- TradingCalendar。
- MarketDataReader。
- DataQualitySnapshot / ReadinessMarker。
- ArtifactEnvelope。
- OutcomeCalculator。

### Batch B：首条 V4 纵向链路需要

- EligibilityPrimitive。
- Board / Capital fact readers。
- NotificationTransport。

### Batch C：运行稳定后迁移

- 数据 providers/update orchestration。
- health runtime、source probe、fault delivery。
- 更广泛的公共特征计算。

每批必须先做行为等价测试，再切换消费者；不得同时重写数据逻辑和 V4 业务逻辑。

---

## 9. 项目管理与升级

### 里程碑

| Milestone | 目标 | 完成定义 |
|:--|:--|:--|
| M0 Repository Ready | 三仓和所有权成立 | platform/V4 仓库、CI、版本、配置、数据目录和依赖扫描可用 |
| M1 Platform Minimum Ready | V4 可读取公共事实 | Batch A 契约发布；当前数据口径通过等价与 consumer tests |
| M2 V4 Gate A Ready | V4 核心对象正确 | 独立 DB、Event/Setup/Snapshot/State/Attribution Capture 可回放 |
| M3 V4 Vertical Slice | V4 进入真实工作流 | 独立 Runner、Daily Review、Outcome Enrichment 可用，不依赖 V3 |
| M4 V3 Retirement Ready | 可停止 V3 Application | 关闭 V3 后 V4 全流程和 Platform health 正常，历史资产已归档 |

### 任务归属

- Platform issue 只能描述公共事实、通用能力、兼容性或运行保障，不能夹带 V3/V4 规则。
- V4 issue 必须引用 110–119 契约和 platform contract version。
- 跨仓变更拆成 platform release 与 application pin update 两个任务，不在一个提交中同时修改多个仓库。
- 每个里程碑必须有 contract tests、回滚方法、数据迁移说明和书面验收结论。
- 不使用“完成百分比”代替验收；未满足完成定义不得进入下一里程碑。

### 平台升级

```text
contract proposal
→ platform tests
→ V3/V4 consumer contract tests
→ version release
→ application pin update
→ deprecation window
```

### 应用升级

应用可以独立发布，不要求其他应用同步升级。V5 可以与 V4 并存，也可以替换 V4，但只能通过平台契约消费公共事实。

### 比较与迁移

Comparison Tool 只读取版本化 Evaluation Output。没有 V3 输出时，V4 使用 Market Universe 和自身状态层作为对照，不能因 V3 停止而无法评估。

---

## 10. V3 退役定义

### Bootstrap 迁移桥的边界

```text
Bootstrap Migration Publisher != 长期生产 Publisher
```

Bootstrap 只允许作为一次性历史事实迁移工具：源数据库路径必须由显式 CLI
参数传入，不得进入 Platform/V4 默认配置、代码默认值或软连接。迁移产物必须是
可脱离 V3 独立读取的实体数据库；V4 只读取 Platform 发布结果。

Bootstrap 的拆除条件：Platform Updater 已能从外部数据源独立更新
`market_data.db`，并连续 5 个交易日不依赖 V3 数据任务。满足后停止并归档
Bootstrap 工具。阶段二开始前另行冻结外部数据源，不在迁移工具中预设。

迁移验收必须包含 V3-off test：临时移走 V3 源数据库后，Platform 已发布数据、
ReadinessMarker 和 V4 Runner 仍正常工作。

### 过渡期 Daily Publisher

Bootstrap 之后、正式 Platform Updater 接管之前，允许使用独立的
`Transitional Daily Publisher` 维持每日运行。它不是 Bootstrap，也不是长期
Platform 数据供应链；只允许通过显式 CLI 参数读取 V3 市场数据库和同日 health
artifact。只有 `status=success + final_gate=ok + 日期/行数一致` 时，才能原子发布
公共市场库并在最后发布 ReadinessMarker。

V4 仍只读取 Platform 数据库和 marker，不读取或感知 V3 路径、health artifact
或 Runner。过渡 Publisher 与 Bootstrap 共用退役条件：正式 Platform Updater
连续 5 个交易日独立成功后停用并归档，不允许与正式 Updater 长期双写。

退役的是 V3 Application，不是 Shared Platform：

```text
停止 V3 推送
→ 停止 V3 Decision Runner
→ 冻结 V3 历史 artifact / attribution
→ 验证 V4 不依赖 V3 Application
→ 删除或归档 V3 业务代码
```

退役验收：关闭全部 V3 Application 进程后，V4 仍能独立完成 Discovery、Setup、Understanding、State、Daily Review 和 Attribution Capture。

---

## 11. 验收标准

1. 三个仓库的依赖方向可由自动化扫描验证。
2. V3/V4 对公共市场库只有只读权限。
3. V3/V4 业务数据库互不可见、互不可写。
4. 平台 Schema 或 API 变更有版本和兼容测试。
5. V4 Runner 不等待 V3 Runner 或 V3 artifact。
6. V3 停止不影响 V4 日流程。
7. 公共层不包含任何版本应用的状态、评分或推荐语义。
8. 未来应用无需复制 V3/V4 代码即可消费公共事实。

最终原则：

```text
Platform is the source of facts.
Applications are the owners of interpretation.
Facts are shared; decisions are isolated.
```
<!-- Repository ownership: yifei-platform -->
