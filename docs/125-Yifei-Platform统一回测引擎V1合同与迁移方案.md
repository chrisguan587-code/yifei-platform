# Yifei Platform 统一回测引擎 V1 合同与迁移方案

> 状态：V1 已实现（M0—M3）
> 日期：2026-09-07
> 定位：将 V3 已实现的统一回测原型提升为与应用版本无关的 Platform 共享基础设施。

---

## 1. 结论

Yifei 不重新发明回测循环。

V3 的 `trial/backtest_engine/` 已经证明以下结构可以工作：

```text
应用产生信号 → 下一交易日尝试成交 → 逐交易日管理现金与持仓 → 输出交易和净值
```

Platform 继承这套结构，但不直接复制 V3 的缺陷，也不继承 V3 的 Strategy、Candidate、score、生命周期或路径配置。

目标归属：

```text
yifei-platform
├── OutcomeCalculatorV1       事件发生后价格表现，不模拟交易
└── BacktestEngineV1          订单、成交、持仓、费用和组合净值
        ↑
        ├── V4 adapter
        ├── Shortline adapter
        └── future app adapter
```

应用可以升级或退役；Platform 的公开回测合同和历史回测入口保持可用。

---

## 2. 现有原型与迁移判断

V3 原型位置：

- `trial/backtest_engine/runner.py`
- `trial/backtest_engine/config.py`
- `trial/backtest_engine/contracts.py`
- `trial/backtest_engine/data_gateway.py`
- `trial/backtest_engine/market_constraints.py`
- `trial/backtest_engine/portfolio.py`
- `trial/backtest_engine/reporting.py`
- `trial/backtest_engine/strategy_adapter.py`

### 2.1 保留的设计

- 应用信号与成交模拟分离；
- 全交易日推进，无信号日也更新持仓；
- 信号日之后的交易日才允许买入；
- 现金、仓位、持仓数量和待处理订单统一管理；
- 涨跌停、停牌、流动性、滑点和费用由成交层处理；
- 输出订单、成交、交易、每日净值和摘要；
- 应用通过薄适配器接入，不复制回测循环。

### 2.2 必须修正的原型问题

- 只有佣金和滑点，没有印花税、最低佣金和过户费；
- 买入股数没有按 100 股一手取整；
- 只有成交额门槛，没有成交参与上限和部分成交；
- 涨跌停处理没有完整使用历史 ST、上市阶段和制度版本；
- 以当日收盘信息判断后又按同一收盘价成交，存在不可实现成交；
- 回测末日强制卖出可绕过 T+1 和跌停约束；
- 最后一笔平仓费用没有完整反映到最终净值；
- 直接读取持续更新的 V3 runtime DB，结果不能稳定复现；
- manifest 未冻结完整配置、数据版本、代码版本和规则版本；
- 相同 run id 可以覆盖旧产物；
- 策略异常可被吞掉并表现为“零信号”；
- Platform 无法约束适配器只读取当时可知数据；
- 缺少回测引擎专项合同测试和逐笔对账测试。

迁移原则：保留结构，重做公共合同和正确性边界；不把已知错误当作行为等价目标。

---

## 3. Platform 与应用边界

### 3.1 Platform 负责

- 冻结并校验回测输入；
- 提供严格 `as_of` 的交易日历和市场数据视图；
- 接收调用方产生的中性订单意图；
- 按冻结的执行规则模拟订单、成交和部分成交；
- 管理现金、持仓、可卖数量和每日净值；
- 计算费用、滑点、已实现及未实现收益；
- 记录所有未成交和降级原因；
- 输出确定、可审计、不可覆盖的回测产物。

### 3.2 应用负责

- 定义研究问题和交易规则；
- 产生买入或卖出意图；
- 决定同日多个机会的先后顺序；
- 定义止损、止盈、最长持有等应用判断；
- 解释回测结果并决定是否继续研究。

### 3.3 Platform 禁止

