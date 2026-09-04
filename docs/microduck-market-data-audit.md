# MICRODUCK 行情接口审计

## 价格计算方式

当前页面和策略中的 MICRODUCK 美元价格不是直接使用 DexScreener 或 Ave 的展示价。它按下面方式计算：

`MICRODUCK→ETH 的可成交报价 × ETH→NVDA 的可成交报价 × NVDA→美元报价`

因此它代表的是在当前链上路径、当前报价数量下的估算成交单价；DexScreener/Ave 通常显示池子中间价或其自身聚合价。两者可能不同，但差值必须被测量和限制。

## 当前必需接口

| 接口 | 当前用途 | 读取的数据 | 是否直接影响交易判断 |
| --- | --- | --- | --- |
| `GET /internal/market-data/nvda` | 控制器内部 NVDA 美元报价入口 | bid、ask、源时间、缓存年龄、来源 | 是 |
| Robinhood `GET /rhj/prices/NVDA` | NVDA/USD 主行情源 | NVDA bid、ask、`generatedAt` | 是 |
| Arbitrum RPC `eth_call` 到 Chainlink NVDA/USD feed | Robinhood 不可用时的备用源和定期交叉检查 | decimals、价格、链上更新时间 | 是，但报价过期时禁止交易 |
| `POST /gateway/swap/quote`，`MICRODUCK-ETH` 双方向 | 计算 MICRODUCK 与 ETH 的当前可成交价格 | amountIn、amountOut、minAmountOut、maxAmountIn、价格影响、路由 | 是 |
| `POST /gateway/swap/quote`，`ETH-NVDA` 双方向 | 计算 ETH 的 NVDA 计价，再换算美元 | 同上 | 是 |
| Gateway 余额查询 | 检查钱包余额、外部转出和 Gas | MICRODUCK、ETH 余额 | 否，不用于价格 |
| Gateway 交易状态查询 | 只在已提交交易后确认结果 | 交易哈希状态 | 否，不用于价格 |

DexScreener 与 Ave 不是当前策略请求；本次仅把它们作为对照，不能把网页显示价直接替换成可成交报价。

## 每个请求的来源、地址与字段说明

以下地址是当前本机运行时使用的地址。`127.0.0.1` 是容器所在机器本身，外网无法访问；Gateway 地址可由运行环境的 `GATEWAY_URL` 改写。所有 Gateway 报价使用 `chainNetwork=ethereum-robinhoodchain` 和 `connector=uniswap/router`。

### 1. 控制器读取 NVDA 美元报价

- **请求地址**：`GET http://127.0.0.1:24872/internal/market-data/nvda?max_age_seconds=15`
- **实际来源**：本机 Hummingbot API 的行情服务；它优先请求 Robinhood，Robinhood 失败时才尝试 Chainlink。
- **返回字段**：
  - `bid`：市场上愿意立刻买入 1 股 NVDA 的最高价格；保守计算“卖出资产后能换回多少美元”时使用。
  - `ask`：市场上愿意立刻卖出 1 股 NVDA 的最低价格；保守计算“买入资产要花多少美元”时使用。
  - `generatedAt`：上游报价产生时间，不是本机收到响应的时间。
  - `source`：本次数据来自 `robinhood` 或 `chainlink`。
  - `quote_age_seconds`：报价从产生到当前已过去多少秒；超过交易允许时长时禁止触发交易。
- **策略作用**：把 ETH/NVDA 的链上报价换成美元。买入 MICRODUCK 使用 NVDA `ask`，卖出 MICRODUCK 使用 NVDA `bid`，避免把过于乐观的价格用于交易判断。

### 2. Robinhood NVDA/USD 主行情

- **请求地址**：`GET https://api.robinhood.com/rhj/prices/NVDA`
- **实际来源**：Robinhood 的 NVDA 股票报价服务。
- **读取字段**：首条 `quotes[0]` 中的 `bid`、`ask`、`generatedAt`。
- **策略作用**：当前 NVDA/USD 主源；本机至少间隔 15 秒才向它重新请求，以减少 `429` 限流风险。它不直接给出 MICRODUCK 价格。

