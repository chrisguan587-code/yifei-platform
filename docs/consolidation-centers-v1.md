# 收盘共享中枢事实 V1

所有者：Platform。用途：中立价格结构事实，不选股、不评分、不自动交易。
计算不依赖ShortLine/V4包；ShortLine研究代码调用Platform同一函数。
2026-10-21用户批准ShortLine从10月22日起读取精确昨日快照执行过滤，消费者规则由
ShortLine `docs/CENTER_PRODUCTION_FILTER_V1.md`拥有，不反向放入Platform。

## 固定口径

- 计算日向前3个自然月（包含两端），额外20个交易日仅用于波幅预热。
- 确认转折：极值日前20个实际成交日平均真实波幅×2；相邻主要极值至少隔3个实际成交日。
- 最近四个已确认主要转折组成三段，每段取整段日线高低；下沿取三段低点最大值，上沿取高点最小值，须下沿<上沿。
- 三段须全部位于三个月内。最近一段未确认，就使用此前已完成的三段；无交集继续向前找最近有效区间。
- 保存构成段、极值发生日、实际确认日、区间历史、收盘离开事件、当日收盘相对位置。
- 近距离的候选极值不能阻塞之后符合间隔条件的转折；没有改变此前确认的参数。
- 只使用截止计算日已知数据，不回填历史确认时点。月份窗口滚动可能使旧区间过期，不称永久不变。
- 窗口及预热范围外的旧缺口不影响近期独立计算。
- 2026-11-02修正：仅预热段的价格链断点、无效价格或无效非成交记录，可丢弃断点前计算状态，
  在后续独立价格段重新初始化。价格链断点当日可作为新段首日，其他无效日从次日开始；
  不跨断点拼接价格、不推算复权因子。实际窗口内异常、重复记录及交易日缺失仍报错。
  后续不足20个实际成交日时仍为历史不足；有足够日线也不保证能形成中枢。
  新发布记录`warmup_policy_version=platform-center-warmup.restart-before-window.v1`，
  个股诊断保留重启原因、日期、实际起点及预热天数；旧无此字段仍按旧事实读取。

## 共享存储与读取

每天一个不可变、带校验和的Platform artifact envelope：

`data/shared/consolidation_centers/YYYY-MM-DD.json`

契约：`platform-consolidation-centers.daily.v1`。算法：`platform-major-wave-center.v1`；窗口：`platform-recent-center.three-calendar-months.v1`。
不兼容口径另升版本，旧快照不覆盖。同日重跑只读取已冻结文件；改源数据后也不会偷偷刷新旧结果。

顶层包括：`as_of`、`published_at`、算法/窗口/参数、源版本与就绪标记、输入行校验和、日历校验和、股票池定义、各状态数量、每股事实。
股票池是三个月内共享日线中出现过的全部A股代码，不应用ShortLine的ST、板块或其他策略过滤。近期退市/无当日行情股票仍保留缺失状态，不假装有最新价。

每股包含：代码、名称、`as_of`、`close`、`status`、`center`、`position`、`center_history`、`departures`、数据诊断。

```python
from pathlib import Path
from yifei_platform import read_center_snapshot_v1, read_stock_center_v1

root = Path('/Users/y-plus/projects/yifei/data/shared/consolidation_centers')
fact = read_stock_center_v1(root, as_of='2026-10-21', stock_code='600721')
# 多股消费者先读整个snapshot，复用snapshot['stocks']，不逐股重新计算。
```

调用方必须指定所需日线日期。次日竞价应指定前一交易日，不自行用latest替代。
ShortLine生产从2026-10-22起按前一交易日读取；数据不足如何处理由其消费合同定义，
不改变这里的事实状态或算法。

### 预热修正版：2026-11-02

经用户确认，允许显式`--correct-warmup`发布同日独立文件：
`YYYY-MM-DD.warmup-restart-v1.json`。原`YYYY-MM-DD.json`不变，修正版也不可覆盖。
仅用于本次窗口外预热修复，必须绑定原artifact ID，且源日线输入校验和完全一致；
不是通用数据回填或修改旧日线入口。`correction`使用`platform-center-correction.v1`，
保留原发布时间、真实修正发布时间（顶层published_at）、原因和修正版引用。