- 生成 Strategy、Candidate、Setup、Pattern 或推荐；
- 按 score 选择股票；
- 自动寻找最佳参数；
- 自动修改任何应用规则；
- 写入 V4、Shortline 或未来应用的业务数据库；
- 接入每日生产链或定时自动回测；
- 把回测结果解释为买卖建议。

应用提交同日多个订单时，必须已经给出确定的 `submission_sequence`。Platform 只按这一顺序和可用资金执行，不理解排序理由。

---

## 4. 两类结果不得混淆

### 4.1 OutcomeCalculatorV1

回答：

> 某只股票在某个观察日之后 T+3、T+5、T+8 或其他窗口表现如何？

它不模拟：

- 是否真的可以买到；
- 仓位和现金；
- T+1 卖出约束；
- 交易费用；
- 组合资金曲线。

### 4.2 BacktestEngineV1

回答：

> 调用方在当时产生这些订单意图后，按冻结的 A 股执行规则，实际能成交多少、持仓如何演化、组合结果如何？

一次性形态研究如果只需要未来涨幅、MFE、MAE和对照组，继续使用 `OutcomeCalculatorV1`，不强制经过组合回测引擎。

需要回答实际买卖收益时，必须使用 `BacktestEngineV1`，不得自行计算交易费用和持仓曲线。

---

## 5. V1 公开输入合同

### 5.1 BacktestRunRequestV1

每次运行至少冻结：

- `run_id`：全局唯一，不可覆盖；
- `decision_start_session`、`decision_end_session`：允许应用产生新订单意图的区间；
- `market_data_start_session`、`market_data_end_session`：包含指标预热和受约束清算所需的完整行情区间；
- `initial_cash`；
- `maximum_positions`；
- `execution_policy_version`；
- `fee_schedule_version`；
- `slippage_model_version`；
- `liquidity_model_version`；
- `price_basis_version`；
- `market_data_snapshot_ref` 和内容摘要；
- `calendar_version`；
- `platform_git_sha`；
- 调用方名称、调用方版本和 adapter 版本；
- 运行结束时未平仓仓位的处理方式。

任何影响结果的字段都必须进入 manifest。缺失关键版本时拒绝运行，不能使用隐式默认值冒充可复现结果。

### 5.2 OrderIntentV1

中性订单意图至少包含：

- `intent_id`；
- `instrument`；
- `side`：`buy` 或 `sell`；
- `decided_as_of`：做出决定时已经收盘的精确交易日；
- `earliest_execution_session`；
- `submission_sequence`；
- 数量或资金预算；
- 订单有效期；
- 调用方版本和调用方自己的原因引用。

Platform 不接收 score，不理解应用原因，只保存引用用于审计。

### 5.3 PointInTimeMarketViewV1

应用 adapter 在交易日 T 收盘产生订单意图时，只能通过回测引擎提供的只读视图读取：

```text
session <= T
```

读取 T 之后的数据必须直接报错。官方 adapter 不得自行连接完整行情数据库。应用异常必须使运行失败或明确降级，禁止转成空订单列表。

### 5.4 OrderIntentProviderV1

应用实现、Platform 定义的唯一接入协议：

```text
on_session_close(context) -> tuple[OrderIntentV1, ...]
```

`context` 只包含：

- 当前已经结束的交易日；
- 截止该交易日的 `PointInTimeMarketViewV1`；
- 当前现金、持仓、可卖数量和未完成订单的只读快照；

Platform 调用传入的 provider，但不 import 任何应用包。provider 的输出为空是合法结果；provider 抛出异常、越界读取或返回非法订单时，运行必须失败或明确降级，不能记作正常零订单。

---

## 6. V1 执行顺序

第一版只支持最清楚、最容易审计的日线执行语义：

```text
T日收盘后：应用读取不晚于T日的数据，产生订单意图
T+1开盘：引擎先执行符合条件的卖单，再执行买单
T+1收盘：按收盘价更新持仓和组合净值
T+1收盘后：应用产生下一批订单意图
```

固定规则：

