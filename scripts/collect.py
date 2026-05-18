
"""
Market Pulse data collector - KRX direct version

- pykrx를 쓰지 않고 KRX Data Marketplace JSON endpoint를 직접 호출합니다.
- 서버/DB 없이 data/latest.json 하나만 갱신합니다.
- 실패한 항목은 가짜값을 넣지 않고 errors에 기록합니다.
- --check-fresh 모드에서는 KRX 조회를 하지 않습니다.
"""

from __future__ import annotations

import json
import math
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import requests

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "latest.json"

KRX_URL = "https://data.krx.co.kr/comm/bldAttendant/getJsonData.cmd"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://data.krx.co.kr/contents/MDC/MDI/mdiLoader/index.cmd",
    "Origin": "https://data.krx.co.kr",
    "Accept": "application/json, text/javascript, */*; q=0.01",
}

TICKERS: dict[str, str] = {
    "005930": "삼성전자",
    "000660": "SK하이닉스",
}

# KRX가 개별종목 조회에서 ISIN/표준코드를 요구하는 경우가 있어서 같이 시도합니다.
ISIN: dict[str, str] = {
    "005930": "KR7005930003",
    "000660": "KR7000660001",
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


def display_date(dt: datetime | pd.Timestamp | str) -> str:
    try:
        return pd.Timestamp(dt).strftime("%m-%d")
    except Exception:
        s = str(dt)
        if len(s) == 8 and s.isdigit():
            return f"{s[4:6]}-{s[6:8]}"
        return s


def trade_date_display(trade_date: str | None) -> str | None:
    if not trade_date or len(trade_date) != 8:
        return None
    return f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}"


