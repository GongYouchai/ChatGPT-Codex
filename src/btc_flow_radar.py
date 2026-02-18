#!/usr/bin/env python3
"""BTC 资金流异动雷达。"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_STATE_PATH = Path("state/btc_flow_radar_state.json")


@dataclass
class RadarConfig:
    glassnode_api_key: str
    asset: str = "BTC"
    interval: str = "1h"
    lookback_points: int = 48
    zscore_threshold: float = 2.5
    min_abs_netflow_btc: float = 1500.0
    min_pct_change: float = 40.0
    check_interval_seconds: int = 300
    cooldown_minutes: int = 45
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    webhook_url: str | None = None


class HttpClient:
    def __init__(self, timeout: int = 15) -> None:
        self.timeout = timeout

    def get_json(self, url: str, params: dict[str, Any]) -> Any:
        q = urllib.parse.urlencode(params)
        req = urllib.request.Request(f"{url}?{q}", method="GET")
        return self._read_json(req)

    def post_json(self, url: str, body: dict[str, Any]) -> Any:
        payload = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        return self._read_json(req)

    def _read_json(self, req: urllib.request.Request) -> Any:
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"HTTP {exc.code}: {body}") from exc


class GlassnodeClient:
    BASE = "https://api.glassnode.com/v1/metrics/transactions"

    def __init__(self, api_key: str, http: HttpClient | None = None) -> None:
        self.api_key = api_key
        self.http = http or HttpClient()

    def _fetch(self, metric: str, asset: str, interval: str) -> list[dict[str, Any]]:
        url = f"{self.BASE}/{metric}"
        params = {"a": asset, "i": interval, "api_key": self.api_key}
        data = self.http.get_json(url, params)
        if not isinstance(data, list):
            raise ValueError(f"Glassnode 返回非列表数据: {data}")
        return data

    def get_latest_flows(self, asset: str, interval: str) -> dict[str, Any]:
        inflow = self._fetch("transfers_volume_exchanges_inflow", asset, interval)
        outflow = self._fetch("transfers_volume_exchanges_outflow", asset, interval)
        netflow = self._fetch("transfers_volume_exchanges_net", asset, interval)

        if not inflow or not outflow or not netflow:
            raise RuntimeError("资金流数据为空")

        return {
            "ts": int(netflow[-1]["t"]),
            "inflow": float(inflow[-1]["v"]),
            "outflow": float(outflow[-1]["v"]),
            "netflow": float(netflow[-1]["v"]),
            "netflow_series": [float(x["v"]) for x in netflow],
        }


class Notifier:
    def __init__(
        self,
        telegram_bot_token: str | None,
        telegram_chat_id: str | None,
        webhook_url: str | None,
        http: HttpClient | None = None,
    ) -> None:
        self.telegram_bot_token = telegram_bot_token
        self.telegram_chat_id = telegram_chat_id
        self.webhook_url = webhook_url
        self.http = http or HttpClient(timeout=10)

    def send(self, title: str, message: str, payload: dict[str, Any]) -> None:
        errors: list[str] = []

        if self.telegram_bot_token and self.telegram_chat_id:
            try:
                self._send_telegram(f"*{title}*\n{message}")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"Telegram 发送失败: {exc}")

        if self.webhook_url:
            try:
                self._send_webhook({"title": title, "message": message, "payload": payload})
            except Exception as exc:  # noqa: BLE001
                errors.append(f"Webhook 发送失败: {exc}")

        if not self.webhook_url and not (self.telegram_bot_token and self.telegram_chat_id):
            errors.append("未配置任何推送通道")

        if errors:
            raise RuntimeError("; ".join(errors))

    def _send_telegram(self, text: str) -> None:
        url = f"https://api.telegram.org/bot{self.telegram_bot_token}/sendMessage"
        self.http.post_json(
            url,
            {
                "chat_id": self.telegram_chat_id,
                "text": text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            },
        )

    def _send_webhook(self, body: dict[str, Any]) -> None:
        self.http.post_json(self.webhook_url, body)


def load_config(path: Path) -> RadarConfig:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("配置文件格式错误，应为 JSON 对象")
    return RadarConfig(**raw)


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def calc_zscore(series: list[float], latest: float, lookback: int) -> float:
    if len(series) < max(5, lookback):
        return 0.0
    window = series[-lookback:]
    mean = statistics.mean(window)
    stdev = statistics.pstdev(window)
    if math.isclose(stdev, 0.0):
        return 0.0
    return (latest - mean) / stdev


def detect_anomaly(
    latest_netflow: float,
    prev_netflow: float | None,
    netflow_series: list[float],
    cfg: RadarConfig,
) -> tuple[bool, dict[str, Any]]:
    z = calc_zscore(netflow_series[:-1], latest_netflow, cfg.lookback_points)
    abs_net = abs(latest_netflow)

    pct_change = None
    if prev_netflow is not None and not math.isclose(prev_netflow, 0.0):
        pct_change = abs((latest_netflow - prev_netflow) / prev_netflow) * 100

    z_hit = abs(z) >= cfg.zscore_threshold
    abs_hit = abs_net >= cfg.min_abs_netflow_btc
    pct_hit = pct_change is None or pct_change >= cfg.min_pct_change

    is_anomaly = z_hit and abs_hit and pct_hit
    details = {
        "zscore": round(z, 4),
        "abs_netflow": round(abs_net, 3),
        "pct_change": round(pct_change, 2) if pct_change is not None else None,
        "z_hit": z_hit,
        "abs_hit": abs_hit,
        "pct_hit": pct_hit,
    }
    return is_anomaly, details


def utc_fmt(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def run_once(cfg: RadarConfig, state_path: Path) -> None:
    client = GlassnodeClient(cfg.glassnode_api_key)
    notifier = Notifier(cfg.telegram_bot_token, cfg.telegram_chat_id, cfg.webhook_url)
    state = load_state(state_path)

    flows = client.get_latest_flows(cfg.asset, cfg.interval)
    latest_ts = int(flows["ts"])
    inflow = float(flows["inflow"])
    outflow = float(flows["outflow"])
    netflow = float(flows["netflow"])
    series = list(flows["netflow_series"])

    previous = state.get("last_netflow")
    is_anomaly, details = detect_anomaly(netflow, previous, series, cfg)

    last_alert_ts = state.get("last_alert_ts")
    in_cooldown = False
    if last_alert_ts:
        in_cooldown = (latest_ts - int(last_alert_ts)) < cfg.cooldown_minutes * 60

    if is_anomaly and not in_cooldown:
        direction = "净流入激增（潜在卖压）" if netflow > 0 else "净流出激增（潜在囤币）"
        title = f"🚨 BTC 资金流异动雷达: {direction}"
        msg = (
            f"时间: {utc_fmt(latest_ts)}\n"
            f"Inflow: {inflow:.2f} BTC\n"
            f"Outflow: {outflow:.2f} BTC\n"
            f"Netflow: {netflow:.2f} BTC\n"
            f"Z-score: {details['zscore']}\n"
            f"变化幅度: {details['pct_change']}%"
        )
        payload = {"ts": latest_ts, "inflow": inflow, "outflow": outflow, "netflow": netflow, **details}
        notifier.send(title, msg, payload)
        state["last_alert_ts"] = latest_ts
        print(f"[ALERT] {title}\n{msg}")
    else:
        print(
            "[INFO] 无告警 | "
            f"ts={utc_fmt(latest_ts)} netflow={netflow:.2f} z={details['zscore']} "
            f"cooldown={in_cooldown}"
        )

    state["last_seen_ts"] = latest_ts
    state["last_netflow"] = netflow
    save_state(state_path, state)


def run_loop(cfg: RadarConfig, state_path: Path) -> None:
    print("[BOOT] BTC 资金流异动雷达启动")
    while True:
        try:
            run_once(cfg, state_path)
        except Exception as exc:  # noqa: BLE001
            print(f"[ERROR] {exc}")
        time.sleep(cfg.check_interval_seconds)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="BTC 资金流异动雷达")
    p.add_argument("--config", type=Path, default=Path("config.example.json"), help="配置文件路径")
    p.add_argument("--state", type=Path, default=DEFAULT_STATE_PATH, help="状态文件路径")
    p.add_argument("--once", action="store_true", help="仅执行一次")
    return p


def main() -> None:
    args = build_parser().parse_args()
    cfg = load_config(args.config)
    if args.once:
        run_once(cfg, args.state)
    else:
        run_loop(cfg, args.state)


if __name__ == "__main__":
    main()
