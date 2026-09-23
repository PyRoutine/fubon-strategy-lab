"""新版 SOP 的唯一純策略引擎；不讀寫資料庫、不送券商委託。"""
from __future__ import annotations

from math import floor
from datetime import datetime, time, timedelta, timezone


LOW_WATERLINE_PCT = 50.0
MAX_ROTATION_MULTIPLIER = 5
PLAN_VALIDITY = timedelta(minutes=5)
TW_TZ = timezone(timedelta(hours=8))
STRATEGY_CUTOFF = time(13, 20)


def _num(value, field):
    try:
        return float(str(value).replace(",", "").replace("%", "").strip())
    except (TypeError, ValueError) as error:
        raise ValueError(f"缺少有效{field}") from error


def _symbol(value):
    return str(value or "").strip().upper()


def _quote(value):
    if not isinstance(value, dict):
        raise ValueError("即時報價格式不正確")
    reference = value.get("reference_price", value.get("last_price", value.get("price")))
    return {
        "reference": _num(reference, "即時市價"),
        "board_bid": _positive((value.get("board") or {}).get("bid")),
        "board_ask": _positive((value.get("board") or {}).get("ask")),
        "odd_bid": _positive((value.get("odd_lot") or {}).get("bid")),
        "odd_ask": _positive((value.get("odd_lot") or {}).get("ask")),
    }


def _positive(value):
    try:
        value = float(value)
        return value if value > 0 else None
    except (TypeError, ValueError):
        return None


def _zone_map(snapshot):
    rows = []
    for zone_name in ("raw_value_zone", "eligible_value_zone"):
        rows.extend((snapshot.get(zone_name) or {}).get("stocks") or [])
    result = {}
    for row in rows:
        if isinstance(row, dict) and _symbol(row.get("symbol")):
            result[_symbol(row["symbol"])] = row
    return result


def _sell_legs(symbol, qty, quote, nav, *, protected=False):
    """依實際股數比較整股／零股賣價，選可成交價格較好的市場。"""
    if qty <= 0:
        return []

    board_bid = quote["board_bid"]
    odd_bid = quote["odd_bid"]
    legs = []

    # 未滿一張只能走零股。
    if qty < 1000:
        if odd_bid:
            legs.append(_leg(symbol, qty, "零股", odd_bid, nav, protected))
        return legs

    # 滿一張時，若零股買一更高，全部拆成合法零股單以取得較佳賣價。
    if odd_bid and (not board_bid or odd_bid > board_bid):
        remaining = qty
        while remaining:
            odd_qty = min(remaining, 999)
            legs.append(_leg(symbol, odd_qty, "零股", odd_bid, nav, protected))
            remaining -= odd_qty
        return legs

    # 整股買一較佳或同價時，整張走整股，尾數走零股。
    board_qty = qty // 1000 * 1000
    if board_qty and board_bid:
        legs.append(_leg(symbol, board_qty, "整股", board_bid, nav, protected))
    remainder = qty - board_qty
    while remainder:
        if not odd_bid:
            break
        odd_qty = min(remainder, 999)
        legs.append(_leg(symbol, odd_qty, "零股", odd_bid, nav, protected))
        remainder -= odd_qty
    return legs


def _leg(symbol, qty, market, price, nav, protected):
    nav_protected = bool(protected and nav and price < nav)
    return {
        "side": "SELL", "symbol": symbol, "quantity": qty, "market": market,
        "limit_price": round(price, 6), "estimated_amount": round(qty * price, 2),
        "nav_protected": nav_protected,
    }


def _buy_legs(symbol, qty, quote, nav, reason):
    """買單永遠掛 NAV；依實際股數比較整股／零股賣價，選較佳市場。"""
    if qty <= 0:
        return []

    board_ask = quote["board_ask"]
    odd_ask = quote["odd_ask"]
    legs = []

    # 未滿一張只能走零股。
    if qty < 1000:
        if odd_ask:
            legs.append(_buy_leg(symbol, qty, "零股", nav, reason))
        return legs

    # 滿一張時，若零股賣一更低，全部拆成合法零股單以取得較佳買價環境。
    if odd_ask and (not board_ask or odd_ask < board_ask):
        remaining = qty
        while remaining:
            odd_qty = min(remaining, 999)
            legs.append(_buy_leg(symbol, odd_qty, "零股", nav, reason))
            remaining -= odd_qty
        return legs

    # 整股賣一較佳或同價時，整張走整股，尾數走零股。
    remaining = qty
    board_qty = remaining // 1000 * 1000
    if board_qty and board_ask:
        legs.append(_buy_leg(symbol, board_qty, "整股", nav, reason))
        remaining -= board_qty
    while remaining:
        if not odd_ask:
            break
        odd_qty = min(remaining, 999)
        legs.append(_buy_leg(symbol, odd_qty, "零股", nav, reason))
        remaining -= odd_qty
    return legs