def clean_number(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        try:
            if math.isnan(float(value)):
                return default
        except Exception:
            pass
        return float(value)
    text = (
        str(value)
        .strip()
        .replace(",", "")
        .replace("%", "")
        .replace("−", "-")
        .replace("▲", "")
        .replace("▼", "")
    )
    if text in {"", "-", "nan", "NaN", "None"}:
        return default
    try:
        return float(text)
    except ValueError:
        return default


def amount_to_eok(value: Any) -> float:
    """
    KRX 화면 단위가 endpoint마다 원/백만원/십억원으로 다를 수 있어 자동 추정합니다.
    - 원 단위처럼 매우 큰 값: /1억
    - 백만원 단위처럼 1만 이상인 값: /100
    - 십억원 단위처럼 작은 값: *10
    """
    v = clean_number(value)
    av = abs(v)
    if av >= 100_000_000:
        return round(v / 100_000_000, 1)
    if av >= 10_000:
        return round(v / 100, 1)
    return round(v * 10, 1)


def find_col(row_or_df: Any, candidates: list[str]) -> str | None:
    if isinstance(row_or_df, pd.DataFrame):
        columns = [str(c) for c in row_or_df.columns]
    elif isinstance(row_or_df, pd.Series):
        columns = [str(c) for c in row_or_df.index]
    elif isinstance(row_or_df, dict):
        columns = [str(c) for c in row_or_df.keys()]
    else:
        return None

    for cand in candidates:
        for col in columns:
            if col == cand:
                return col

    for cand in candidates:
        for col in columns:
            if cand in col:
                return col

    return None


def krx_json(bld: str, params: dict[str, Any], errors: list[str], name: str) -> dict[str, Any]:
    payload = {
        "bld": bld,
        "locale": "ko_KR",
        "csvxls_isNo": "false",
        **params,
    }
    try:
        r = requests.post(KRX_URL, data=payload, headers=HEADERS, timeout=20)
        r.raise_for_status()
        try:
            return r.json()
        except Exception:
            errors.append(f"{name}: JSONDecodeError: {r.text[:120]}")
            return {}
    except Exception as e:
        errors.append(f"{name}: {type(e).__name__}: {e}")
        return {}


def extract_rows(data: dict[str, Any]) -> list[dict[str, Any]]:
    if not data:
        return []
    for key in ["output", "output1", "block1", "OutBlock_1"]:
        rows = data.get(key)
        if isinstance(rows, list):
            return [r for r in rows if isinstance(r, dict)]
    # 일부 endpoint는 단일 dict를 돌려줄 수 있음
    for value in data.values():
        if isinstance(value, list) and value and isinstance(value[0], dict):
            return value
    return []


def parse_flow_from_rows(rows: list[dict[str, Any]]) -> tuple[dict[str, float], bool]:
    """
    KRX 투자자별 데이터는 두 형태가 가능:
    1) 행: 투자자구분 / 순매수
    2) 열: 개인 / 기관합계 / 외국인합계
    """
    empty = {"foreign": 0.0, "institution": 0.0, "individual": 0.0}

    if not rows:
        return empty, False

    df = pd.DataFrame(rows)

    # 1) 투자자 유형이 행으로 있는 형태
    type_col = find_col(df, ["INVST_TP_NM", "INVST_TP_CD_NM", "투자자구분", "투자자", "구분"])
    net_col = find_col(
        df,
        [
            "NETBID_TRDVAL",
            "NETBID_VAL",
            "NET_BUY_AMT",
            "NETBID",
            "순매수거래대금",
            "순매수대금",
            "순매수",
            "NETBID_TRDVOL",
        ],
    )

    if type_col and net_col:
        result = empty.copy()
        found = False
        for row in rows:
            name = str(row.get(type_col, ""))
            val = amount_to_eok(row.get(net_col, 0))

            if "개인" in name:
                result["individual"] += val
                found = True
            elif "기관" in name and "기타" not in name:
                # 기관합계가 있으면 그 행을 우선 사용. 없으면 기관 관련 행을 합산.
                if "합계" in name or result["institution"] == 0:
                    result["institution"] += val
                found = True
            elif "외국인" in name and "기타" not in name:
                result["foreign"] += val
                found = True

        return {k: round(v, 1) for k, v in result.items()}, found

    # 2) 투자자가 컬럼으로 있는 형태
    row = rows[-1]

    f_col = find_col(row, ["외국인합계", "외국인", "외국인계", "FRGN_NETBID_TRDVAL", "FRGN_NETBID_VAL"])
    i_col = find_col(row, ["기관합계", "기관", "INST_NETBID_TRDVAL", "INST_NETBID_VAL"])
    p_col = find_col(row, ["개인", "PRSN_NETBID_TRDVAL", "INDV_NETBID_TRDVAL", "개인순매수"])

    if f_col or i_col or p_col:
        return {
            "foreign": amount_to_eok(row.get(f_col, 0)) if f_col else 0.0,
            "institution": amount_to_eok(row.get(i_col, 0)) if i_col else 0.0,
            "individual": amount_to_eok(row.get(p_col, 0)) if p_col else 0.0,
        }, True

    return empty, False


def fetch_market_flow_for_day(trade_date: str, errors: list[str]) -> tuple[dict[str, float], bool]:
    """
    KOSPI 전체 투자자별 수급.
    여러 BLD를 순서대로 시도합니다.
    """
    attempts = [
        (
            "dbms/MDC/STAT/standard/MDCSTAT02201",
            {
                "mktId": "STK",
                "trdDd": trade_date,
                "money": "1",
            },
        ),
        (
            "dbms/MDC/STAT/standard/MDCSTAT02301",
            {
                "mktId": "STK",
                "trdDd": trade_date,
                "money": "1",
            },
        ),
    ]

    local_errors: list[str] = []

    for bld, params in attempts:
        data = krx_json(bld, params, local_errors, f"market flow {trade_date} {bld}")
        rows = extract_rows(data)
        flow, ok = parse_flow_from_rows(rows)
        if ok:
            return flow, True

    errors.extend(local_errors[-2:])
    return {"foreign": 0.0, "institution": 0.0, "individual": 0.0}, False


def fetch_stock_flow_for_day(ticker: str, trade_date: str, errors: list[str]) -> tuple[dict[str, float], bool]:
    """
    개별종목 투자자별 수급.
    KRX endpoint별 파라미터 차이가 있어 여러 조합을 시도합니다.
    """
    isin = ISIN.get(ticker, ticker)

    base_param_variants = [
        {
            "mktId": "STK",
            "trdDd": trade_date,
            "money": "1",
            "isuCd": isin,
        },
        {
            "mktId": "STK",
            "trdDd": trade_date,
            "money": "1",
            "isuCd": ticker,
        },
        {
            "mktId": "STK",
            "strtDd": trade_date,
            "endDd": trade_date,
            "money": "1",
            "isuCd": isin,
        },
        {
            "mktId": "STK",
            "strtDd": trade_date,
            "endDd": trade_date,
            "money": "1",
            "isuCd": ticker,
        },
        {
            "mktId": "STK",
            "trdDd": trade_date,
            "money": "1",
            "tboxisuCd_finder_stkisu0_0": ticker,
            "isuCd": isin,
            "isuCd2": ticker,
            "codeNmisuCd_finder_stkisu0_0": TICKERS.get(ticker, ticker),
        },
    ]

    blds = [
        "dbms/MDC/STAT/standard/MDCSTAT02303",
        "dbms/MDC/STAT/standard/MDCSTAT02301",
        "dbms/MDC/STAT/standard/MDCSTAT02401",
        "dbms/MDC/STAT/standard/MDCSTAT02501",
    ]

    local_errors: list[str] = []

    for bld in blds:
        for params in base_param_variants:
            data = krx_json(bld, params, local_errors, f"stock flow {ticker} {trade_date} {bld}")
            rows = extract_rows(data)
            flow, ok = parse_flow_from_rows(rows)
            if ok:
                return flow, True

    errors.append(f"stock flow {ticker} {trade_date}: no parsable KRX output")
    return {"foreign": 0.0, "institution": 0.0, "individual": 0.0}, False


def latest_trading_day(today: datetime, errors: list[str]) -> str:
    for offset in range(0, 10):
        d = today - timedelta(days=offset)
        ds = ymd(d)
        _, ok = fetch_market_flow_for_day(ds, [])
        if ok:
            return ds

    errors.append("최근 10일 내 KOSPI 수급 거래일 데이터를 찾지 못했습니다.")
    return ymd(today)


def already_collected_by_generated_date() -> bool:
    """
    18:00~19:00 재시도 중, 오늘 이미 updateReady=true 데이터를 만들었으면 이후 실행 skip.
    """
    if not OUT.exists():
        return False

    try:
        old = json.loads(OUT.read_text(encoding="utf-8"))
    except Exception:
        return False

    now = datetime.now(KST)
    generated = str(old.get("generatedAt", ""))
    return old.get("updateReady") is True and generated.startswith(now.strftime("%Y-%m-%d"))


def check_fresh() -> None:
    skip = already_collected_by_generated_date()

    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as f:
            f.write(f"skip={'true' if skip else 'false'}\n")

    print("skip=true" if skip else "skip=false")


def fetch_price_table(trade_date: str, errors: list[str]) -> dict[str, dict[str, Any]]:
    """
    가격은 부가 정보입니다.
    실패해도 updateReady에 영향 주지 않습니다.
    """
    params = {
        "mktId": "STK",
        "trdDd": trade_date,
        "share": "1",
        "money": "1",
    }
    data = krx_json("dbms/MDC/STAT/standard/MDCSTAT01501", params, errors, "price table")
    rows = extract_rows(data)
    result: dict[str, dict[str, Any]] = {}

    for row in rows:
        code_col = find_col(row, ["ISU_SRT_CD", "종목코드"])
        if not code_col:
            continue
        code = str(row.get(code_col, "")).zfill(6)
        if code not in TICKERS:
            continue

        close_col = find_col(row, ["TDD_CLSPRC", "종가"])
        pct_col = find_col(row, ["FLUC_RT", "등락률"])
        diff_col = find_col(row, ["CMPPREVDD_PRC", "대비"])

        close = clean_number(row.get(close_col, 0)) if close_col else 0.0
        pct = clean_number(row.get(pct_col, 0)) if pct_col else 0.0
        diff = clean_number(row.get(diff_col, 0)) if diff_col else 0.0

        result[code] = {
            "close": close,
            "pct": pct,
            "diff": diff,
        }

    return result


def fetch_foreign_ratio(ticker: str, trade_date: str, errors: list[str]) -> dict[str, Any]:
    """
    외인지분율은 부가 정보입니다. 실패해도 unavailable.
    """
    isin = ISIN.get(ticker, ticker)
    attempts = [
        {"trdDd": trade_date, "isuCd": isin, "mktId": "STK"},
        {"trdDd": trade_date, "isuCd": ticker, "mktId": "STK"},
    ]

    for params in attempts:
        data = krx_json(
            "dbms/MDC/STAT/standard/MDCSTAT03701",
            params,
            errors,
            f"foreign ratio {ticker}",
        )
        rows = extract_rows(data)
        if not rows:
            continue

        row = rows[0]
        ratio_col = find_col(row, ["FORN_HD_QTY_RT", "FORN_SHR_RT", "지분율", "외국인비율", "한도소진률"])
        if ratio_col:
            ratio = clean_number(row.get(ratio_col, None), default=float("nan"))
            if not math.isnan(ratio):
                return {
                    "available": True,
                    "ratio": round(ratio, 2),
                    "high": None,
                    "highDate": None,
                    "pctOfHigh": None,
                    "labels": [],
                    "confirmed": [round(ratio, 2)],
                    "history": [{"period": trade_date_display(trade_date), "ratio": round(ratio, 2), "note": "최근"}],
                }

    return {
        "available": False,
        "ratio": None,
        "high": None,
        "highDate": None,
        "pctOfHigh": None,
        "labels": [],
        "confirmed": [],
        "history": [],
    }


def compute_ratio_drop(foreign_info: dict[str, Any]) -> float | None:
    vals = [v for v in foreign_info.get("confirmed", []) if isinstance(v, (int, float))]
    if len(vals) < 2:
        return None
    return round(vals[-2] - vals[-1], 2)


def calc_divergence_from_rows(
    flow_rows: list[dict[str, Any]],
    ratio_drops: list[float | None],
) -> dict[str, Any]:
    if not flow_rows:
        return {"score": 0, "level": 1, "details": {}, "availableWeights": {}}

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
            "creditGrowthPct": None,
            "avgRatioDropPctp": round(sum(valid_ratio_drops) / len(valid_ratio_drops), 2) if valid_ratio_drops else None,
        },
        "availableWeights": {k: round(v / total_weight, 3) for k, v in used.items()},
    }


