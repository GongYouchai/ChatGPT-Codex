# BTC 资金流异动雷达

这是一个可直接运行的“情报雷达”脚本：
- 定时监控比特币交易所 `inflow / outflow / netflow`
- 用滚动窗口 + Z-Score 检测异动
- 触发阈值后推送到 Telegram 或任意 Webhook

## 1) 环境

Python 3.10+（无第三方依赖）。

## 2) 配置

```bash
cp config.example.json config.json
```

编辑 `config.json`：
- `glassnode_api_key`：你的 Glassnode API Key（必填）
- 推送至少配置一个：
  - `telegram_bot_token` + `telegram_chat_id`
  - `webhook_url`

## 3) 先跑一次验证

```bash
python3 src/btc_flow_radar.py --config config.json --once
```

如果配置正确，你会看到当前轮询结果（命中异动则发送推送）。

## 4) 长期运行

```bash
python3 src/btc_flow_radar.py --config config.json
```

建议部署在 VPS / NAS，并用 systemd、supervisor 或 docker 方式常驻。

## 异动规则（默认）

当满足以下条件时发送告警：
1. `|zscore(netflow)| >= 2.5`
2. `|netflow| >= 1500 BTC`
3. 与上一周期净流差异比例 `>= 40%`（首轮没有上一周期则跳过该项）
4. 不在 `cooldown_minutes` 冷却期

## 你可以怎么调优

- 如果告警太频繁：提高 `zscore_threshold`、`min_abs_netflow_btc`
- 如果想更灵敏：降低 `zscore_threshold`，缩小 `lookback_points`
- 想减少噪声：把 `interval` 从 `1h` 改成 `24h`

## 注意

- 资金流是“交易所侧链上流动”的代理指标，不等价于直接买卖成交量。
- 建议把该雷达和价格、未平仓合约、稳定币流量一起看，避免单指标误判。
