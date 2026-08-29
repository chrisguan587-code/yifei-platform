# Shared Concept Membership V1

## 定位

Platform 发布“股票属于哪些同花顺概念”的中性共享事实。它不判断题材强弱，
不选股、不评分、不推送。当前消费者是 Shortline 09:20 扫描器。

## 生产合同

- 生产者：`yifei-platform-publish-concepts`。
- 上游：同花顺公开概念页面，分类口径固定为 `ths_concept`。
- 触发：`com.yplus.yifei-platform.concept-membership`，每周五 18:30；
  休市时以最近交易日作为 `trade_date`。
- 输出：
  `data/shared/concepts/{trade_date}/concept_membership_{trade_date}.json`。
- 合同版本：`platform-concept-membership.v1`。

快照保存来源、抓取时间、概念数量、各概念成分、完整概念比例和六位股票代码
解析率。同一日期的正式快照不可覆盖。

首次切换允许从原 Shortline 目录复制一份已通过同样完整性门槛的同日 `bootstrap`
快照。读取器只允许该 bootstrap 使用旧的 `shortline-concept-membership.v1` schema，
且只在标准文件尚不存在时使用它。Platform 标准文件一旦发布即取得优先级；旧
bootstrap 最多按下述 15 个交易日窗口复用，不形成第二生产链。

## 完整性与失败

发布门槛固定为：概念不少于 300、完整概念比例不低于 95%、股票代码解析率
不低于 98%。未达到门槛不发布新快照，也不混合其他概念体系。

消费者可读取最近成功快照：0—5 个交易日为 `normal`，6—15 个交易日为
`degraded`，超过 15 个交易日为不可用。复用是显式的新鲜度降级，不等于当周
概念已经更新。

## 边界

Platform 只负责事实生产、不可变保存和只读解析。Shortline 不得拥有概念采集器
或写入共享概念目录；它只能通过 Platform 的 `resolve_concept_snapshot` 读取。