1. T 日收盘产生的订单，最早 T+1 执行；
2. T+1 买入的股票，最早 T+2 才有可卖数量；
3. 卖出回款可在同一交易日后续买单中使用；
4. 买单按 100 股整数手成交；卖出允许处理因公司行为形成的零股；
5. 订单不得以“看见当日最终收盘价后，再按该收盘价成交”；
6. 第一版只执行下一交易日开盘订单，不模拟盘中主观追单；
7. 买单下一开盘未成交后默认失效，不自动延迟追买；
8. 已产生的卖出意图因停牌或跌停无法成交时，后续交易日继续尝试，直至成交、到达明确终止边界或回测结束；
9. 同一股票重复订单、超出现金或超出持仓数量时，必须记录明确原因。

止损、止盈和最长持有等条件由应用在 T 日收盘依据 PIT 视图产生卖出意图；Platform 不内置某一版本的交易风格。

---

## 7. A股成交约束

### 7.1 涨跌停与停牌

- 使用未复权交易价格模拟成交；
- 涨跌停价格必须由带生效日期的市场规则计算；
- 规则至少区分市场板块、历史 ST 状态、上市初期限制和制度变更；
- 开盘锁死涨停的买单不成交；
- 开盘锁死跌停的卖单不成交；
- 停牌或价格字段无效时不成交；
- V1 只模拟开盘订单，因此不使用“后来开板”倒推开盘时可以成交。

### 7.2 流动性与部分成交

日线数据无法知道集合竞价排队位置，也不能精确重建封单成交。V1 必须明确使用保守的 `daily-open-liquidity.v1` 估算，不能宣称是逐笔真实成交。

该模型要求：

- 禁止无限成交；
- 最大可成交数量由订单提交前已经可知的历史成交量窗口和冻结参与率限制；
- 订单超过上限时只允许部分成交；
- 涨跌停锁死时即使历史流动性充足也不成交；
- 所有容量参数和计算结果写入订单审计记录。

若未来获得可靠的集合竞价或逐笔数据，应发布新的流动性模型版本，不能静默改变 V1 结果。

### 7.3 费用

费用表必须按交易日版本化，至少包括：

- 买卖佣金；
- 最低佣金；
- 卖出印花税；
- 过户费；
- 买卖方向不同费率；
- 滑点。

具体费率在 M2 实现前以权威规则核实并冻结，不能继续只用单一 `commission_rate`。

### 7.4 公司行为和价格连续性

- 交易成交使用未复权价格；
- V1 不自行推算送转、拆并股或现金分红；
- 持仓期间若当日 `preclose` 与上一交易日 `close` 出现无法解释的断点，运行直接阻断；
- 未来只有接入版本化公司行为账本后，才能发布支持除权除息持仓调整的新版本；
- 禁止把除权跳空或价格血缘断点当作真实盈亏。

---

## 8. 回测结束边界

默认禁止“最后一天无条件强制平仓”。

V1 固定输出：

- 已实现收益；
- 未实现收益；
- 期末现金；
- 期末持仓市值；
- 期末总净值；
- 尚未成交的退出意图；
- 无法平仓原因。

如果调用方要求全部平仓，必须显式给出清算开始日和允许使用的后续交易日。清算仍受 T+1、停牌、跌停、流动性和费用约束；到达数据末端仍无法卖出的仓位保持未平仓，不制造虚假成交。

累计收益必须从扣除全部已发生成交费用后的最终净值计算，并能与现金、持仓、成交记录逐日对账。

---

## 9. V1 输出合同

每次运行独占一个不可覆盖目录，至少包含：

```text
manifest.json          完整输入、版本、哈希和行为边界
intents.json           调用方产生的全部订单意图
orders.json            引擎接受、拒绝和延期的订单
fills.json             每笔完整或部分成交
trades.json            已结束交易
positions.json         期末持仓与可卖数量
daily_snapshots.json   每日现金、持仓市值和净值
summary.json           组合统计和数据水位
report.md              人工可读结论，不解释策略好坏
```

运行状态只允许：