def _buy_leg(symbol, qty, market, nav, reason):
    return {
        "side": "BUY", "symbol": symbol, "quantity": qty, "market": market,
        "limit_price": round(nav, 6), "estimated_amount": round(qty * nav, 2),
        "reason_code": "VALUE_ZONE_BUY", "reason": reason,
    }


def _best_buy_ask(quote, qty):
    """回傳此股數真正可使用市場中的較佳賣一。"""
    if qty <= 0:
        return None
    if qty < 1000:
        return quote["odd_ask"]
    asks = [price for price in (quote["board_ask"], quote["odd_ask"]) if price]
    return min(asks) if asks else None


def _buy_discount_pct(quote, nav, qty):
    """以該股數真正可使用市場的最佳賣一比較折溢價。"""
    ask = _best_buy_ask(quote, qty)
    if not ask:
        return None
    return (ask - nav) / nav * 100


def _partial_candidate_legs(item, amount_needed, *, protected=False):
    """從最後一檔找出達標的最小合法股數；優先讓小額需求走零股。"""
    if protected:
        quote, nav, symbol = item["quote"], item["nav"], item["symbol"]
        choices = []
        if quote["odd_bid"] and quote["odd_bid"] >= nav:
            qty = min(item["qty"], max(1, int(amount_needed / quote["odd_bid"] + 0.999999)))
            choices.append([_leg(symbol, qty, "零股", quote["odd_bid"], nav, True)])
        if quote["board_bid"] and quote["board_bid"] >= nav and item["qty"] >= 1000:
            shares = int(amount_needed / quote["board_bid"] + 0.999999)
            qty = min(item["qty"] // 1000 * 1000, max(1000, ((shares + 999) // 1000) * 1000))
            choices.append([_leg(symbol, qty, "整股", quote["board_bid"], nav, True)])
        if choices:
            return min(choices, key=lambda legs: sum(leg["estimated_amount"] for leg in legs))
    low, high, answer = 1, item["qty"], []
    while low <= high:
        qty = (low + high) // 2
        legs = _sell_legs(item["symbol"], qty, item["quote"], item["nav"], protected=protected)
        legs = [leg for leg in legs if not leg["nav_protected"]]
        if sum(leg["quantity"] for leg in legs) != qty:
            low = qty + 1
            continue
        if sum(leg["estimated_amount"] for leg in legs) >= amount_needed:
            answer, high = legs, qty - 1
        else:
            low = qty + 1
    return answer


def build_affordable_buys(plan, cash, submitted_sell_amount=0.0):
    """送出賣單後唯一的買單重算器：不等成交，只使用成功送出的賣單估計金額。"""
    if not isinstance(plan, dict):
        raise ValueError("策略計畫格式不正確")
    available_cash = _num(cash, "可用現金") + max(0.0, _num(submitted_sell_amount, "成功送出賣單金額"))
    n = int(plan.get("rotation_multiplier", 0) or 0)
    if plan.get("mode") == "HIGH":
        rotation_target = _num(plan.get("rotation_target_amount", 0), "N 資金目標")
        scale = min(1.0, max(0.0, _num(submitted_sell_amount, "成功送出賣單金額") / rotation_target)) if rotation_target else 0.0
    else:
        scale = 0.0
    targets = []
    for target in plan.get("buy_targets") or []:
        nav = _num(target.get("nav"), "NAV")
        quote = target.get("quote") or {}
        app_shares = int(_num(target.get("app_shares"), "App 位階股數"))
        desired = app_shares * 2 if plan.get("mode") == "LOW" else app_shares + floor(app_shares * n * scale)
        discount = _buy_discount_pct(quote, nav, desired)
        if discount is None:
            continue
        targets.append((discount, _symbol(target.get("symbol")), desired, nav, quote))
    buys = []
    for discount, symbol, desired, nav, quote in sorted(targets, key=lambda row: (row[0], row[1])):
        affordable = min(desired, floor(available_cash / nav))
        legs = _buy_legs(symbol, affordable, quote, nav,
            "LOW：App 位階股數×2，不扣既有庫存。" if plan.get("mode") == "LOW"
            else f"HIGH：基礎 App 位階股數＋依成功送出賣單比例調整的 N 部分，N={n}。")
        for leg in legs:
            if leg["estimated_amount"] <= available_cash:
                buys.append(leg)
                available_cash -= leg["estimated_amount"]
    return buys


def build_submission_orders(plan, cash, submitted_sell_amount=0.0, *, include_buys=True):
    """把純策略 leg 轉成執行層訂單；執行層不得自行推導任何策略。"""
    sells = [{
        "side": "SELL", "symbol": leg["symbol"], "qty": leg["quantity"],
        "market": leg["market"], "limit_price": leg["limit_price"],
        "reason_code": leg.get("reason_code", "ROTATION"),
        "reason_text": leg.get("reason", "依純策略引擎調節。"),
    } for leg in plan.get("planned_sells") or []]
    if not include_buys:
        return sells
    buys = [{
        "side": "BUY", "symbol": leg["symbol"], "qty": leg["quantity"],
        "market": leg["market"], "limit_price": leg["limit_price"],
        "reason_code": leg.get("reason_code", "VALUE_ZONE_BUY"),
        "reason_text": leg.get("reason", "依純策略引擎佈局。"),
    } for leg in build_affordable_buys(plan, cash, submitted_sell_amount)]
    return sells + buys


def build_midday_spiral_plan(ark_snapshot, portfolio, live_quotes, *, now=None):
    """12:00 專用：不重跑 LOW/HIGH 正常調節，只從既有庫存選一組雙股螺旋。"""
    plan = build_strategy_plan(ark_snapshot, portfolio, live_quotes, spiral="midday", now=now)
    return {
        "snapshot_id": plan["snapshot_id"], "account": plan["account"],
        "mode": plan["mode"], "valid_until": plan["valid_until"],
        "planned_sells": plan["planned_sells"], "planned_buys": plan["planned_buys"],
        "spiral_plan": plan["spiral_plan"], "holdings": plan["holdings"],
        "reason": "12:00 午盤檢查：當日尚未使用雙股螺旋，僅檢查既有庫存。",
    }


def _valid_until(now):
    cutoff = now.replace(hour=STRATEGY_CUTOFF.hour, minute=STRATEGY_CUTOFF.minute,
                         second=0, microsecond=0)
    if now >= cutoff:
        raise ValueError("已超過台灣時間 13:20，禁止產生新的策略卡")
    return min(now + PLAN_VALIDITY, cutoff)


def _build_spiral_plan(snapshot, holdings, selected_sells):
    """雙股螺旋只定案配對與賣方上限；成交後買量由追蹤器依實收金額換算。"""
    sold = {}
    for leg in selected_sells:
        sold[leg["symbol"]] = sold.get(leg["symbol"], 0) + leg["quantity"]
    adjustments = {
        _symbol(row.get("symbol")): row.get("app_reduce_min_shares")
        for row in ((snapshot.get("app_adjustments") or {}).get("stocks") or [])
        if isinstance(row, dict) and _symbol(row.get("symbol"))
    }
    remaining = [item for item in holdings if item["qty"] > sold.get(item["symbol"], 0)]
    sellers = []
    buyers = []
    for item in remaining:
        if not item["nav"]:
            continue
        remaining_qty = item["qty"] - sold.get(item["symbol"], 0)
        ask_premium = _best_buy_ask(item["quote"], remaining_qty)
        if ask_premium:
            ask_premium = (ask_premium - item["nav"]) / item["nav"] * 100
        if item["return_pct"] > 0 and adjustments.get(item["symbol"]):
            sell_qty = min(remaining_qty, int(_num(adjustments[item["symbol"]], "App 建議調節股數")))
            sell_legs = _sell_legs(item["symbol"], sell_qty, item["quote"], item["nav"])
            if sum(leg["quantity"] for leg in sell_legs) == sell_qty:
                sell_amount = sum(leg["estimated_amount"] for leg in sell_legs)
                bid_premium = (sell_amount / sell_qty - item["nav"]) / item["nav"] * 100
                sellers.append((bid_premium, item, sell_qty, sell_legs))
        if ask_premium is not None:
            buyers.append((ask_premium, item))
    if not sellers or len(buyers) < 2:
        return None
    # 同溢價時代號小者優先，避免每次重算任意換配對。
    seller_premium, seller, sell_qty, sell_legs = sorted(
        sellers, key=lambda value: (-value[0], value[1]["symbol"])
    )[0]
    buyer_rows = [row for row in buyers if row[1]["symbol"] != seller["symbol"]]
    if not buyer_rows:
        return None
    buyer_premium, buyer = min(buyer_rows, key=lambda value: (value[0], value[1]["symbol"]))
    if seller_premium - buyer_premium <= 0.5:
        return None
    expected_amount = sum(leg["estimated_amount"] for leg in sell_legs)
    return {
        "status": "PLANNED", "seller_symbol": seller["symbol"], "buyer_symbol": buyer["symbol"],
        "seller_premium_pct": round(seller_premium, 6), "buyer_premium_pct": round(buyer_premium, 6),
        "premium_gap_pct": round(seller_premium - buyer_premium, 6),
        "seller_planned_qty": sell_qty, "seller_legs": sell_legs,
        "buyer_nav": buyer["nav"], "buyer_max_qty": floor(expected_amount / buyer["nav"]),
        "reason": "雙股螺旋：正報酬賣方折溢價最高、買方折溢價最低，差距超過 0.5 個百分點。",
    }


def build_strategy_plan(ark_snapshot, portfolio, live_quotes, *, spiral=None, now=None):
    """依新版 SOP 回傳可重播的決策計畫；此函式永不建立訂單。"""
    if not isinstance(ark_snapshot, dict) or not isinstance(portfolio, dict):
        raise ValueError("snapshot 與 portfolio 必須是 JSON 物件")
    if now is None:
        now = datetime.now(TW_TZ)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=TW_TZ)
    eligible_rows = {
        _symbol(row.get("symbol")): row
        for row in ((ark_snapshot.get("eligible_value_zone") or {}).get("stocks") or [])
        if isinstance(row, dict) and _symbol(row.get("symbol"))
    }
    if not eligible_rows:
        raise ValueError("ARK Snapshot 缺少有效價值區")
    zones = _zone_map(ark_snapshot)
    cash = _num(portfolio.get("available_idle_money", portfolio.get("cash")), "閒錢")
    quotes = {_symbol(k): _quote(v) for k, v in (live_quotes or {}).items()}
    warming = {
        _symbol(row.get("symbol")) for row in ((ark_snapshot.get("warming_zone") or {}).get("cross_table") or [])
        if isinstance(row, dict) and row.get("is_warming")
    }
    holdings = []
    for symbol, source in (portfolio.get("inventory_details") or {}).items():
        symbol = _symbol(symbol)
        if not symbol or not isinstance(source, dict):
            continue
        qty = int(_num(source.get("qty", 0), f"{symbol} 持股股數"))
        if qty <= 0:
            continue
        quote = quotes.get(symbol)
        if not quote:
            raise ValueError(f"富邦缺少 {symbol} 即時報價")
        avg = _num(source.get("avg_cost"), f"{symbol} 平均成本")
        nav = _positive((zones.get(symbol) or {}).get("nav"))
        executable_legs = _sell_legs(symbol, qty, quote, nav)
        executable_qty = sum(leg["quantity"] for leg in executable_legs)
        if executable_qty != qty:
            raise ValueError(f"富邦缺少 {symbol} 完整可賣買一，禁止以不可成交價格判定策略")
        realizable_value = sum(leg["estimated_amount"] for leg in executable_legs)
        pnl = realizable_value - avg * qty
        ret = pnl / (avg * qty) * 100 if avg and qty else 0.0
        holdings.append({
            "symbol": symbol, "qty": qty, "avg_cost": avg, "nav": nav, "quote": quote,
            "return_pct": round(ret, 6), "unrealized_pnl": round(pnl, 2),
            "is_eligible": symbol in eligible_rows, "is_warming": symbol in warming,
            "market_value": round(qty * quote["reference"], 2),
        })
    holding_value = sum(item["market_value"] for item in holdings)
    total_asset = cash + holding_value
    waterline = holding_value / total_asset * 100 if total_asset else 0.0
    mode = "LOW" if waterline <= LOW_WATERLINE_PCT else "HIGH"
    x_amount = sum(
        _num(row.get("nav"), f"{symbol} NAV") * int(_num(row.get("app_shares"), f"{symbol} App 位階股數"))
        for symbol, row in eligible_rows.items()
    )

    if spiral == "midday":
        generated_at = now
        return {
            "preview_only": True, "snapshot_id": ark_snapshot.get("snapshot_id"),
            "account": ark_snapshot.get("account"), "mode": mode,
            "strategy_generated_at": generated_at.isoformat(),
            "valid_until": _valid_until(generated_at),
            "actual_waterline_pct": round(waterline, 6), "x_amount": round(x_amount, 2),
            "rotation_multiplier": 0, "planned_sells": [], "planned_buys": [],
            "holdings": holdings,
            "spiral_plan": _build_spiral_plan(ark_snapshot, holdings, []),
            "funding_source": "12:00 僅檢查既有庫存的雙股螺旋；不執行正常 LOW/HIGH 調節。",
        }

    mandatory, candidates, low_loss_candidates, explanations = [], [], [], []
    warm_realized_profit = 0.0
    for item in holdings:
        symbol = item["symbol"]
        if item["is_warming"] and not item["is_eligible"]:
            mandatory.extend(_sell_legs(symbol, item["qty"], item["quote"], item["nav"]))
            explanations.append(f"{symbol}：升溫且不在有效價值區，依規則全數調節。")
            continue
        if item["is_warming"] and item["return_pct"] > 0:
            warm_legs = _sell_legs(symbol, item["qty"], item["quote"], item["nav"])
            mandatory.extend(warm_legs)
            warm_realized_profit += sum(
                max(0.0, (leg["limit_price"] - item["avg_cost"]) * leg["quantity"])
                for leg in warm_legs
            )
            explanations.append(f"{symbol}：升溫且獲利，依規則先獲利了結。")
            continue
        if mode == "HIGH" and item["is_warming"] and item["is_eligible"] and item["return_pct"] < 0:
            mandatory.extend(_sell_legs(symbol, item["qty"], item["quote"], item["nav"]))
            explanations.append(f"{symbol}：HIGH 下升溫、有效價值區且負報酬，獨立必賣，不參與 P 排序。")
            continue
        if mode == "LOW":
            if item["return_pct"] <= -5:
                low_loss_candidates.append(item)
            continue
        if mode != "HIGH":
            continue
        if item["return_pct"] <= -5:
            priority, key, reason = "P1", (1, item["unrealized_pnl"], symbol), "報酬率 ≤ -5%"
        elif item["return_pct"] < 0:
            priority, key, reason = "P2", (2, item["unrealized_pnl"], symbol), "其他負報酬，未實現虧損金額較大優先"
        elif not item["is_eligible"] and item["return_pct"] > 0:
            priority, key, reason = "P3", (3, -item["unrealized_pnl"], symbol), "不在有效價值區且正報酬"
        elif item["is_eligible"] and item["return_pct"] > 0:
            priority, key, reason = "P4", (4, -item["unrealized_pnl"], symbol), "有效價值區且正報酬；逐 leg NAV 保護"
        else:
            continue
        legs = _sell_legs(symbol, item["qty"], item["quote"], item["nav"], protected=priority == "P4")
        candidates.append({"item": item, "priority": priority, "key": key, "reason": reason, "legs": legs})
    candidates.sort(key=lambda row: row["key"])

    # LOW：不為籌資賣虧損股；僅把升溫已實現獲利配對成等額 P1 虧損。
    low_loss_offset_legs = []
    remaining_offset = warm_realized_profit
    # LOW 弱勢整理：報酬率最差優先，且每天最多 2 檔。
    for item in sorted(low_loss_candidates, key=lambda row: (row["return_pct"], row["symbol"]))[:2]:
        if remaining_offset <= 0:
            break
        loss_per_share = max(0.0, -item["unrealized_pnl"] / item["qty"])
        qty = min(item["qty"], int(remaining_offset // loss_per_share)) if loss_per_share else 0
        if qty <= 0:
            continue
        legs = _sell_legs(item["symbol"], qty, item["quote"], item["nav"])
        low_loss_offset_legs.extend(legs)
        remaining_offset -= qty * loss_per_share
        explanations.append(
            f"{item['symbol']}：LOW 僅以升溫已實現獲利對沖 P1 虧損；"
            f"本次對沖目標 {qty * loss_per_share:,.2f}。"
        )

    # HIGH：N 必須由可實際掛出的賣單左一價計算；P4 被 NAV 保護的 leg 不計入。
    available_legs = mandatory[:]
    for candidate in candidates:
        available_legs.extend(leg for leg in candidate["legs"] if not leg["nav_protected"])
    capacity = sum(leg["estimated_amount"] for leg in available_legs)
    n = min(MAX_ROTATION_MULTIPLIER, floor(capacity / x_amount)) if mode == "HIGH" and x_amount else 0
    target_sell = n * x_amount
    # 必賣先完整保留；P1~P4 的最後一檔才依缺口縮小。
    selected = mandatory[:]
    selected_amount = sum(leg["estimated_amount"] for leg in selected)
    if mode == "HIGH":
        for candidate in candidates:
            legal_legs = [leg for leg in candidate["legs"] if not leg["nav_protected"]]
            if not legal_legs or selected_amount >= target_sell:
                break
            candidate_amount = sum(leg["estimated_amount"] for leg in legal_legs)
            if selected_amount + candidate_amount <= target_sell:
                selected.extend(legal_legs)
                selected_amount += candidate_amount
                continue
            partial = _partial_candidate_legs(
                candidate["item"], target_sell - selected_amount,
                protected=candidate["priority"] == "P4",
            )
            selected.extend(partial)
            selected_amount += sum(leg["estimated_amount"] for leg in partial)
            break

    # mandatory sells remain mandatory even when N=0.
    for leg in mandatory + low_loss_offset_legs:
        if leg not in selected:
            selected.append(leg)
            selected_amount += leg["estimated_amount"]
    for leg in selected:
        if leg in mandatory:
            leg["reason_code"] = "WARM_MANDATORY"
            leg["reason"] = next(text for text in explanations if text.startswith(leg["symbol"] + "："))
        elif leg in low_loss_offset_legs:
            leg["reason_code"] = "LOW_LOSS_OFFSET"
            leg["reason"] = next(text for text in explanations if text.startswith(leg["symbol"] + "："))
        else:
            match = next(candidate for candidate in candidates if candidate["item"]["symbol"] == leg["symbol"])
            leg["reason_code"] = match["priority"]
            leg["reason"] = match["reason"]

    spiral_plan = _build_spiral_plan(ark_snapshot, holdings, selected)

    multiplier = 2 if mode == "LOW" else 1 + n
    buy_targets = []
    for symbol, row in eligible_rows.items():
        nav = _num(row.get("nav"), f"{symbol} NAV")
        shares = int(_num(row.get("app_shares"), f"{symbol} App 位階股數"))
        quote = quotes.get(symbol)
        if not quote:
            raise ValueError(f"富邦缺少 {symbol} 即時報價")
        buy_targets.append({"symbol": symbol, "nav": nav, "app_shares": shares, "quote": quote})

    generated_at = now
    valid_until = _valid_until(generated_at)
    return {
        "preview_only": True, "snapshot_id": ark_snapshot.get("snapshot_id"),
        "account": ark_snapshot.get("account"), "mode": mode,
        "strategy_generated_at": generated_at.isoformat(),
        "valid_until": valid_until.isoformat(),
        "actual_waterline_pct": round(waterline, 6), "x_amount": round(x_amount, 2),
        "expected_sell_capacity": round(capacity, 2), "rotation_capacity": round(capacity, 2),
        "rotation_multiplier": n, "target_multiplier": multiplier,
        "rotation_target_amount": round(target_sell, 2),
        "rotation_remaining_amount": round(max(0, target_sell - selected_amount), 2),
        "funding_source": "；".join(explanations) or ("P 排序可調節持股" if mode == "HIGH" else "LOW 不需調節"),
        "planned_sell_amount": round(sum(leg["estimated_amount"] for leg in selected), 2),
        "planned_buy_amount": round(sum(row["nav"] * row["app_shares"] * multiplier for row in buy_targets), 2),
        "planned_sells": selected, "planned_buys": [], "buy_targets": buy_targets,
        "trade_plan": selected,
        "spiral_plan": spiral_plan,
        "orders": [],
        "holdings": holdings,
        "rotation_candidates": [{"symbol": r["item"]["symbol"], "priority": r["priority"], "reason": r["reason"]} for r in candidates],
    }