### 3. Chainlink NVDA/USD 备用源与核对

- **请求地址**：`POST https://arb1.arbitrum.io/rpc`
- **实际来源**：Arbitrum 上的 Chainlink NVDA/USD feed，合约地址为 `0x4881A4418b5F2460B21d6F08CD5aA0678a7f262F`。
- **请求内容**：同一个 JSON-RPC 批量请求中读取 `decimals()` 与 `latestRoundData()`；后者包含价格和 `updatedAt`。
- **策略作用**：Robinhood 失败时的备用值，以及每 5 分钟一次的交叉检查。`updatedAt` 太旧或与 Robinhood 偏差超过 10% 时，不能把它用于交易。它不提供买卖盘，因此备用时 `bid=ask=feed price`。

### 4. ETH 与 NVDA 的双向可成交报价

- **请求地址**：`GET http://127.0.0.1:15888/trading/swap/quote`
- **买入 ETH 的请求参数**：`baseToken=ETH&quoteToken=NVDA&amount=1&side=BUY`，表示“买入 1 ETH，最多支付多少 NVDA”。
- **卖出 ETH 的请求参数**：`baseToken=ETH&quoteToken=NVDA&amount=1&side=SELL`，表示“卖出 1 ETH，最少得到多少 NVDA”。
- **实际来源**：本机 Gateway，再由 Gateway 向 Robinhood Chain 上的 Uniswap Router 询问路径和模拟成交结果。
- **返回字段**：
  - `amountIn` / `amountOut`：按当前路径估算的投入与产出。
  - `maxAmountIn`：买入时最多可能投入的 NVDA；计算 ETH 美元买入价时使用它，避免低估成本。
  - `minAmountOut`：卖出时最少可得到的 NVDA；计算 ETH 美元卖出价时使用它，避免高估收益。
  - `priceImpact`：该报价数量对池子价格的影响；数值越大，报价越不适合作为小额展示价。
  - `route`：Gateway 选择的兑换路径。
- **策略作用**：计算 ETH 的保守美元价格：`ETH 买入价 = maxAmountIn × NVDA ask`；`ETH 卖出价 = minAmountOut × NVDA bid`。这是同一交易对的两个相反方向，Gateway 的单次报价只会返回其中一个方向，不能由一条请求同时得到两者。

### 5. MICRODUCK 与 ETH 的双向可成交报价

- **请求地址**：`GET http://127.0.0.1:15888/trading/swap/quote`
- **买入 MICRODUCK 的请求参数**：`baseToken=MICRODUCK&quoteToken=ETH&amount={数量}&side=BUY`。
- **卖出 MICRODUCK 的请求参数**：`baseToken=MICRODUCK&quoteToken=ETH&amount={数量}&side=SELL`。
- **实际来源**：本机 Gateway，再由 Gateway 向 Robinhood Chain 上的 Uniswap Router 询问路径和模拟成交结果。
- **返回字段**：与第 4 项相同。买入时用 `maxAmountIn` 计算最多要付多少 ETH；卖出时用 `minAmountOut` 计算最少能收到多少 ETH。
- **策略作用**：
  - 日常判断使用 `amount=1`，得到每 1 MICRODUCK 的买入可成交价和卖出可成交价。
  - 最终交易确认使用实际买入数量或当前持仓数量，防止 1 枚的展示报价与真实交易数量的滑点不同。

### 6. 钱包余额查询

- **请求地址**：`POST http://127.0.0.1:15888/chains/ethereum/balances`
- **请求内容**：`network=robinhoodchain`、交易钱包地址、`tokens=["MICRODUCK", "ETH"]`。
- **实际来源**：本机 Gateway 读取 Robinhood Chain 链上余额。
- **返回字段**：`balances.MICRODUCK` 与 `balances.ETH`。
- **策略作用**：确认可卖的 MICRODUCK、可用于买入和 Gas 的 ETH，并发现外部余额变化。它不参与价格计算，页面按 5 分钟刷新。

### 7. 已提交交易的链上状态查询

