"""
Market Pulse data collector
- 서버/DB 없이 data/latest.json 하나만 갱신합니다.
- 기본 데이터 소스는 pykrx입니다. pykrx는 KRX/Naver 데이터를 스크래핑하므로
  데이터 제공처 구조 변경 시 실패할 수 있습니다.
- 실패한 항목은 가짜값을 넣지 않고 status/error에 기록합니다.
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import pandas as pd
from pykrx import stock

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "latest.json"


def trade_date_display(trade_date: str) -> str:
    return f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}"


def already_collected(trade_date: str) -> bool:
    """같은 거래일의 핵심 데이터가 이미 저장돼 있으면 이후 재시도 실행은 건너뜁니다."""
    if not OUT.exists():
        return False
    try:
        old = json.loads(OUT.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return False
    return bool(
        old.get("tradeDate") == trade_date_display(trade_date)
        and old.get("updateReady") is True
        and old.get("market")
        and old.get("weekly")
    )


TICKERS: dict[str, str] = {
    "005930": "삼성전자",
    "000660": "SK하이닉스",
}

WEIGHTS = {
    "foreign_sell_days": 0.35,
    "indiv_net_buy": 0.30,
    "credit_growth": 0.20,
    "ratio_drop": 0.15,
}


def ymd(dt: datetime | pd.Timestamp) -> str:
    return pd.Timestamp(dt).strftime("%Y%m%d")


def iso(dt: datetime | pd.Timestamp) -> str:
    return pd.Timestamp(dt).strftime("%Y-%m-%d")


def display_date(dt: datetime | pd.Timestamp) -> str:
    return pd.Timestamp(dt).strftime("%m-%d")


def clean_number(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if math.isnan(value) if isinstance(value, float) else False:
            return default
        return float(value)
    text = str(value).strip().replace(",", "").replace("%", "")
    if text in {"", "-", "nan", "None"}:
        return default
    try:
        return float(text)
    except ValueError:
        return default


def to_억원(value_원: Any) -> float:
    return round(clean_number(value_원) / 100_000_000, 1)


def safe_df(fn: Callable[[], pd.DataFrame], name: str, errors: list[str]) -> pd.DataFrame:
    try:
        df = fn()
        if df is None:
            errors.append(f"{name}: empty response")
            return pd.DataFrame()
        return df.copy()
    except Exception as e:  # noqa: BLE001
        errors.append(f"{name}: {type(e).__name__}: {e}")
        return pd.DataFrame()


def find_col(df: pd.DataFrame, candidates: list[str]) -> str | None:
    if df is None or df.empty:
        return None
    columns = [str(c) for c in df.columns]
    for cand in candidates:
        for col in columns:
            if col == cand:
                return col
    for cand in candidates:
        for col in columns:
            if cand in col:
                return col
    return None


def last_valid_row(df: pd.DataFrame) -> tuple[pd.Timestamp | None, pd.Series | None]:
    if df is None or df.empty:
        return None, None
    df2 = df.dropna(how="all")
    if df2.empty:
        return None, None
    idx = pd.Timestamp(df2.index[-1])
    return idx, df2.iloc[-1]


def previous_valid_row(df: pd.DataFrame) -> tuple[pd.Timestamp | None, pd.Series | None]:
    if df is None or len(df.dropna(how="all")) < 2:
        return None, None
    df2 = df.dropna(how="all")
    return pd.Timestamp(df2.index[-2]), df2.iloc[-2]


def latest_trading_day(today: datetime, errors: list[str]) -> str:
    """최근 10일 안에서 KOSPI OHLCV가 존재하는 마지막 거래일을 찾습니다."""
    for offset in range(0, 10):
        d = today - timedelta(days=offset)
        ds = ymd(d)
        df = safe_df(lambda ds=ds: stock.get_market_ohlcv_by_ticker(ds, market="KOSPI"), f"ohlcv_by_ticker {ds}", errors)
        if not df.empty:
            return ds
    raise RuntimeError("최근 10일 내 KOSPI 거래일 데이터를 찾지 못했습니다.")


def collect_price_table(trade_date: str, errors: list[str]) -> pd.DataFrame:
    return safe_df(lambda: stock.get_market_ohlcv_by_ticker(trade_date, market="KOSPI"), "market ohlcv by ticker", errors)


def collect_index_ohlcv(trade_date: str, errors: list[str]) -> pd.DataFrame:
    start = ymd(pd.Timestamp(trade_date) - pd.Timedelta(days=20))
    return safe_df(lambda: stock.get_index_ohlcv_by_date(start, trade_date, "1001"), "KOSPI index ohlcv", errors)


def collect_trading_value(target: str, trade_date: str, lookback_days: int, errors: list[str]) -> pd.DataFrame:
    start = ymd(pd.Timestamp(trade_date) - pd.Timedelta(days=lookback_days))
    # detail=True가 일부 환경에서 안 먹을 수 있어 두 번 시도합니다.
    df = safe_df(lambda: stock.get_market_trading_value_by_date(start, trade_date, target, detail=True), f"trading value detail {target}", errors)
    if df.empty:
        df = safe_df(lambda: stock.get_market_trading_value_by_date(start, trade_date, target), f"trading value {target}", errors)
    return df


def normalize_flow(row: pd.Series | None) -> dict[str, float]:
    if row is None:
        return {"foreign": 0.0, "institution": 0.0, "individual": 0.0}
    df = pd.DataFrame([row])
    f_col = find_col(df, ["외국인합계", "외국인", "외국인계"])
    i_col = find_col(df, ["기관합계", "기관", "금융투자", "투신", "연기금"])
    p_col = find_col(df, ["개인"])
    return {
        "foreign": to_억원(row.get(f_col, 0)) if f_col else 0.0,
        "institution": to_억원(row.get(i_col, 0)) if i_col else 0.0,
        "individual": to_억원(row.get(p_col, 0)) if p_col else 0.0,
    }


def price_change_from_df(df: pd.DataFrame, price_col_candidates: list[str]) -> tuple[float, float, float]:
    """return close, diff, diff_pct"""
    if df.empty:
        return 0.0, 0.0, 0.0
    close_col = find_col(df, price_col_candidates)
    chg_col = find_col(df, ["등락률"])
    _, row = last_valid_row(df)
    _, prev = previous_valid_row(df)
    close = clean_number(row.get(close_col, 0)) if row is not None and close_col else 0.0
    if chg_col and row is not None:
        pct = clean_number(row.get(chg_col, 0))
        if prev is not None and close_col:
            prev_close = clean_number(prev.get(close_col, close))
            diff = close - prev_close
        else:
            diff = 0.0
        return close, diff, pct
    if prev is not None and close_col:
        prev_close = clean_number(prev.get(close_col, close))
        diff = close - prev_close
        pct = (diff / prev_close * 100) if prev_close else 0.0
        return close, diff, pct
    return close, 0.0, 0.0


def collect_foreign_ratio(ticker: str, trade_date: str, errors: list[str]) -> dict[str, Any]:
    recent_start = ymd(pd.Timestamp(trade_date) - pd.Timedelta(days=40))
    long_start = ymd(pd.Timestamp(trade_date) - pd.Timedelta(days=3650))

    recent = safe_df(
        lambda: stock.get_exhaustion_rates_of_foreign_investment(recent_start, trade_date, ticker),
        f"foreign ratio recent {ticker}",
        errors,
    )
    long = safe_df(
        lambda: stock.get_exhaustion_rates_of_foreign_investment(long_start, trade_date, ticker),
        f"foreign ratio long {ticker}",
        errors,
    )

    ratio_col = find_col(recent, ["지분율", "보유비중", "외국인비율", "한도소진률"])
    date_idx, row = last_valid_row(recent)
    ratio = clean_number(row.get(ratio_col, 0)) if row is not None and ratio_col else None

    history: list[dict[str, Any]] = []
    labels: list[str] = []
    confirmed: list[float | None] = []
    if not recent.empty and ratio_col:
        tail = recent.dropna(subset=[ratio_col]).tail(12)
        for idx, r in tail.iterrows():
            labels.append(display_date(idx))
            confirmed.append(round(clean_number(r.get(ratio_col)), 2))

    high = None
    high_date = None
    pct_of_high = None
    if not long.empty:
        long_col = find_col(long, ["지분율", "보유비중", "외국인비율", "한도소진률"])
        if long_col:
            s = pd.to_numeric(long[long_col], errors="coerce").dropna()
            if not s.empty:
                high = round(float(s.max()), 2)
                high_date = iso(s.idxmax())
                if ratio is not None and high:
                    pct_of_high = round(ratio / high * 100, 1)
                history = [
                    {"period": high_date[:7], "ratio": high, "note": "조회기간 고점"},
                ]

    if ratio is not None:
        history.append({"period": iso(date_idx) if date_idx is not None else trade_date, "ratio": round(ratio, 2), "note": "최근 확정"})

    return {
        "available": ratio is not None,
        "ratio": round(ratio, 2) if ratio is not None else None,
        "high": high,
        "highDate": high_date,
        "pctOfHigh": pct_of_high,
        "labels": labels,
        "confirmed": confirmed,
        "history": history,
    }


def compute_ratio_drop(foreign_info: dict[str, Any]) -> float | None:
    vals = [v for v in foreign_info.get("confirmed", []) if isinstance(v, (int, float))]
    if len(vals) < 2:
        return None
    return round(vals[-2] - vals[-1], 2)  # 전일 대비 하락폭. 하락이면 양수


def collect_credit(trade_date: str, errors: list[str]) -> dict[str, Any]:
    """신용잔고 수집. 지원 함수가 없거나 구조가 다르면 가짜값 없이 unavailable 처리."""
    start = ymd(pd.Timestamp(trade_date) - pd.Timedelta(days=80))
    candidates = [
        "get_market_credit_by_date",
        "get_credit_trading_value_by_date",
        "get_credit_trading_volume_by_date",
        "get_margin_trading_balance_by_date",
        "get_margin_trading_volume_by_date",
    ]
    tried: list[str] = []
    for fname in candidates:
        fn = getattr(stock, fname, None)
        if fn is None:
            continue
        tried.append(fname)
        call_variants = [
            lambda fn=fn: fn(start, trade_date, "KOSPI"),
            lambda fn=fn: fn(start, trade_date),
        ]
        for call in call_variants:
            df = safe_df(call, f"credit {fname}", errors)
            if df.empty:
                continue
            bal_col = find_col(df, ["신용융자", "융자", "잔고", "합계", "전체"])
            if not bal_col:
                continue
            series = pd.to_numeric(df[bal_col], errors="coerce").dropna()
            if series.empty:
                continue
            labels = [display_date(i) for i in series.tail(8).index]
            # 값 단위가 원/주/백만원 등일 수 있어 너무 큰 값은 억원화, 이미 작은 값이면 그대로 둡니다.
            raw_values = [float(v) for v in series.tail(8).values]
            if max(raw_values) > 1_000_000_000:
                values = [round(v / 100_000_000, 1) for v in raw_values]
                unit = "억원"
            else:
                values = [round(v, 1) for v in raw_values]
                unit = "원자료"
            current = values[-1]
            prev_30 = values[0] if values else None
            growth = round((current - prev_30) / prev_30 * 100, 1) if prev_30 else None
            high = max(values) if values else None
            return {
                "available": True,
                "source": fname,
                "unit": unit,
                "current": current,
                "growth30d": growth,
                "allTimeHigh": high,
                "pctOfHigh": round(current / high * 100, 1) if high else None,
                "history": [{"date": d, "balance": v} for d, v in zip(labels, values)],
            }
    return {
        "available": False,
        "source": None,
        "message": "pykrx에서 신용잔고 수집 함수를 찾지 못했거나 컬럼 구조가 맞지 않습니다. 가짜값은 넣지 않았습니다.",
        "tried": tried,
        "history": [],
    }


def calc_divergence_from_rows(flow_rows: list[dict[str, Any]], credit: dict[str, Any] | None, ratio_drops: list[float | None]) -> dict[str, Any]:
    """최근 flow_rows 기준 점수 계산. 사용 가능한 지표만 가중치 재배분."""
    if not flow_rows:
        return {"score": 0, "level": 1, "details": {}, "availableWeights": {}}

    # 최신순으로 정렬
    rows_desc = list(reversed(flow_rows))
    sell_days = 0
    for r in rows_desc:
        if clean_number(r.get("foreign", 0)) < 0:
            sell_days += 1
        else:
            break

    indiv_total = sum(max(clean_number(r.get("individual", 0)), 0) for r in flow_rows[-30:])
    components: dict[str, float | None] = {
        "foreign_sell_days": min(sell_days / 10, 1.0),
        "indiv_net_buy": min(indiv_total / 30000, 1.0),
        "credit_growth": None,
        "ratio_drop": None,
    }

    if credit and credit.get("available") and credit.get("growth30d") is not None:
        components["credit_growth"] = min(max(clean_number(credit.get("growth30d")) / 30, 0), 1.0)

    valid_ratio_drops = [x for x in ratio_drops if isinstance(x, (int, float))]
    if valid_ratio_drops:
        avg_drop = sum(max(x, 0) for x in valid_ratio_drops) / len(valid_ratio_drops)
        components["ratio_drop"] = min(avg_drop / 1.0, 1.0)

    used = {k: WEIGHTS[k] for k, v in components.items() if v is not None}
    total_weight = sum(used.values()) or 1.0
    score = 0.0
    for k, v in components.items():
        if v is not None:
            score += (WEIGHTS[k] / total_weight) * v * 100

    score = round(min(score, 100), 1)
    level = 1
    if score >= 85:
        level = 4
    elif score >= 70:
        level = 3
    elif score >= 50:
        level = 2

    return {
        "score": score,
        "level": level,
        "details": {
            "sellDays": sell_days,
            "individualTotalEok": round(indiv_total, 1),
            "creditGrowthPct": credit.get("growth30d") if credit else None,
            "avgRatioDropPctp": round(sum(valid_ratio_drops) / len(valid_ratio_drops), 2) if valid_ratio_drops else None,
        },
        "availableWeights": {k: round(v / total_weight, 3) for k, v in used.items()},
    }


def level_label(level: int) -> str:
    return {4: "위험", 3: "경계", 2: "주의", 1: "보통"}.get(level, "보통")


def build_market_cards(trade_date: str, price_table: pd.DataFrame, index_df: pd.DataFrame, flows: dict[str, pd.DataFrame], foreign_infos: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []

    idx_close, idx_diff, idx_pct = price_change_from_df(index_df, ["종가"])
    _, kospi_flow_row = last_valid_row(flows.get("KOSPI", pd.DataFrame()))
    kospi_flow = normalize_flow(kospi_flow_row)
    max_abs = max(abs(v) for v in kospi_flow.values()) or 1
    cards.append({
        "name": "KOSPI",
        "code": "코스피 전체",
        "price": f"{idx_close:,.2f}",
        "change": f"{'▲' if idx_diff >= 0 else '▼'} {abs(idx_diff):,.2f} ({idx_pct:+.2f}%)",
        "flow": kospi_flow,
        "maxAbs": max_abs,
    })

    close_col = find_col(price_table, ["종가"])
    pct_col = find_col(price_table, ["등락률"])
    for ticker, name in TICKERS.items():
        row = price_table.loc[ticker] if ticker in price_table.index else None
        close = clean_number(row.get(close_col, 0)) if row is not None and close_col else 0
        pct = clean_number(row.get(pct_col, 0)) if row is not None and pct_col else 0
        # 등락금액은 pykrx by_ticker에 없는 경우가 많아 퍼센트 중심 표기
        _, flow_row = last_valid_row(flows.get(ticker, pd.DataFrame()))
        flow = normalize_flow(flow_row)
        max_abs = max(abs(v) for v in flow.values()) or 1
        finfo = foreign_infos.get(ticker, {})
        cards.append({
            "name": name,
            "code": ticker,
            "price": f"{close:,.0f}원" if close else "N/A",
            "change": f"{pct:+.2f}%",
            "flow": flow,
            "maxAbs": max_abs,
            "foreignRatio": finfo.get("ratio"),
            "foreignPctOfHigh": finfo.get("pctOfHigh"),
        })
    return cards


def build_weekly(flows: pd.DataFrame, credit: dict[str, Any], ratio_drops: list[float | None]) -> list[dict[str, Any]]:
    if flows.empty:
        return []
    items: list[dict[str, Any]] = []
    tail = flows.tail(7)
    temp_rows: list[dict[str, Any]] = []
    for idx, row in tail.iterrows():
        flow = normalize_flow(row)
        temp_rows.append(flow)
        score = calc_divergence_from_rows(temp_rows, credit, ratio_drops)
        items.append({
            "date": display_date(idx),
            "foreign": flow["foreign"],
            "individual": flow["individual"],
            "score": score["score"],
            "danger": score["score"] >= 70,
        })
    if items:
        items[-1]["today"] = True
    return items


def build_backtest(index_df: pd.DataFrame, flow_df: pd.DataFrame) -> dict[str, Any]:
    """간단 백테스트. 신용/지분율 없이 수급만으로 계산하므로 참고용."""
    if index_df.empty or flow_df.empty:
        return {"available": False, "thresholds": [], "cases": [], "note": "백테스트 데이터 부족"}
    close_col = find_col(index_df, ["종가"])
    if close_col is None:
        return {"available": False, "thresholds": [], "cases": [], "note": "KOSPI 종가 컬럼 없음"}

    df = flow_df.copy()
    rows = []
    for i, (idx, row) in enumerate(df.iterrows()):
        hist = []
        for _, rr in df.iloc[max(0, i - 29): i + 1].iterrows():
            hist.append(normalize_flow(rr))
        score = calc_divergence_from_rows(hist, None, [None])["score"]
        rows.append({"date": pd.Timestamp(idx), "score": score})

    close = pd.to_numeric(index_df[close_col], errors="coerce").dropna()
    thresholds = []
    for t in [50, 60, 70, 75, 80, 85, 90]:
        returns = []
        for item in rows:
            d = item["date"]
            if item["score"] < t or d not in close.index:
                continue
            loc = close.index.get_loc(d)
            if isinstance(loc, slice):
                continue
            next_loc = loc + 10
            if next_loc >= len(close):
                continue
            r10 = (close.iloc[next_loc] / close.iloc[loc] - 1) * 100
            returns.append(r10)
        if returns:
            hit = sum(1 for r in returns if r < 0) / len(returns) * 100
            avg = sum(returns) / len(returns)
            thresholds.append({"t": t, "n": len(returns), "hit": round(hit), "avg": round(avg, 1)})
        else:
            thresholds.append({"t": t, "n": 0, "hit": 0, "avg": 0})

    cases = []
    for item in sorted(rows, key=lambda x: x["score"], reverse=True)[:5]:
        d = item["date"]
        if d not in close.index:
            continue
        loc = close.index.get_loc(d)
        if isinstance(loc, slice):
            continue
        r5 = None
        r10 = None
        if loc + 5 < len(close):
            r5 = round((close.iloc[loc + 5] / close.iloc[loc] - 1) * 100, 1)
        if loc + 10 < len(close):
            r10 = round((close.iloc[loc + 10] / close.iloc[loc] - 1) * 100, 1)
        cases.append({"date": iso(d), "score": item["score"], "r5": r5, "r10": r10, "note": "수급 과열 신호"})

    return {
        "available": True,
        "thresholds": thresholds,
        "cases": cases,
        "note": "신용잔고/외인지분율 제외, KOSPI 수급만 사용한 간이 백테스트입니다.",
    }


def main() -> None:
    errors: list[str] = []
    now = datetime.now(KST)
    trade_date = latest_trading_day(now, errors)

    if already_collected(trade_date):
        print(f"Already collected fresh data for {trade_date_display(trade_date)}. Skip retry run.")
        return

    price_table = collect_price_table(trade_date, errors)
    index_df = collect_index_ohlcv(trade_date, errors)

    flows: dict[str, pd.DataFrame] = {"KOSPI": collect_trading_value("KOSPI", trade_date, 420, errors)}
    for ticker in TICKERS:
        flows[ticker] = collect_trading_value(ticker, trade_date, 60, errors)

    foreign_infos = {ticker: collect_foreign_ratio(ticker, trade_date, errors) for ticker in TICKERS}
    ratio_drops = [compute_ratio_drop(v) for v in foreign_infos.values()]
    credit = collect_credit(trade_date, errors)

    kospi_flow_rows = [normalize_flow(r) for _, r in flows["KOSPI"].tail(30).iterrows()] if not flows["KOSPI"].empty else []
    div = calc_divergence_from_rows(kospi_flow_rows, credit, ratio_drops)

    market_cards = build_market_cards(trade_date, price_table, index_df, flows, foreign_infos)
    weekly = build_weekly(flows["KOSPI"], credit, ratio_drops)

    # 백테스트용 인덱스는 420일치 필요하므로 다시 길게 조회합니다.
    long_start = ymd(pd.Timestamp(trade_date) - pd.Timedelta(days=600))
    index_long = safe_df(lambda: stock.get_index_ohlcv_by_date(long_start, trade_date, "1001"), "KOSPI index long", errors)
    backtest = build_backtest(index_long, flows["KOSPI"])

    update_ready = (
        not price_table.empty
        and not index_df.empty
        and not flows.get("KOSPI", pd.DataFrame()).empty
        and all(not flows.get(ticker, pd.DataFrame()).empty for ticker in TICKERS)
    )

    level = int(div["level"])
    payload = {
        "schemaVersion": 2,
        "status": "ok" if update_ready and not errors else "partial" if update_ready else "not_ready",
        "updateReady": update_ready,
        "generatedAt": now.strftime("%Y-%m-%d %H:%M:%S KST"),
        "tradeDate": trade_date_display(trade_date),
        "source": "pykrx/KRX public data",
        "notice": "수집 실패 항목은 가짜값 없이 null 또는 수집 불가로 표시합니다. 투자 권유가 아닙니다.",
        "alert": {
            "level": level,
            "label": level_label(level),
            "message": f"수급 다이버전스 점수 {div['score']}점 · {level_label(level)} 구간",
        },
        "market": market_cards,
        "divergence": div,
        "weekly": weekly,
        "foreign": {
            ticker: {"name": TICKERS[ticker], "code": ticker, **info}
            for ticker, info in foreign_infos.items()
        },
        "credit": credit,
        "backtest": backtest,
        "errors": errors[-20:],
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {OUT.relative_to(ROOT)}")
    print(f"status={payload['status']}, updateReady={payload['updateReady']}, tradeDate={payload['tradeDate']}, errors={len(errors)}")


if __name__ == "__main__":
    main()