- `complete`：合同内结果完整；
- `degraded`：结果可用，但存在明确的不完整数据或未平仓事项；
- `blocked`：关键数据、版本或合同不满足，未产生有效结论。

“零订单”“零成交”“策略异常”“数据缺失”必须互相区分。

---

## 10. 确定性与可靠性AC

### AC-1：T+1

- T 日决定的买单不得早于 T+1 成交；
- T+1 买入的数量不得在 T+1 卖出；
- 回测结束清算也不能绕过 T+1。

### AC-2：成交约束

- 开盘涨停买不到、开盘跌停卖不掉；
- 停牌不成交；
- 超出流动性上限只部分成交；
- 不允许无限成交或负现金；
- 买入数量符合 100 股整数手。

### AC-3：费用

- 使用人工手算样例逐项核对佣金、最低佣金、印花税、过户费和滑点；
- 单笔净收益、现金流水和最终净值完全对账。

### AC-4：PIT

- T 日 adapter 读取 T+1 数据必须失败；
- 收盘生成的决定不得按同一收盘价成交；
- 应用异常不得被记录为零订单。

### AC-5：复现

- 同一冻结数据、订单、配置和代码版本得到相同语义结果；
- run id 不可复用，历史产物不可覆盖；
- 修改快照后完整性校验失败；
- manifest 足以说明结果来自什么数据和规则。

### AC-6：组合对账

- 每日满足 `现金 + 持仓市值 = 净值`；
- 订单数量、成交数量、持仓变化和现金变化逐笔一致；
- 最后一日费用、未平仓和延期卖出都进入最终结果。

### AC-7：公共边界

- Platform 不 import V3、V4、Shortline；
- Platform 公共类型不出现 Strategy、Candidate、Setup、score 或推荐语义；
- 应用只通过版本化公开合同接入；
- 停止或删除 V3 后，Platform 回测测试和其他应用 adapter 仍能运行。

---

## 11. 实施阶段

### M0：合同冻结（本文件）

- 明确所有权、上下游和不做事项；
- 明确日线执行顺序、T+1、费用、流动性和期末边界；
- 不移动代码，不切换消费者。

完成后暂停，由用户确认是否进入 M1。

### M1：公共骨架与合同测试

- 在 `yifei-platform` 建立中性 `BacktestEngineV1` 候选实现；
- 先加入最小人工行情 fixture 和合同测试；
- 迁移 V3 的订单、持仓、逐日推进和报告结构；
- 改用 Platform `TradingCalendarV1`、`MarketDataReaderV1` 和显式路径；
- 不接入 V4 或 Shortline；
- M1—M2 不从 Platform 顶层导出，也不宣称为已发布公共能力。

### M2：A股执行正确性

- 实现并验证 T+1 可卖数量；
- 实现带日期版本的涨跌停与费用规则；
- 实现整数手、流动性上限和部分成交；
- 修复期末、停牌、跌停、公司行为和净值对账；
- 完成手算样例测试。

### M3：冻结运行入口

- 提供唯一手动 CLI；
- 创建不可变输入快照和完整 manifest；
- 输出标准产物和明确状态；
- 相同输入完成确定性验证；
- 不建立定时任务；
- 完成 M3 后才发布 `BacktestEngineV1` 公共合同，首个真实消费者是用户手动发起的离线回测。

### M4：应用接入

- 先选择一个 V4 研究规则做 adapter 和新旧结果解释；
- 再选择一个 Shortline 版本做 adapter；
- 应用只负责产生订单意图，Platform 负责成交和组合；
- 通过 consumer contract test 后，禁止新增自制成交循环。

### M5：V3回测代码退役

- 扫描并迁移仍有价值的 V3 回测调用方；
- 保留历史产物和迁移说明；
- 确认没有消费者依赖 V3 `trial/backtest_engine/`；
- 再允许随 V3 本地仓库物理删除。

---

## 12. 明确不做

