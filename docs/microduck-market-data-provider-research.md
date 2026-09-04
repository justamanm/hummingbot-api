# MICRODUCK 行情服务调查与推荐

## 结论概述

当前慢的主要原因不是计算，而是策略每轮通过 Gateway 请求多个完整兑换报价。实测每个报价约需 2.2～2.8 秒，串行执行后无法做到秒级更新。

推荐把“持续观察价格”和“真正下单前确认”拆开：

1. 使用 Alchemy WebSocket 监听 Robinhood Chain 上的 Uniswap 池变化，在本地计算池子价格。
2. 日常跟踪阶段不再反复请求 Gateway。
3. 只有触发买入或卖出时，才用实际交易数量请求一次 Gateway 报价并执行交易。
4. Robinhood 官方 NVDA 接口继续作为低频美元参考；如果确实需要亚秒级股票价格，再评估付费的 Chainlink Data Streams。
5. Alchemy 连接失败时切换到 Validation Cloud；两者都失败时才降级到低频轮询，禁止静默使用过期价格。

这套方案优先解决两个问题：页面价格更新慢，以及跟踪阶段因单个接口失败而停止。它不会取消最终 Gateway 报价，因为池子价格不是最终可成交承诺。

## 三层换算分别怎样优化

当前美元价格由三层换算得到：

`MICRODUCK ↔ ETH × ETH ↔ NVDA × NVDA ↔ USD`

### 第一层：MICRODUCK ↔ ETH

- 当前方式：每个方向分别请求 Gateway，单次实测约 2.2～2.5 秒。
- 优化方式：Alchemy WebSocket 监听该 Uniswap 池的成交事件，并读取池状态，在本地计算买卖方向的参考价格。
- 提升：有链上成交时立即更新，不需要每秒主动请求；断线重连后再读取一次池状态校准。
- 保留限制：实际下单前仍需按真实数量请求 Gateway，以包含滑点、手续费和路由变化。

### 第二层：ETH ↔ NVDA

- 当前方式：每个方向分别请求 Gateway，单次实测约 2.8 秒。
- 优化方式：如果交易路径确实来自固定的 WETH/NVDA Uniswap 池，Alchemy 可以用与第一层相同的 WebSocket 连接监听这个池，并在本地计算。
- 提升：不再为每一轮判断等待第二个 Gateway 报价。
- 前提：必须先确认 Gateway 使用的 PoolManager、Pool ID、手续费档位和 Hook。不能只根据代币地址猜池子。

### 第三层：NVDA ↔ USD

- Alchemy Node API 不能直接把 NVDA 股票换算成美元；它只负责读取 Robinhood Chain。
- Alchemy Prices API 是聚合参考价，免费版每小时 300 次，不适合每秒跟踪，也不能保证与 Robinhood Stock Token 的价格含义完全一致。
- Robinhood 官方 `/rhj/prices/NVDA` 返回原始股票 bid/ask，缓存 15 秒，官方限制为每秒 60 次。因为缓存固定为 15 秒，请求再频繁也不会得到更快的新价格。
- Chainlink Data Streams 提供 NVDA/USD 的亚秒级 REST 和 WebSocket 数据，但 NVDA 产品标为 Premium，公开页面没有低价套餐，必须申请访问并询价。

因此，Alchemy 可以直接优化前两层；第三层需要 Robinhood REST 或 Chainlink Data Streams，不能仅靠 Alchemy 解决。

## 多个 MICRODUCK 池能否减少换算层数

DexScreener 当前能看到 MICRODUCK 分别与 NVDA、USDG、WETH 和 ETH 组成的多个池。它们价格接近，通常有三个原因：

1. 套利者会买入较便宜池中的 MICRODUCK，并在较贵池中卖出，把明显价差拉回。
2. Uniswap 路由会寻找多个池之间更有利的路径。
3. DexScreener 会把不同报价币按自己的美元价格再换算后展示，所以页面上的美元数并不都是池内直接存在的美元价格。

价格接近不表示任意池都同样适合交易。还要比较流动性、真实交易数量的滑点、手续费、Hook、池版本和当前路由。用户截图中各池流动性差异明显：USDG 池最高约 66.7 万美元，NVDA 池约 47 万美元，WETH 池约 17.7 万美元，部分 ETH 池只有约 3.2 万美元或更低。交易数量较大时，较浅的池可能产生明显滑点。