def level_label(level: int) -> str:
    return {4: "위험", 3: "경계", 2: "주의", 1: "보통"}.get(level, "보통")


def build_weekly(kospi_history: list[dict[str, Any]], ratio_drops: list[float | None]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    temp: list[dict[str, Any]] = []

    for item in kospi_history[-7:]:
        flow = item["flow"]
        temp.append(flow)
        score = calc_divergence_from_rows(temp, ratio_drops)
        items.append(
            {
                "date": display_date(item["date"]),
                "foreign": flow["foreign"],
                "individual": flow["individual"],
                "score": score["score"],
                "danger": score["score"] >= 70,
            }
        )

    if items:
        items[-1]["today"] = True

    return items


def collect_flow_history(trade_date: str, errors: list[str]) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    end = pd.Timestamp(trade_date)
    dates = [ymd(end - pd.Timedelta(days=i)) for i in range(0, 14)]
    dates = list(reversed(dates))

    kospi_history: list[dict[str, Any]] = []
    ticker_history: dict[str, list[dict[str, Any]]] = {t: [] for t in TICKERS}

    for ds in dates:
        flow, ok = fetch_market_flow_for_day(ds, errors)
        if ok:
            kospi_history.append({"date": ds, "flow": flow})

        for ticker in TICKERS:
            t_flow, t_ok = fetch_stock_flow_for_day(ticker, ds, errors)
            if t_ok:
                ticker_history[ticker].append({"date": ds, "flow": t_flow})

    return kospi_history, ticker_history


def build_market_cards(
    trade_date: str,
    kospi_history: list[dict[str, Any]],
    ticker_history: dict[str, list[dict[str, Any]]],
    prices: dict[str, dict[str, Any]],
    foreign_infos: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []

    kospi_flow = kospi_history[-1]["flow"] if kospi_history else {"foreign": 0.0, "institution": 0.0, "individual": 0.0}
    cards.append(
        {
            "name": "KOSPI",
            "code": "코스피 전체",
            "price": "N/A",
            "change": "N/A",
            "flow": kospi_flow,
            "maxAbs": max(abs(v) for v in kospi_flow.values()) or 1,
        }
    )

    for ticker, name in TICKERS.items():
        hist = ticker_history.get(ticker, [])
        flow = hist[-1]["flow"] if hist else {"foreign": 0.0, "institution": 0.0, "individual": 0.0}
        p = prices.get(ticker, {})
        close = p.get("close", 0)
        pct = p.get("pct", 0)
        finfo = foreign_infos.get(ticker, {})

        cards.append(
            {
                "name": name,
                "code": ticker,
                "price": f"{close:,.0f}원" if close else "N/A",
                "change": f"{pct:+.2f}%" if close else "N/A",
                "flow": flow,
                "maxAbs": max(abs(v) for v in flow.values()) or 1,
                "foreignRatio": finfo.get("ratio"),
                "foreignPctOfHigh": finfo.get("pctOfHigh"),
            }
        )

    return cards


def main() -> None:
    if "--check-fresh" in sys.argv:
        check_fresh()
        return

    errors: list[str] = []
    now = datetime.now(KST)

    trade_date = latest_trading_day(now, errors)

    kospi_history, ticker_history = collect_flow_history(trade_date, errors)
    prices = fetch_price_table(trade_date, errors)

    foreign_infos = {
        ticker: {"name": TICKERS[ticker], "code": ticker, **fetch_foreign_ratio(ticker, trade_date, errors)}
        for ticker in TICKERS
    }
    ratio_drops = [compute_ratio_drop(v) for v in foreign_infos.values()]

    div = calc_divergence_from_rows([x["flow"] for x in kospi_history[-30:]], ratio_drops)
    weekly = build_weekly(kospi_history, ratio_drops)
    market_cards = build_market_cards(trade_date, kospi_history, ticker_history, prices, foreign_infos)

    update_ready = bool(
        kospi_history
        and all(len(ticker_history.get(ticker, [])) > 0 for ticker in TICKERS)
    )

    credit = {
        "available": False,
        "source": None,
        "message": "이번 KRX 직접수집 버전에서는 신용잔고를 아직 연결하지 않았습니다. 가짜값은 넣지 않았습니다.",
        "history": [],
    }

    backtest = {
        "available": False,
        "thresholds": [],
        "cases": [],
        "note": "이번 KRX 직접수집 버전에서는 백테스트를 아직 연결하지 않았습니다.",
    }

    level = int(div["level"])

    payload = {
        "schemaVersion": 4,
        "status": "ok" if update_ready and not errors else "partial" if update_ready else "not_ready",
        "updateReady": update_ready,
        "generatedAt": now.strftime("%Y-%m-%d %H:%M:%S KST"),
        "tradeDate": trade_date_display(trade_date),
        "source": "KRX Data Marketplace direct JSON",
        "notice": "KRX 직접수집 버전입니다. 실패 항목은 가짜값 없이 null 또는 수집 불가로 표시합니다. 투자 권유가 아닙니다.",
        "alert": {
            "level": level,
            "label": level_label(level),
            "message": f"수급 다이버전스 점수 {div['score']}점 · {level_label(level)} 구간",
        },
        "market": market_cards,
        "divergence": div,
        "weekly": weekly,
        "foreign": foreign_infos,
        "credit": credit,
        "backtest": backtest,
        "debug": {
            "kospiHistoryCount": len(kospi_history),
            "tickerHistoryCount": {ticker: len(v) for ticker, v in ticker_history.items()},
            "unitNote": "수급 금액은 KRX 원자료 단위를 억원으로 자동 추정했습니다.",
        },
        "errors": errors[-40:],
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"Wrote {OUT.relative_to(ROOT)}")
    print(
        f"status={payload['status']}, "
        f"updateReady={payload['updateReady']}, "
        f"tradeDate={payload['tradeDate']}, "
        f"errors={len(errors)}"
    )


if __name__ == "__main__":
    main()
