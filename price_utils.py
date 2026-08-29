"""
price_utils.py

CoinGeckoから現在価格を取得する処理の共通化。

もともと accuracy_tracker / target_tracker / position_monitor / paper_trader が
それぞれ独自にシンボル対応表と価格取得関数を持っていたが、リトライ処理の有無が
スクリプトごとにバラバラで、リトライを実装し忘れていた accuracy_tracker が
CoinGeckoの429(レート制限)で落ちて検証データを取りこぼす事故が起きた。
同じ事故を繰り返さないよう、対応表とリトライ付き取得をここに一本化する。

注意:
- CoinGecko無料APIはレート制限が厳しいため、複数銘柄を連続で取得する場合は
  呼び出し側で間隔を空けるか、fetch_prices()でまとめて1回のリクエストにすること。
"""

import time

import requests

COINGECKO_BASE = "https://api.coingecko.com/api/v3"

# 監視対象シンボル → CoinGecko上のコインID
SYMBOL_TO_COINGECKO_ID = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "SOL": "solana",
    "XRP": "ripple",
    "HYPE": "hyperliquid",
    "DOGE": "dogecoin",
    "EDGE": "edgex",
    "TRIA": "tria",
    "SUI": "sui",
    "AAVE": "aave",
}

MAX_RETRIES = 2
RETRY_WAIT_SECONDS = 30


def is_supported(symbol: str) -> bool:
    return symbol in SYMBOL_TO_COINGECKO_ID


def _get_with_retry(url: str, params: dict) -> requests.Response:
    """429(レート制限)を受けたら待機してリトライする。
    リトライしても解消しない場合のみ例外を送出する。"""
    resp = requests.get(url, params=params, timeout=15)
    for _ in range(MAX_RETRIES):
        if resp.status_code != 429:
            break
        time.sleep(RETRY_WAIT_SECONDS)
        resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    return resp


def fetch_prices(symbols: list[str]) -> dict[str, float]:
    """複数シンボルの現在価格を1回のリクエストでまとめて取得する。
    レート制限を避けるため、複数銘柄が必要な場合はこちらを使うこと。
    対応表に無いシンボルや価格が取れなかったシンボルは戻り値に含まれない。"""
    coin_ids = {SYMBOL_TO_COINGECKO_ID[s]: s for s in symbols if s in SYMBOL_TO_COINGECKO_ID}
    if not coin_ids:
        return {}

    resp = _get_with_retry(
        f"{COINGECKO_BASE}/simple/price",
        {"ids": ",".join(coin_ids.keys()), "vs_currencies": "usd"},
    )
    data = resp.json()

    prices = {}
    for coin_id, symbol in coin_ids.items():
        usd = data.get(coin_id, {}).get("usd")
        if usd is not None:
            prices[symbol] = usd
    return prices


def get_current_price(symbol: str) -> float | None:
    """単一シンボルの現在価格。対応表に無い場合や取得できない場合はNone。"""
    return fetch_prices([symbol]).get(symbol)