可以减少换算层数，但要分别考虑“计算参考价”和“钱包真实交易”：

| 选择的池 | 参考美元价计算 | 层数 | 说明 |
| --- | --- | --- | --- |
| MICRODUCK/NVDA | `MICRODUCK/NVDA × NVDA/USD` | 2 | 能去掉 ETH/NVDA；适合现有 NVDA 美元行情体系 |
| MICRODUCK/WETH | `MICRODUCK/WETH × ETH/USD` | 2 | WETH 与 ETH 按 1:1 处理；需要可靠的 ETH/USD 行情 |
| MICRODUCK/ETH | `MICRODUCK/ETH × ETH/USD` | 2 | 与 WETH 方案相近，但要确认池和 Gateway 对原生 ETH 的处理 |
| MICRODUCK/USDG | `MICRODUCK/USDG × USDG/USD` | 2 | USDG 目标锚定 1 美元；若只用于快速观察可近似把第二层当作 1，但交易保护仍应检查脱锚 |

其中最直接的是 MICRODUCK/USDG。USDG 是 Robinhood Chain 官方列出的代币，由 Paxos 提供稳定币基础设施，目标是 1 USDG 兑 1 美元。但稳定币仍可能短暂偏离 1 美元，因此不能永远写死为 1；至少要设置偏离上限和备用 USDG/USD 数据。

2026-09-03 的本机只读验证表明，这个方向目前只能用于行情设计，不能直接用于交易。当前 Gateway 的 Universal Router 日志显示只检查 V2、V3，没有检查流动性最大的 MICRODUCK/USDG v4 池。它选中的小型 v3 池报价异常：买 1 枚约需 0.111704 USDG，卖 1 枚约得 0.053480 USDG，价格影响分别达到 35.13% 和 26.19%；买入 10 枚以上直接找不到路由。详细记录见 [MICRODUCK/USDG 路由只读验证](test-results/microduck-usdg-route-2026-09-03.md)。

如果交易钱包里只有 ETH，选择 MICRODUCK/NVDA 或 MICRODUCK/USDG 并不会凭空省掉真实兑换步骤。Router 仍可能执行 `ETH→NVDA→MICRODUCK` 或 `ETH→USDG→MICRODUCK`，只是对调用方表现为一次报价和一次交易。只有钱包本身持有 NVDA 或 USDG，并直接使用对应池，实际路径才可能只经过一个池。

因此当前建议是：

- 行情观察优先使用流动性充足的 `MICRODUCK/NVDA` 或 `MICRODUCK/USDG`，把三层计算缩短为两层。
- 当前不能把 ETH 转成 USDG 后交给 Gateway 交易；必须先补充 Uniswap v4 报价和执行能力。
- 在 v4 支持完成后，实际交易仍应比较可用路径，并以实际数量选出净到账最多或净支出最少的路线。
- 下单前比较直接池和多跳路径；页面美元价格接近时，真正决定路线的是交易数量、费用和滑点，而不是池名。

## 为什么不能直接使用 `getReserves()` 和 `Sync`

用户提供的 DexScreener 地址是 32 字节的 Pool ID，不是普通 20 字节合约地址，形态符合 Uniswap v4 池。

Uniswap v4 的池集中在 PoolManager 中，不是每个池一个 Pair 合约。因此正确方式是：

- 监听 PoolManager 的 `Swap` 等事件，并按 Pool ID 过滤；
- 使用对应部署的 StateView 读取当前池状态；
- 定期读取状态进行校准；
- 不能套用 Uniswap v2 的 `getReserves()` 和 `Sync` 事件。

Robinhood Chain 上可能存在不止一套 Uniswap v4 部署。正式接入前必须从当前 Gateway 的实际路由确定部署地址，避免监听了错误的 PoolManager。

## 候选服务比较

