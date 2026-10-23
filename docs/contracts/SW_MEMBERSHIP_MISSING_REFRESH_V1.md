# 缺失申万成员增量补采 v1

2026-10-23用户授权补齐申万数据，所属为Platform共享基础事实，不是V4私有数据。
消费者：既有申万行业事实发布 → V4四象限/预热/认知卡；不修改策略或准入。

唯一补采入口（在Platform仓库运行）：

```sh
PYTHONPATH=src .venv/bin/python -m yifei_platform.sw_membership_refresh \
  --market-db /Users/y-plus/projects/yifei/data/shared/market_data.db \
  --target-db /Users/y-plus/projects/yifei/data/shared/supplemental_facts.db \
  --snapshot-root /Users/y-plus/projects/yifei/data/shared/sw_membership_captures \
  --as-of 2026-10-23
```

仅查询指定交易日有成交、且不存在当日有效L2映射的股票；不删除或修改已有历史记录。
请求同一个官方CNInfo p_stock2110接口，分类标准008003，保持现有SW_L2数据字段/契约版本。
使用系统已有OpenSSL执行官方公开AES时间戳算法，不再依赖故障MiniRacer，不新增安装依赖。
原始响应完整保存到不可变ArtifactStore；传输捕获版本cninfo-stock2110-http.v1。
记录真实完成采集时间fetched_at，本地valid_from不早于实际采集日。
官方原历史变更日保留在原始响应中；不把今日补采当作过去现场，不生成历史live样本。
同股多条冲突、截断、代码不符拒绝；源异常明确保留unavailable_codes，不猜测行业。
共享写锁+SQLite事务保证不与其他发布者互相覆盖。重复执行已补股票不再插入。

本轮实跑：缺失381只，补入381只，无剩余源不可用；实际捕获2026-10-23T23:36:56.605695+08:00。
捕获ID：e0f5e272323cc301681d3a082f109b7ea0e91451b2aa69d43ffc0b9132ba5a73。
随后既有publish-sector-market-daily-v2重算10月23日131个行业，成交额覆盖率从0.969573恢复到1.0。
没有修改97%门槛、没有覆盖旧成员历史或旧行业产物，也没有增加定时任务。
应用继续使用其固定Platform读取契约，不导入Platform兄弟仓库源码或本补采模块。