`read_center_snapshot_v1(..., known_at=带时区时间)`仅选该时间前已发布的修正版，否则读原版；
未传known_at表示读取精确日期的当前版本。原版亦晚于cutoff时返回None。
校验失败直接报错，不悄悄退回原版。无修正版的日期行为不变，正常新日发布仍用原路径。
ShortLine09:25、14:30读取分别显式传入当日09:25、14:30截止时间并冻结实际文件引用；
不得在历史回放时把今日修正版当作当时已知事实。

## 状态和失败行为

- `ready`：有已确认中枢，`position`为`inside / above / below`。
- `no_confirmed_center`：当前范围未形成已确认的有效交集，不表示价格安全。
- `history_insufficient`：历史不足。
- `source_sessions_missing / as_of_close_missing / price_lineage_invalid / invalid_price / invalid_nontrading_bar / duplicate_daily_rows`：数据不可用，`center=null`、`position=unknown`。
- 指定日期文件不存在：整日读取返回`None`；单股读取返回`snapshot_missing`。文件存在但无该代码：`stock_not_found`。
- 校验和、日期、契约冲突：直接抛异常，不取旧数据替代。
- 个股数据错误不阻塞其他股票，顶层`degraded`并列明数量；没有中枢与源数据错误分开统计。
- 整日行情未就绪、非交易日、数据库与就绪日期不一致、收盘前：不创建文件。

## 触发与运维

复用已安装17:30的`com.yplus.yifei-platform.daily-market`，在`run_daily_market.sh`当日日线成功发布后串行执行，不另加定时器。
中枢失败让任务非零退出，不重新抓行情、不影响已成功发布的日线、不使用昨日中枢顶替。
现有脚本从仓库源码调用，下一次运行自动生效，不需要重装LaunchAgent。

手动发布（仅收盘后、精确当日日线已就绪）：

```sh
PYTHONPATH=src .venv/bin/python -m yifei_platform.center_facts_cli \
  --market-db /Users/y-plus/projects/yifei/data/shared/market_data.db \
  --exchange-calendar /Users/y-plus/projects/yifei/data/shared/exchange_calendar/sse-2026.v1.json \
  --readiness-root /Users/y-plus/projects/yifei/data/shared/platform_state \
  --output-root /Users/y-plus/projects/yifei/data/shared/consolidation_centers \
  --as-of 2026-10-21
```

只读CLI：同一命令只传`--output-root`、`--as-of`和`--stock-code`。
无需新数据库、行情源、大模型或网络调用。

## 人工验收实例

截至2026-10-21：景旺电子85.00—106.01，收盘90.00在区间内；百花医药12.29—14.64，收盘10.67在下方。
原始日线固定于测试fixture，两例回归、边界和发布合同测试不依赖线上数据。
这些例子确认识别口径，不证明全市场识别均正确或具有收益优势。

## 首批发布验收：2026-10-21

已实际冻结5578个近期有日线的代码：

| 状态 | 数量 |
|---|---:|
| 有已确认中枢 | 2817 |
| 尚无已确认中枢 | 945 |
| 历史不足 | 16 |
| 价格链不连续 | 1701 |
| 日线价格不合法 | 68 |
| 非成交日记录异常 | 28 |
| 无当日收盘 | 3 |

整批明确标为degraded，而不是声称全股票已可用。价格链异常没有自动认定为除权、没有反推复权因子；源数据修复不在本轮范围。
从共享文件读回的景旺电子、百花医药与人工验收一致；同日再次执行只读已冻结文件。
本机Platform和ShortLine环境已使用本地Platform 0.12.0；远端依赖固定提交未更新，本轮未提交/推送。

测试：Platform全量244项复跑通过；ShortLine全量295项通过（14项算法回归迁至Platform，ShortLine新增所有权引用测试）。
Platform原换手率测试首轮因未完全隔离的备用行情网络调用出现一次错误；该文件未修改，原HEAD独立核对与当前全量复跑均通过。
中枢22项及既有artifact6项独立测试通过。

## 历史补算边界

`--historical-reconstruction`显式允许共享日线库晚于`as_of`，但查询仍严格截止
`as_of`，并要求该日readiness及该日实际日线存在。默认日常发布仍要求最新日等于
`as_of`。补算保留真实`published_at`，标记`input_semantics=historical_reconstruction`，
不冒充当时已经发布的事实；现有冻结文件不覆盖。