- **请求地址**：`POST http://127.0.0.1:15888/chains/ethereum/poll`
- **请求内容**：`network=robinhoodchain` 与已提交交易的 `signature`（交易哈希）。
- **实际来源**：本机 Gateway 查询 Robinhood Chain 上的交易状态。
- **返回字段**：`txStatus`：`1` 已确认、`0` 等待确认、`-1` 失败、`-2` 未找到；还可能包含手续费和失败原因。
- **策略作用**：交易提交后只用它确认结果；在没有交易哈希时不会调用，更不会据此重新提交同一笔交易。

## 买入与卖出的请求顺序

这里要区分两类请求：

- **行情更新**：用于判断是否进入买入、卖出或跟踪状态；不发送交易。
- **交易前确认**：条件已满足后，用实际买卖数量再取一次报价；确认仍可成交后才发送交易。

### 买入

1. 读取 NVDA/USD 的 bid、ask；主源为 Robinhood，必要时读取 Chainlink 备用源并检查时间是否新鲜。
2. 读取 `ETH→NVDA` 的 `side=BUY` 报价，得到 ETH 的美元买入价。
3. 读取 `MICRODUCK→ETH` 的 `side=BUY` 报价，计算“买入 1 MICRODUCK 的可成交美元价格”。这是买入条件与买入跟踪使用的价格。
4. 当该价格进入买入条件，或处于跟踪状态时继续重复步骤 1～3；跟踪阶段以最低价为基准，反弹比例达到规则才进入下一步。
5. 条件最终满足后，按配置的**买入预算**或**买入数量**请求一次实际数量的 `MICRODUCK→ETH`、`side=BUY` 最终报价。报价有效、余额充足且滑点在限制内，才提交买入交易。
6. 已提交后只查询交易状态，不再把同一笔交易重复提交。

### 卖出

1. 读取 NVDA/USD 的 bid、ask；主源为 Robinhood，必要时读取 Chainlink 备用源并检查时间是否新鲜。
2. 读取 `ETH→NVDA` 的 `side=SELL` 报价，得到 ETH 的美元卖出价。
3. 读取 `MICRODUCK→ETH` 的 `side=SELL` 报价，计算“卖出 1 MICRODUCK 的可成交美元价格”。这是卖出条件与卖出跟踪使用的价格。
4. 当该价格达到卖出门槛后进入跟踪；每次以历史最高价计算当前回落比例，超过配置的回落限制才进入下一步。
5. 条件最终满足后，按 Bot 当前持仓数量请求一次实际数量的 `MICRODUCK→ETH`、`side=SELL` 最终报价。报价有效、余额足够且滑点在限制内，才提交卖出交易。
6. 已提交后只查询交易状态，不再把同一笔交易重复提交。

### 并发边界

买入判断只需要三条独立请求，且应同时开始：`NVDA/USD`、`ETH→NVDA side=BUY`、`MICRODUCK→ETH side=BUY`。卖出判断同样只需要三条：`NVDA/USD`、`ETH→NVDA side=SELL`、`MICRODUCK→ETH side=SELL`。收到三条结果后再计算美元价格。

当前实现为了同时保存买入价与卖出价供页面展示，会额外请求相反方向的两条报价，并且仍是串行等待；这是可以移除的性能浪费。页面若需要同时展示双向价，应把额外两条也并发获取，但它们不应阻塞当前策略方向的判断。

第 5 步不能复用前面的行情报价：它必须在触发交易后，使用实际预算或实际数量重新取得一次最终报价。这能避免把几秒前的展示价格当作交易价格。

## 测试约束

测试脚本只调用报价和行情读取接口，不会调用任何执行交易接口。默认每个接口连续三次、间隔五秒；Robinhood 已出现过 `429`，因此不会用高频压测来人为扩大限流问题。

测试结果将在本节追加：时间、响应时间、HTTP 状态、报价来源、源时间和与外部价格的差异。

## 实测结果（2026-09-03 06:04，北京时间）