| 服务 | 适合用途 | 免费额度或低价方案 | 频率 | 评价 |
| --- | --- | --- | --- | --- |
| Alchemy Node API | 两个链上池的 WebSocket 监听、状态读取 | 每月 3000 万 CU；免费档 300 CU/秒 | 适合秒级读取和事件推送 | 首选主节点；Robinhood 官方推荐 |
| Validation Cloud Node API | Alchemy 的独立备用节点 | 每月 5000 万 CU 免费；之后约每百万 CU 0.50 美元起 | 官方页面未给出简单 RPS 数字 | 最适合低成本备用 |
| QuickNode | 备用 RPC 与 WebSocket | 仅首月免费，1000 万 credits、15 请求/秒；之后 49 美元/月起 | 免费试用 15 请求/秒 | 服务完整，但长期成本高于前两项 |
| Robinhood 公共 RPC | 开发和紧急排查 | 免费 | 官方明确限流 | 不用于生产主链路 |
| Robinhood Stock Token REST | NVDA 原始股票 bid/ask | 免费 | 官方 60 请求/秒，但数据缓存 15 秒 | 低成本第三层主源；共享缓存后每 15 秒请求一次即可 |
| Chainlink Data Streams | 亚秒级 NVDA/USD | NVDA 属于 Premium，需申请与询价 | 亚秒级 REST/WebSocket | 性能最好，但成本尚不透明 |
| Alchemy Prices API | 通用代币美元参考价 | 免费每小时 300 次；付费每小时 1 万次 | 不适合每秒策略 | 只能做页面参考或交叉检查 |
| DexScreener API | 页面对照、发现池子 | 免费接口每分钟 300 次 | 最多约 5 次/秒 | 聚合展示价，不作为下单触发主源 |

### 成本粗算

Alchemy WebSocket 按返回数据量计算。官方给出的典型事件约 1000 字节、约消耗 40 CU。按每月 3000 万免费 CU 粗算，可容纳约 75 万条典型事件。实际消耗取决于两个池的成交频率和事件大小；必须按 PoolManager 地址和 Pool ID 做窄过滤，不能订阅整条链的所有日志。

这个估算只用于判断免费额度量级，不是账单保证。上线时应设置用量提醒和费用上限。

## 价格含义必须分开

### bid 与 ask

- `bid`：别人愿意立刻买入 NVDA 的最高价。估算卖出 MICRODUCK 最终能得到多少美元时，使用 bid 更保守。
- `ask`：别人愿意立刻卖出 NVDA 的最低价。估算买入 MICRODUCK 要花多少美元时，使用 ask 更保守。

### 池子参考价

根据池状态即时算出的价格，速度快，适合进入条件和跟踪最高价、最低价。它没有包含真实交易数量造成的滑点。

### Gateway 可成交报价

针对指定方向和指定数量计算，包含路由、费用、滑点保护等信息。它速度较慢，但触发交易后必须使用。

页面应同时标明这两类价格，不能都叫“最新价格”。建议名称为“池子参考价”和“预计可成交价”。

## 推荐的买入请求顺序

### 等待与跟踪阶段

1. Alchemy WebSocket 收到 MICRODUCK/ETH 或 ETH/NVDA 池事件。
2. 本地更新两个池的状态和双向参考价。
3. 使用最近一条仍在有效期内的 NVDA/USD bid/ask 换算美元。
4. 判断是否进入买入范围；进入后持续更新最低价和反弹比例。

这一阶段不请求 Gateway。两个池的状态更新彼此独立，不需要串行等待。

### 真正买入前

1. 按配置的预算或数量，请求一次 `MICRODUCK/ETH side=BUY` 的 Gateway 最终报价。
2. 同时检查钱包余额、Gas 和 NVDA/USD 是否过期。
3. 将最终报价与池子参考价比较，差异超过限制时取消本轮。
4. 条件全部通过后提交交易；提交后只轮询同一个交易哈希。

## 推荐的卖出请求顺序

### 等待与跟踪阶段

1. Alchemy WebSocket 收到两个相关池的事件。
2. 本地更新池状态和卖出方向参考价。
3. 使用仍有效的 NVDA/USD bid 换算美元。
4. 判断是否达到卖出门槛；进入跟踪后始终以历史最高价计算回落比例。

### 真正卖出前

1. 按 Bot 当前持仓数量，请求一次 `MICRODUCK/ETH side=SELL` 的 Gateway 最终报价。
2. 同时检查实际 MICRODUCK 余额、Gas 和行情时间。
3. 最终报价偏差过大时取消本轮，不重复提交。
4. 通过后提交交易，并按交易哈希确认结果。

