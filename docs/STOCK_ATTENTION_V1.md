# 股票关注度事实采集 v1

当前消费者：ShortLine只读人气标记、股票池准备阶段的人工核对。不参与筛选和排序。
每日北京时间18:40由 `com.yplus.yifei-platform.stock-attention` 触发。
使用共享交易日历判断，节假日跳过；交易日盘前误触发和日历过期直接报错。

运行入口（项目虚拟环境）：

```sh
.venv/bin/python -m yifei_platform.attention_capture --snapshot-root /Users/y-plus/projects/yifei/data/shared/stock_attention --codes 000678 600664 300750
```

每次采集东财前100、zzshare匿名服务提供的同花顺100条，以及显式指定股票的东财当前排名。
不安装 zzshare，不读取账号密钥，不回填历史，不合成跨平台排名。
腾讯继续由原行情链提供价格与成交事实，不当作人气来源；百度、雪球不接入本采集器。

使用现有 ArtifactStoreV1 保存不可变原始响应和标准化行，schema/source_version 为 source-attention.v1。
`as_of` 是采集日期，`captured_at` 是实际抓取时刻；`source_time` 保留提供方更新时间，
东财批量响应没有更新时间时明确为 null，不能用抓取时间替代。历史读取须要求 captured_at 不晚于决策时刻。
ShortLine只读精确前一交易日且09:25前已经生成的快照，09:50沿用早盘冻结上下文。

各来源 status 为 ok / degraded / unavailable。更新时间跨日或位于未来标 degraded；请求、
解析或业务失败标 unavailable 并保留已拿到的原始响应。另一来源仍保存，不用缓存冒充成功。
缺失排名为 unknown，不意味着不热门。`ok` 只表示响应有效，不保证完整榜单或分钟级新鲜度。
不保证100条排名连续、不保证跨平台代码交集，不把两个人气榜并成全市场前200。

股票池总上限200与供应商批量覆盖100条是不同约束。
2026-10-23用户确认8个方向24只代表，清单在`config/stock_representatives.v1.json`。
每日任务将人工表中的方向/子方向/角色/确认时间按原意冻结至`representative_assertions`，
这是v1可选增量字段；没有该字段的历史快照不能事后补成人工已确认。
Platform只保存人工身份断言，不负责生成策略池、综合排名或推荐。
共享日线继续提供成交额、强势次数和涨停次数；本采集器不重复维护这些事实，不生成股票池或推荐。

定时入口：`scripts/run_attention_daily.sh EXCHANGE_CALENDAR SNAPSHOT_ROOT`。
LaunchAgent模板：`ops/launchd/com.yplus.yifei-platform.stock-attention.plist`。
每天抓两个人气榜，并为这24只代表分别更新东财当前排名；不自动查询全市场。
其他指定股票仍可通过手工入口`--codes`查询；`--representatives`显式指定已确认清单。
任务不发通知，日志为 `data/logs/platform_stock_attention.log` 与 `_err.log`。
来源失败保留结构化失败状态，退出码1；不读取昨日缓存、不自动入池。
模板部署到 `~/Library/LaunchAgents/` 后需由 launchctl bootstrap 加载；已在当前Mac安装。