原始逐次结果保存在 [test-results/microduck-market-data-2026-09-03-v2.json](test-results/microduck-market-data-2026-09-03-v2.json)。测试为三轮，每轮相隔五秒，所有请求均为只读。

| 接口 | 成功情况 | 响应时间中位数 | 数据新鲜度/结论 |
| --- | --- | --- | --- |
| 本地 NVDA 聚合报价 | 3/3 成功 | 13ms | 返回 Robinhood 缓存；源报价年龄为 11.8～14.7 秒 |
| Robinhood NVDA/USD | 本轮 3/3 成功；上一轮 2/3，出现一次 429 | 385ms | 5 秒间隔并非稳定安全频率，不能用作秒级主行情 |
| Chainlink NVDA/USD | 3/3 成功 | 395ms | 价格更新时间为 2026-09-03 03:02（北京时间），测试时约早 3 小时，不可用于交易 |
| Gateway MICRODUCK→ETH 卖出 | 3/3 成功 | 2486ms | 真实可成交路径，单次约 2.5 秒 |
| Gateway ETH→MICRODUCK 买入 | 3/3 成功 | 2242ms | 真实可成交路径，单次约 2.2 秒 |
| Gateway ETH→NVDA 买入 | 3/3 成功 | 2842ms | 真实可成交路径，单次约 2.8 秒 |
| Gateway NVDA→ETH 卖出 | 3/3 成功 | 2844ms | 真实可成交路径，单次约 2.8 秒 |
| Gateway MICRODUCK/ETH 钱包余额 | 3/3 成功 | 2526ms | 只用于余额与外部转出保护，不参与价格计算 |
| Gateway 交易状态 | 本轮未触发 | 不适用 | 仅在已有交易哈希、等待链上确认时调用；当前没有待确认交易，未伪造哈希测试 |

### 同一轮最终价格复算

测试第三轮中，Robinhood 的 NVDA bid/ask 为 `$224.65 / $224.89`。按控制器当前公式复算：

- 买入：`MICRODUCK 买入所需 0.000012434496 ETH × ETH 买入价 $2415.732688 = $0.030038`。
- 卖出：`MICRODUCK 卖出最少得到 0.000011945720 ETH × ETH 卖出价 $2361.975774 = $0.028216`。

这说明 `$0.031357` 不是该时刻的当前可成交买入价，应视为旧报价或上一轮计算结果。买卖价之间约 6.1% 的差距来自两个路由报价的价差、`minAmountOut`/`maxAmountIn` 保护值及 1 MICRODUCK 报价产生的价格影响，不能把它们合并成一个“实时价格”。

### 对 DexScreener 与 Ave 的比较边界

DexScreener 和 Ave 页面价通常是池子中间价或聚合展示价；策略需要的是指定方向、指定数量、带成交保护的可成交价。两者应同时展示并计算差异，但不能以网页展示价直接替代下单前报价。

本机对 DexScreener 公共 API 的只读访问在本轮网络环境中连接失败，因此没有把不同时间的截图价与实测报价当成精确差异。用户截图中的 `$0.03080` 与本轮买入可成交价 `$0.030038` 相差约 2.5%，这是需要持续监控的差异，但尚不能据此断言哪一方错误。

## 当前结论与后续改造

1. 当前链路不具备每秒更新能力：四段 Gateway 报价若串行调用，单轮约 10 秒；即使并行，也受最慢一段约 3 秒限制。
2. Robinhood 的 15 秒缓存和实际 429 表明它只能作为低频美元换算源，不能作为跟踪阶段的秒级触发依据。
3. Chainlink 在股票不更新时会长时间陈旧；它只能作为有明确新鲜时间的备用源，不能填补实时行情空缺。
4. 下一步应把“池子中间价”和“指定数量可成交买入价/卖出价”分开显示；跟踪阶段先读取链上池状态或监听池事件，再在触发点使用 Gateway 最终报价确认。这样既能快，又不会用展示价直接下单。

余额接口的三次耗时为 2.526 秒、2.038 秒、2.837 秒。它属于安全检查而非价格更新，页面每 5 分钟读取一次是合理的；不应加入每秒价格轮询。