## 并发和缓存设计

- 两个池共用一条 Alchemy WebSocket，每个池独立维护最后更新时间。
- NVDA/USD 使用全局共享缓存，所有 Bot 共用，不允许每个 Bot 单独请求 Robinhood。
- Robinhood 数据在 15 秒缓存期内直接复用；并行 Bot 不增加外部请求数。
- Gateway 最终报价、余额检查和数据新鲜度检查可以同时开始；只有三者都成功才能提交交易。
- WebSocket 断开后立即标记链上参考价为“暂不可用”，使用指数退避重连，并切换备用 RPC。
- 恢复后先读取完整池状态，再恢复触发判断，不能只依赖断线期间遗漏的事件。

## 实测与尚未验证的部分

2026-09-03 的本机只读测试结果：

- 本地 NVDA 缓存读取中位数 13ms；实际 Robinhood 源数据年龄约 12～15 秒。
- Robinhood 直接请求中位数 385ms；历史测试出现过一次 429，虽然官方现在标明每秒 60 次，因此仍需共享缓存和退避处理。
- Gateway 四个方向报价中位数约 2.2～2.8 秒，这是当前更新慢的主要来源。
- Robinhood 公共 RPC 可以访问，但官方明确不建议生产使用。
- 当前项目没有发现已配置的 Alchemy Robinhood Chain HTTP/WSS 地址，因此暂时不能做带密钥的真实延迟、断线恢复和事件吞吐测试。

下列项目必须在改代码前完成：

1. 从 Gateway 实际报价响应或交易记录中确定两个 Pool ID、PoolManager、StateView、手续费档位和 Hook。
2. 创建 Alchemy Robinhood Chain 应用，分别测试 HTTP `eth_call` 和 WebSocket 日志订阅至少 30 分钟。
3. 配置 Validation Cloud 备用端点，验证主节点断开后能否无缝补读状态。
4. 对比本地池子参考价、Gateway 实际数量报价、DexScreener 展示价，记录买卖方向误差。
5. 向 Chainlink 询问 NVDA Data Streams 的价格与试用额度，再决定是否替换 15 秒缓存的 Robinhood REST。

## 最终推荐

### 第一阶段：低成本、优先落地

- 主节点：Alchemy 免费档。
- 备用节点：Validation Cloud 免费档。
- 第一、二层：WebSocket 事件驱动，本地计算池子参考价。
- 第三层：Robinhood 官方 REST，每 15 秒全局更新一次。
- 下单前：仅请求当前方向、实际数量的一次 Gateway 报价。

这会把普通跟踪从“每轮等待多个 2～3 秒请求”改为“池子变化时立即本地计算”。第三层仍最多约 15 秒更新一次，但同一 15 秒内链上池子变化可以立即反映。

### 第二阶段：确实需要亚秒级美元价时

- 保留 Alchemy 和 Validation Cloud 负责链上池。
- 将第三层改为 Chainlink Data Streams NVDA/USD WebSocket。
- Robinhood REST 退为交叉检查和断流备用。

只有当 15 秒股票缓存实际影响买卖结果时，付费 Data Streams 才有明显价值。当前更大的瓶颈是 Gateway 报价链路，应先完成第一阶段。

## 官方资料

- Robinhood Chain 连接与推荐节点：https://docs.robinhood.com/chain/connecting/
- Robinhood Stock Token REST：https://docs.robinhood.com/chain/stock-token-apis/
- Robinhood Chain Data Streams：https://docs.robinhood.com/chain/data-streams/
- Alchemy 价格与免费额度：https://www.alchemy.com/docs/reference/pricing-plans
- Alchemy WebSocket 计费：https://www.alchemy.com/docs/reference/compute-unit-costs
- Alchemy Prices API 限制：https://www.alchemy.com/docs/reference/prices-api-faq
- Validation Cloud Robinhood Chain：https://www.validationcloud.io/robinhood
- QuickNode 价格：https://www.quicknode.com/pricing
- DexScreener API 限制：https://docs.dexscreener.com/api/reference
- Uniswap v4 StateView：https://developers.uniswap.org/docs/protocols/v4/guides/state-view