- 不建设网页回测平台；
- 不自动调参或寻找最优组合；
- 不自动运行回测；
- 不把归因状态机搬进 Platform；
- 不迁移 V3 的策略、评分或候选合同；
- 不一次性重写全部旧研究脚本；
- 不强迫只做未来涨幅研究的脚本使用组合执行引擎；
- 不声称仅凭日线可以还原集合竞价排队和逐笔成交。

---

## 13. 第一批真实消费者

M1—M2 是同一迁移任务内部的候选实现和验收阶段，不把测试本身冒充真实消费者，也不把未接线代码宣称为 Platform 已有能力。

M3 的第一个真实消费者是用户通过唯一 CLI 手动发起的离线回测。

M4 的首批应用消费者：

1. V4：需要回答“按冻结规则执行能否真正获得收益”的研究假设；
2. Shortline：需要回答“卡片内重点票或最终选择票按次日执行后的组合表现”；
3. 未来应用：通过相同公开合同接入，不复制引擎。

`OutcomeCalculatorV1` 继续服务不需要模拟交易的探索归因。两条能力各自有明确消费者，不互相替代。

## 14. 实现交付记录（2026-09-08）

- 代码归属：`src/yifei_platform/backtest.py`；手动入口：`backtest_cli.py`。
- 安装入口：`yifei-platform-backtest --spec ... --market-db ... --output-root ...`。
- 首个消费者：手动 CLI 的冻结订单输入；V4、Shortline 业务适配器尚未迁移。
- OCR：已完成一次 `ocr-p0` 审查，session 为 `2650f6ff-eb3d-44dc-8cc4-6582d364493d`；修复后由 Codex 复核和测试，没有再次调用外部模型。
- 修复：部分卖出只续排剩余请求量；末日结算日期不可用时现金不变；买入未成交余量显式记录；支持北交所43前缀；持仓价格断点阻断；不支持的模型或复权价格拒绝执行。
- 买入部分成交后的余量当日终止；卖出剩余量按订单有效期继续尝试。
- 数据不足和执行延迟输出原因；关键合同错误直接报错，CLI 不保留伪成功结果。
- V1 日线成交容量是历史成交量的保守估算，不能还原集合竞价真实可成交量。
- V1 不自动处理公司行为；无法解释的持仓价格断点阻断。缺少上市日期时，少于六条历史记录保守不成交，不能视为完整的历史 IPO 规则模型。
- 费用由输入中的按日期费用表明确提供；不内置适用于所有历史年份和券商的费率。
- 本次完成 M0—M3。M4 应用适配和 M5 旧代码退役属于后续消费者迁移；本次未删除 V3。

最小输入字段示例（日期须存在于输入行情库的交易日历，费用和参与率须由研究者冻结）：

```json
{
  "run_id": "manual-example-001",
  "decision_start_session": "2026-08-03",
  "decision_end_session": "2026-08-04",
  "market_data_start_session": "2026-07-01",
  "market_data_end_session": "2026-08-07",
  "initial_cash": 100000,
  "maximum_positions": 3,
  "market_data_source_version": "your-frozen-market-version",
  "calendar_version": "your-frozen-calendar-version",
  "price_basis_version": "raw-unadjusted.v1",
  "caller": "manual-research",
  "caller_version": "v1",
  "adapter_version": "frozen-intents.v1",
  "fee_schedule": {
    "version": "research-example.v1",
    "rules": [{
      "effective_from": "2026-01-01",
      "commission_rate": 0.0003,
      "minimum_commission": 5,
      "stamp_duty_sell_rate": 0.0005,
      "transfer_fee_rate": 0.00001
    }]
  },
  "execution_policy": {
    "version": "research-example.v1",
    "slippage_rate": 0.001,
    "maximum_volume_participation": 0.001
  },
  "intents": [{
    "intent_id": "buy-001",
    "instrument": "000001",
    "side": "buy",
    "decided_as_of": "2026-08-03",
    "earliest_execution_session": "2026-08-04",
    "submission_sequence": 1,
    "cash_budget": 10000
  }]
}
```
