import random
import unittest
from datetime import datetime

from modules.pure_strategy import build_affordable_buys, build_midday_spiral_plan, build_strategy_plan as _build_strategy_plan, build_submission_orders


TEST_NOW = datetime.fromisoformat("2026-09-23T10:00:00+08:00")


def build_strategy_plan(*args, **kwargs):
    return _build_strategy_plan(*args, now=TEST_NOW, **kwargs)


def quote(price=100, board_bid=100, board_ask=101, odd_bid=100, odd_ask=101):
    return {
        "reference_price": price,
        "board": {"bid": board_bid, "ask": board_ask},
        "odd_lot": {"bid": odd_bid, "ask": odd_ask},
    }


def snapshot(*, eligible=("0050",), shares=10, warming=()):
    rows = [{"symbol": s, "nav": 100, "app_shares": shares} for s in eligible]
    return {
        "snapshot_id": "fixture", "account": "fixture@example.invalid",
        "eligible_value_zone": {"stocks": rows},
        "raw_value_zone": {"stocks": rows + [{"symbol": "9999", "nav": 100, "app_shares": shares}]},
        "warming_zone": {"cross_table": [{"symbol": s, "is_warming": True} for s in warming]},
    }


def portfolio(cash, holdings):
    return {"available_idle_money": cash, "inventory_details": holdings}


class PureStrategyTests(unittest.TestCase):
    def test_low_buys_double_app_shares_without_deducting_holding(self):
        plan = build_strategy_plan(snapshot(), portfolio(10_000, {"0050": {"qty": 1, "avg_cost": 90}}), {"0050": quote()})
        self.assertEqual(plan["mode"], "LOW")
        buys = build_affordable_buys(plan, 10_000)
        self.assertEqual(sum(x["quantity"] for x in buys), 20)
        self.assertTrue(all(x["limit_price"] == 100 for x in buys))
        self.assertLessEqual(
            (datetime.fromisoformat(plan["valid_until"]) - datetime.fromisoformat(plan["strategy_generated_at"])).total_seconds(),
            300,
        )

    def test_low_warming_loss_in_effective_zone_waits(self):
        plan = build_strategy_plan(snapshot(warming=("0050",)), portfolio(10_000, {"0050": {"qty": 1, "avg_cost": 110}}), {"0050": quote()})
        self.assertEqual(plan["mode"], "LOW")
        self.assertEqual(plan["planned_sells"], [])

    def test_low_warming_profit_is_mandatory_sale(self):
        plan = build_strategy_plan(snapshot(warming=("0050",)), portfolio(10_000, {"0050": {"qty": 1, "avg_cost": 90}}), {"0050": quote()})
        self.assertEqual(plan["planned_sells"][0]["reason_code"], "WARM_MANDATORY")
        self.assertIn("升溫且獲利", plan["planned_sells"][0]["reason"])

    def test_low_offsets_p1_loss_only_against_warming_realized_profit(self):
        plan = build_strategy_plan(snapshot(warming=("0050",)), portfolio(100_000, {
            "0050": {"qty": 100, "avg_cost": 50},
            "9999": {"qty": 100, "avg_cost": 200},
        }), {"0050": quote(), "9999": quote()})
        offset = next(leg for leg in plan["planned_sells"] if leg["symbol"] == "9999")
        self.assertEqual(plan["mode"], "LOW")
        self.assertEqual(offset["quantity"], 50)
        self.assertEqual(offset["reason_code"], "LOW_LOSS_OFFSET")
        self.assertEqual(plan["rotation_multiplier"], 0)

    def test_low_loss_offsets_at_most_two_and_worst_return_first(self):
        data = snapshot(eligible=("0050",), warming=("0050",))
        plan = build_strategy_plan(data, portfolio(300_000, {
            "0050": {"qty": 1000, "avg_cost": 50},
            "1111": {"qty": 100, "avg_cost": 120},
            "2222": {"qty": 100, "avg_cost": 130},
            "3333": {"qty": 100, "avg_cost": 140},
        }), {
            "0050": quote(), "1111": quote(), "2222": quote(), "3333": quote(),
        })
        offsets = [leg for leg in plan["planned_sells"] if leg.get("reason_code") == "LOW_LOSS_OFFSET"]
        self.assertLessEqual(len({leg["symbol"] for leg in offsets}), 2)
        self.assertEqual([leg["symbol"] for leg in offsets], ["3333", "2222"])

    def test_high_warming_loss_is_mandatory_not_ranked(self):
        plan = build_strategy_plan(snapshot(warming=("0050",)), portfolio(0, {"0050": {"qty": 1000, "avg_cost": 110}}), {"0050": quote()})
        self.assertEqual(plan["mode"], "HIGH")
        self.assertEqual(plan["planned_sells"][0]["reason_code"], "WARM_MANDATORY")
        self.assertEqual(plan["rotation_candidates"], [])

    def test_p1_is_return_at_or_below_minus_five(self):
        plan = build_strategy_plan(snapshot(), portfolio(0, {"9999": {"qty": 1000, "avg_cost": 106}}), {"0050": quote(), "9999": quote()})
        self.assertEqual(plan["rotation_candidates"][0]["priority"], "P1")

    def test_return_priority_uses_executable_bid_not_reference_price(self):
        plan = build_strategy_plan(snapshot(), portfolio(0, {
            "9999": {"qty": 999, "avg_cost": 100},
        }), {"0050": quote(), "9999": quote(price=95.1, odd_bid=94)})
        self.assertEqual(plan["rotation_candidates"][0]["priority"], "P1")

    def test_p2_sorts_other_losses_by_larger_unrealized_loss(self):
        plan = build_strategy_plan(snapshot(), portfolio(0, {
            "1111": {"qty": 1000, "avg_cost": 104},
            "2222": {"qty": 2000, "avg_cost": 103},
        }), {"0050": quote(), "1111": quote(), "2222": quote()})
        self.assertEqual([x["symbol"] for x in plan["rotation_candidates"]][:2], ["2222", "1111"])
        self.assertEqual(plan["rotation_candidates"][0]["priority"], "P2")

    def test_p3_sells_positive_outside_effective_zone_without_premium_gate(self):
        plan = build_strategy_plan(snapshot(), portfolio(0, {"9999": {"qty": 1000, "avg_cost": 90}}), {"0050": quote(), "9999": quote()})
        self.assertEqual(plan["rotation_candidates"][0]["priority"], "P3")

    def test_p4_protects_only_discounted_odd_lot_leg(self):
        plan = build_strategy_plan(snapshot(), portfolio(0, {"0050": {"qty": 1500, "avg_cost": 90}}), {
            "0050": quote(board_bid=101, odd_bid=99),})
        sells = plan["planned_sells"]
        self.assertEqual([(x["quantity"], x["market"]) for x in sells], [(1000, "整股")])
        self.assertEqual(plan["rotation_candidates"][0]["priority"], "P4")

    def test_under_one_thousand_uses_odd_lot_only(self):
        plan = build_strategy_plan(snapshot(), portfolio(0, {"9999": {"qty": 999, "avg_cost": 90}}), {"0050": quote(), "9999": quote()})
        self.assertEqual(plan["planned_sells"][0]["market"], "零股")

    def test_sell_prefers_odd_lot_when_odd_bid_is_higher(self):
        plan = build_strategy_plan(
            snapshot(shares=10),
            portfolio(0, {"9999": {"qty": 1500, "avg_cost": 90}}),
            {
                "0050": quote(),
                "9999": quote(board_bid=100, odd_bid=101),
            },
        )
        sells = [leg for leg in plan["planned_sells"] if leg["symbol"] == "9999"]
        self.assertTrue(sells)
        self.assertTrue(all(leg["market"] == "零股" for leg in sells))
        self.assertTrue(all(leg["limit_price"] == 101 for leg in sells))

    def test_buy_prefers_odd_lot_when_odd_ask_is_lower(self):
        plan = build_strategy_plan(
            snapshot(shares=600),
            portfolio(200_000, {}),
            {
                "0050": quote(board_ask=101, odd_ask=99),
            },
        )
        buys = build_affordable_buys(plan, 200_000)
        self.assertEqual(sum(leg["quantity"] for leg in buys), 1200)
        self.assertTrue(all(leg["market"] == "零股" for leg in buys))
        self.assertTrue(all(leg["limit_price"] == 100 for leg in buys))

    def test_n_uses_executable_bid_proceeds_and_is_capped_at_five(self):
        plan = build_strategy_plan(snapshot(shares=10), portfolio(0, {"9999": {"qty": 10_000, "avg_cost": 90}}), {"0050": quote(), "9999": quote(board_bid=100)})
        self.assertEqual(plan["x_amount"], 1000)
        self.assertEqual(plan["rotation_multiplier"], 5)

    def test_high_buy_does_not_wait_for_sell_fill_and_uses_nav(self):
        plan = build_strategy_plan(snapshot(shares=10), portfolio(0, {"9999": {"qty": 1000, "avg_cost": 90}}), {"0050": quote(), "9999": quote()})
        self.assertTrue(plan["planned_sells"])
        buys = build_affordable_buys(plan, 0, plan["planned_sell_amount"])
        self.assertTrue(buys)
        self.assertEqual(plan["trade_plan"][0]["side"], "SELL")
        self.assertTrue(all(x["limit_price"] == 100 for x in buys))

    def test_high_n_buys_scale_only_the_n_part_by_submitted_sell_amount(self):
        plan = build_strategy_plan(snapshot(shares=10), portfolio(2_000, {
            "9999": {"qty": 2000, "avg_cost": 90},
        }), {"0050": quote(), "9999": quote()})
        self.assertEqual(plan["rotation_multiplier"], 5)
        buys = build_affordable_buys(plan, 2_000, plan["rotation_target_amount"] / 2)
        self.assertEqual(sum(x["quantity"] for x in buys), 35)  # 10 + floor(10*5*0.5)

    def test_low_cash_shortage_prefers_largest_actual_ask_discount_then_partial(self):
        plan = build_strategy_plan(snapshot(eligible=("0050", "006208"), shares=10), portfolio(1_500, {}), {
            "0050": quote(board_ask=99, odd_ask=99),
            "006208": quote(board_ask=101, odd_ask=101),
        })
        buys = build_affordable_buys(plan, 1_500)
        self.assertEqual(sum(x["quantity"] for x in buys if x["symbol"] == "0050"), 15)
        self.assertFalse(any(x["symbol"] == "006208" for x in buys))

    def test_high_final_candidate_is_partial_not_the_full_holding(self):
        plan = build_strategy_plan(snapshot(shares=10), portfolio(0, {
            "9999": {"qty": 2_000, "avg_cost": 90},
        }), {"0050": quote(), "9999": quote()})
        self.assertEqual(plan["rotation_multiplier"], 5)
        # N=5 means only 50 shares / 5,000 元 are needed, not the whole 2,000-share position.
        self.assertLess(sum(x["quantity"] for x in plan["planned_sells"]), 2_000)

    def test_execution_orders_are_serialized_only_from_the_shared_pure_plan(self):
        plan = build_strategy_plan(snapshot(), portfolio(10_000, {"0050": {"qty": 1, "avg_cost": 90}}), {"0050": quote()})
        orders = build_submission_orders(plan, 10_000)
        self.assertEqual(sum(order["qty"] for order in orders if order["side"] == "BUY"), 20)
        self.assertTrue(all(order["limit_price"] == 100 for order in orders if order["side"] == "BUY"))

    def test_midday_spiral_checks_only_existing_inventory_not_normal_rotation(self):
        data = snapshot(eligible=("0050", "006208"), shares=10)
        data["app_adjustments"] = {"stocks": [{"symbol": "0050", "app_reduce_min_shares": 20}]}
        result = build_midday_spiral_plan(data, portfolio(0, {
            "0050": {"qty": 100, "avg_cost": 90},
            "006208": {"qty": 100, "avg_cost": 110},
        }), {
            "0050": quote(board_bid=102, odd_bid=102, board_ask=103, odd_ask=103),
            "006208": quote(board_bid=99, odd_bid=99, board_ask=99, odd_ask=99),
        }, now=TEST_NOW)
        self.assertEqual(result["planned_sells"], [])
        self.assertEqual(result["spiral_plan"]["seller_symbol"], "0050")
        self.assertEqual(result["spiral_plan"]["buyer_symbol"], "006208")

    def test_spiral_plan_builds_sell_and_buy_legs(self):
        data = snapshot(eligible=("0050", "006208"), shares=10)
        data["app_adjustments"] = {"stocks": [{"symbol": "0050", "app_reduce_min_shares": 20}]}
        plan = build_strategy_plan(data, portfolio(100_000, {
            "0050": {"qty": 100, "avg_cost": 90},
        }), {
            "0050": quote(board_bid=102, board_ask=103, odd_bid=102, odd_ask=103),
            "006208": quote(board_bid=99, board_ask=99, odd_bid=99, odd_ask=99),
        })
        spiral = plan["spiral_plan"]
        self.assertEqual(sum(x["quantity"] for x in spiral["seller_legs"]), 20)
        self.assertTrue(all(x["reason_code"] == "SPIRAL_SELL" for x in spiral["seller_legs"]))
        self.assertEqual(
            sum(x["quantity"] for x in spiral["buyer_legs"]),
            spiral["buyer_max_qty"],
        )
        self.assertTrue(all(x["reason_code"] == "SPIRAL_BUY" for x in spiral["buyer_legs"]))
        self.assertTrue(all(x["limit_price"] == spiral["buyer_nav"] for x in spiral["buyer_legs"]))

    def test_spiral_buyer_does_not_need_existing_holding(self):
        data = snapshot(eligible=("0050", "006208"), shares=10)
        data["app_adjustments"] = {"stocks": [{"symbol": "0050", "app_reduce_min_shares": 20}]}
        plan = build_strategy_plan(data, portfolio(100_000, {
            "0050": {"qty": 100, "avg_cost": 90},
        }), {
            "0050": quote(board_bid=102, board_ask=103, odd_bid=102, odd_ask=103),
            "006208": quote(board_bid=99, board_ask=99, odd_bid=99, odd_ask=99),
        })
        spiral = plan["spiral_plan"]
        self.assertIsNotNone(spiral)
        self.assertEqual(spiral["seller_symbol"], "0050")
        self.assertEqual(spiral["buyer_symbol"], "006208")

    def test_spiral_buyer_must_be_in_eligible_value_zone(self):
        data = snapshot(eligible=("0050",), shares=10)
        data["raw_value_zone"]["stocks"].append({"symbol": "9999", "nav": 100, "app_shares": 10})
        data["app_adjustments"] = {"stocks": [{"symbol": "0050", "app_reduce_min_shares": 20}]}
        plan = build_strategy_plan(data, portfolio(100_000, {
            "0050": {"qty": 100, "avg_cost": 90},
            "9999": {"qty": 100, "avg_cost": 110},
        }), {
            "0050": quote(board_bid=102, board_ask=103, odd_bid=102, odd_ask=103),
            "9999": quote(board_bid=95, board_ask=95, odd_bid=95, odd_ask=95),
        })
        self.assertIsNone(plan["spiral_plan"])

    def test_high_planned_sells_keep_p1_p2_p3_p4_order(self):
        data = snapshot(eligible=("0050",), shares=100)
        plan = build_strategy_plan(data, portfolio(0, {
            "1111": {"qty": 60, "avg_cost": 120},
            "2222": {"qty": 50, "avg_cost": 103},
            "3333": {"qty": 50, "avg_cost": 90},
            "0050": {"qty": 100, "avg_cost": 90},
        }), {
            "1111": quote(), "2222": quote(), "3333": quote(), "0050": quote(),
        })
        self.assertEqual(plan["rotation_multiplier"], 2)
        self.assertEqual(
            [leg["reason_code"] for leg in plan["planned_sells"]],
            ["P1", "P2", "P3", "P4"],
        )
        self.assertEqual(sum(leg["estimated_amount"] for leg in plan["planned_sells"]), 20_000)

    def test_p1_sorts_by_larger_unrealized_loss_amount(self):
        plan = build_strategy_plan(snapshot(), portfolio(0, {
            "1111": {"qty": 1000, "avg_cost": 120},
            "2222": {"qty": 3000, "avg_cost": 110},
        }), {
            "0050": quote(), "1111": quote(), "2222": quote(),
        })
        rows = [row for row in plan["rotation_candidates"] if row["priority"] == "P1"]
        self.assertEqual([row["symbol"] for row in rows], ["2222", "1111"])

    def test_p3_sorts_by_larger_positive_unrealized_amount(self):
        plan = build_strategy_plan(snapshot(), portfolio(0, {
            "1111": {"qty": 1000, "avg_cost": 90},
            "2222": {"qty": 3000, "avg_cost": 95},
        }), {
            "0050": quote(), "1111": quote(), "2222": quote(),
        })
        rows = [row for row in plan["rotation_candidates"] if row["priority"] == "P3"]
        self.assertEqual([row["symbol"] for row in rows], ["2222", "1111"])

    def test_p4_sorts_by_larger_positive_unrealized_amount(self):
        data = snapshot(eligible=("0050", "006208"), shares=1)
        plan = build_strategy_plan(data, portfolio(0, {
            "0050": {"qty": 1000, "avg_cost": 90},
            "006208": {"qty": 2000, "avg_cost": 90},
        }), {
            "0050": quote(board_bid=101, odd_bid=101),
            "006208": quote(board_bid=101, odd_bid=101),
        })
        rows = [row for row in plan["rotation_candidates"] if row["priority"] == "P4"]
        self.assertEqual([row["symbol"] for row in rows], ["006208", "0050"])

    def test_warming_mandatory_sale_is_not_cut_down_by_five_x_target(self):
        data = snapshot(eligible=("0050",), shares=10, warming=("0050",))
        plan = build_strategy_plan(data, portfolio(0, {
            "0050": {"qty": 100, "avg_cost": 90},
        }), {
            "0050": quote(),
        })
        self.assertEqual(plan["rotation_multiplier"], 5)
        self.assertEqual(plan["rotation_target_amount"], 5_000)
        self.assertEqual(sum(leg["quantity"] for leg in plan["planned_sells"]), 100)
        self.assertEqual(sum(leg["estimated_amount"] for leg in plan["planned_sells"]), 10_000)
        self.assertTrue(all(leg["reason_code"] == "WARM_MANDATORY" for leg in plan["planned_sells"]))

    def test_spiral_gap_exactly_half_percent_does_not_trade(self):
        data = snapshot(eligible=("0050", "006208"), shares=10)
        data["app_adjustments"] = {"stocks": [{"symbol": "0050", "app_reduce_min_shares": 20}]}
        plan = build_strategy_plan(data, portfolio(100_000, {
            "0050": {"qty": 100, "avg_cost": 90},
        }), {
            "0050": quote(board_bid=100.5, odd_bid=100.5, board_ask=101, odd_ask=101),
            "006208": quote(board_bid=100, odd_bid=100, board_ask=100, odd_ask=100),
        })
        self.assertIsNone(plan["spiral_plan"])

    def test_spiral_gap_just_over_half_percent_can_trade(self):
        data = snapshot(eligible=("0050", "006208"), shares=10)
        data["app_adjustments"] = {"stocks": [{"symbol": "0050", "app_reduce_min_shares": 20}]}
        plan = build_strategy_plan(data, portfolio(100_000, {
            "0050": {"qty": 100, "avg_cost": 90},
        }), {
            "0050": quote(board_bid=100.5001, odd_bid=100.5001, board_ask=101, odd_ask=101),
            "006208": quote(board_bid=100, odd_bid=100, board_ask=100, odd_ask=100),
        })
        self.assertIsNotNone(plan["spiral_plan"])
        self.assertGreater(plan["spiral_plan"]["premium_gap_pct"], 0.5)

    def test_zero_app_shares_creates_no_buy_order(self):
        data = snapshot(eligible=("0050",), shares=0)
        plan = build_strategy_plan(data, portfolio(10_000, {}), {"0050": quote()})
        self.assertEqual(plan["x_amount"], 0)
        self.assertEqual(build_affordable_buys(plan, 10_000), [])

    def test_waterline_exactly_fifty_is_low(self):
        plan = build_strategy_plan(
            snapshot(),
            portfolio(10_000, {"9999": {"qty": 100, "avg_cost": 100}}),
            {"0050": quote(), "9999": quote()},
        )
        self.assertEqual(plan["actual_waterline_pct"], 50.0)
        self.assertEqual(plan["mode"], "LOW")

    def test_waterline_just_above_fifty_is_high(self):
        plan = build_strategy_plan(
            snapshot(),
            portfolio(9_999, {"9999": {"qty": 100, "avg_cost": 100}}),
            {"0050": quote(), "9999": quote()},
        )
        self.assertGreater(plan["actual_waterline_pct"], 50.0)
        self.assertEqual(plan["mode"], "HIGH")

    def test_minus_five_percent_boundary_is_p1(self):
        plan = build_strategy_plan(
            snapshot(),
            portfolio(0, {"9999": {"qty": 1000, "avg_cost": 100}}),
            {"0050": quote(), "9999": quote(price=95, board_bid=95, odd_bid=95)},
        )
        self.assertEqual(plan["rotation_candidates"][0]["priority"], "P1")

    def test_just_better_than_minus_five_percent_is_p2(self):
        plan = build_strategy_plan(
            snapshot(),
            portfolio(0, {"9999": {"qty": 1000, "avg_cost": 100}}),
            {"0050": quote(), "9999": quote(price=95.0001, board_bid=95.0001, odd_bid=95.0001)},
        )
        self.assertEqual(plan["rotation_candidates"][0]["priority"], "P2")

    def test_p4_price_exactly_nav_is_allowed(self):
        plan = build_strategy_plan(
            snapshot(),
            portfolio(0, {"0050": {"qty": 1000, "avg_cost": 90}}),
            {"0050": quote(board_bid=100, odd_bid=100)},
        )
        self.assertTrue(plan["planned_sells"])
        self.assertTrue(all(leg["limit_price"] >= 100 for leg in plan["planned_sells"]))

    def test_p4_price_below_nav_is_blocked(self):
        plan = build_strategy_plan(
            snapshot(),
            portfolio(0, {"0050": {"qty": 999, "avg_cost": 90}}),
            {"0050": quote(board_bid=99.9999, odd_bid=99.9999)},
        )
        self.assertEqual(plan["planned_sells"], [])

    def test_share_boundary_999_1000_1001_uses_legal_markets(self):
        for qty in (999, 1000, 1001):
            with self.subTest(qty=qty):
                plan = build_strategy_plan(
                    snapshot(),
                    portfolio(0, {"9999": {"qty": qty, "avg_cost": 90}}),
                    {"0050": quote(), "9999": quote(board_bid=100, odd_bid=99)},
                )
                legs = plan["planned_sells"]
                self.assertEqual(sum(leg["quantity"] for leg in legs), min(qty, sum(leg["quantity"] for leg in legs)))
                self.assertTrue(all(0 < leg["quantity"] <= qty for leg in legs))
                self.assertTrue(all(leg["market"] in ("整股", "零股") for leg in legs))

    def test_planned_sell_never_exceeds_original_holding(self):
        plan = build_strategy_plan(
            snapshot(shares=50),
            portfolio(0, {"9999": {"qty": 1234, "avg_cost": 90}}),
            {"0050": quote(), "9999": quote()},
        )
        sold = sum(leg["quantity"] for leg in plan["planned_sells"] if leg["symbol"] == "9999")
        self.assertLessEqual(sold, 1234)

    def test_randomized_strategy_invariants(self):
        rng = random.Random(20260923)
        for case in range(1000):
            eligible = ("0050", "006208", "00878")
            rows = []
            quotes = {}
            holdings = {}
            for index, symbol in enumerate(eligible):
                nav = rng.uniform(30, 150)
                shares = rng.randint(0, 800)
                rows.append({"symbol": symbol, "nav": nav, "app_shares": shares})
                bid = nav * rng.uniform(0.97, 1.03)
                ask = nav * rng.uniform(0.97, 1.03)
                odd_bid = bid * rng.uniform(0.995, 1.005)
                odd_ask = ask * rng.uniform(0.995, 1.005)
                quotes[symbol] = quote(price=nav, board_bid=bid, board_ask=ask, odd_bid=odd_bid, odd_ask=odd_ask)
                if rng.random() < 0.7:
                    qty = rng.randint(1, 3000)
                    avg = nav * rng.uniform(0.7, 1.3)
                    holdings[symbol] = {"qty": qty, "avg_cost": avg}

            for symbol in ("1111", "2222", "3333"):
                if rng.random() < 0.6:
                    nav = rng.uniform(30, 150)
                    qty = rng.randint(1, 3000)
                    avg = nav * rng.uniform(0.7, 1.3)
                    holdings[symbol] = {"qty": qty, "avg_cost": avg}
                    bid = nav * rng.uniform(0.97, 1.03)
                    ask = nav * rng.uniform(0.97, 1.03)
                    quotes[symbol] = quote(
                        price=nav, board_bid=bid, board_ask=ask,
                        odd_bid=bid * rng.uniform(0.995, 1.005),
                        odd_ask=ask * rng.uniform(0.995, 1.005),
                    )

            data = {
                "snapshot_id": f"random-{case}",
                "account": "fixture@example.invalid",
                "eligible_value_zone": {"stocks": rows},
                "raw_value_zone": {"stocks": rows},
                "warming_zone": {"cross_table": []},
            }
            cash = rng.uniform(0, 500_000)
            plan = build_strategy_plan(data, portfolio(cash, holdings), quotes)

            self.assertIn(plan["mode"], ("LOW", "HIGH"))
            self.assertGreaterEqual(plan["rotation_multiplier"], 0)
            self.assertLessEqual(plan["rotation_multiplier"], 5)
            if plan["mode"] == "LOW":
                self.assertEqual(plan["rotation_multiplier"], 0)

            original_qty = {symbol: row["qty"] for symbol, row in holdings.items()}
            sold_by_symbol = {}
            for leg in plan["planned_sells"]:
                self.assertGreater(leg["quantity"], 0)
                sold_by_symbol[leg["symbol"]] = sold_by_symbol.get(leg["symbol"], 0) + leg["quantity"]
                if leg.get("reason_code") == "P4":
                    nav = next(row["nav"] for row in rows if row["symbol"] == leg["symbol"])
                    self.assertGreaterEqual(leg["limit_price"], nav)
            for symbol, sold in sold_by_symbol.items():
                self.assertLessEqual(sold, original_qty[symbol])

            spiral = plan.get("spiral_plan")
            if spiral:
                self.assertNotEqual(spiral["seller_symbol"], spiral["buyer_symbol"])
                self.assertIn(spiral["buyer_symbol"], eligible)
                self.assertGreater(spiral["premium_gap_pct"], 0.5)
                self.assertLessEqual(spiral["seller_planned_qty"], original_qty[spiral["seller_symbol"]])

    def test_ark_real_shape_100_shadow_cases(self):
        """以兩次真實 ARK snapshot 的策略欄位形狀為基底，隨機化私人庫存後跑 100 組。"""
        baselines = [
            {
                "eligible": [
                    ("0053", 254.15, 2), ("0052", 65.88, 9), ("00631L", 39.43, 15),
                    ("0050", 113.11, 5), ("0055", 49.86, 13), ("0056", 56.71, 10),
                    ("00911", 57.32, 10), ("00960", 22.24, 31), ("00875", 57.74, 9),
                ],
                "excluded": [("0057", 336.49, 0)],
                "warming": {"00960"},
            },
            {
                "eligible": [
                    ("0053", 251.03, 0), ("0052", 64.91, 0), ("00631L", 38.58, 0),
                    ("0050", 111.83, 0), ("006208", 255.90, 0), ("006201", 46.87, 0),
                    ("0055", 50.21, 0), ("00911", 56.01, 0), ("0056", 56.70, 0),
                    ("00875", 57.89, 0), ("00960", 22.69, 1),
                ],
                "excluded": [("0057", 332.44, 0), ("006203", 202.82, 0)],
                "warming": {"00960"},
            },
        ]
        rng = random.Random(35805463471 ^ 35706480249)

        for case in range(100):
            base = baselines[case % 2]
            eligible_rows = [
                {"symbol": symbol, "nav": nav, "app_shares": shares}
                for symbol, nav, shares in base["eligible"]
            ]
            excluded_rows = [
                {"symbol": symbol, "nav": nav, "app_shares": shares}
                for symbol, nav, shares in base["excluded"]
            ]
            all_zone_rows = eligible_rows + excluded_rows
            nav_by_symbol = {row["symbol"]: row["nav"] for row in all_zone_rows}

            holdings = {}
            quotes = {}
            adjustment_rows = []
            universe = [row["symbol"] for row in all_zone_rows] + ["2308", "2330", "00830", "00861"]

            for symbol in universe:
                nav = nav_by_symbol.get(symbol, rng.uniform(25, 250))
                board_bid = nav * rng.uniform(0.97, 1.03)
                board_ask = nav * rng.uniform(0.97, 1.03)
                quotes[symbol] = quote(
                    price=nav * rng.uniform(0.99, 1.01),
                    board_bid=board_bid,
                    board_ask=board_ask,
                    odd_bid=board_bid * rng.uniform(0.995, 1.005),
                    odd_ask=board_ask * rng.uniform(0.995, 1.005),
                )
                if rng.random() < 0.65:
                    qty = rng.randint(1, 3500)
                    avg_cost = nav * rng.uniform(0.70, 1.35)
                    holdings[symbol] = {"qty": qty, "avg_cost": avg_cost}
                    if symbol in nav_by_symbol and rng.random() < 0.7:
                        adjustment_rows.append({
                            "symbol": symbol,
                            "app_reduce_min_shares": rng.randint(0, qty),
                        })

            data = {
                "snapshot_id": f"ark-real-shape-{case}",
                "account": "sanitized-fixture@example.invalid",
                "eligible_value_zone": {"stocks": eligible_rows},
                "raw_value_zone": {"stocks": all_zone_rows},
                "warming_zone": {
                    "cross_table": [
                        {"symbol": symbol, "is_warming": True}
                        for symbol in base["warming"]
                    ]
                },
                "app_adjustments": {"stocks": adjustment_rows},
            }
            cash = rng.uniform(0, 900_000)
            plan = build_strategy_plan(data, portfolio(cash, holdings), quotes)

            self.assertIn(plan["mode"], ("LOW", "HIGH"))
            self.assertGreaterEqual(plan["rotation_multiplier"], 0)
            self.assertLessEqual(plan["rotation_multiplier"], 5)
            if plan["mode"] == "LOW":
                self.assertEqual(plan["rotation_multiplier"], 0)

            sold = {}
            for leg in plan["planned_sells"]:
                sold[leg["symbol"]] = sold.get(leg["symbol"], 0) + leg["quantity"]
                self.assertGreater(leg["quantity"], 0)
                self.assertIn(leg["market"], ("整股", "零股"))
                if leg.get("reason_code") == "P4":
                    self.assertGreaterEqual(leg["limit_price"], nav_by_symbol[leg["symbol"]])
            for symbol, qty in sold.items():
                self.assertLessEqual(qty, holdings[symbol]["qty"])

            buys = build_affordable_buys(
                plan,
                cash,
                plan["rotation_target_amount"] * rng.random() if plan["mode"] == "HIGH" else 0,
            )
            for leg in buys:
                self.assertIn(leg["symbol"], {row["symbol"] for row in eligible_rows})
                self.assertGreater(leg["quantity"], 0)
                self.assertEqual(leg["limit_price"], nav_by_symbol[leg["symbol"]])

            spiral = plan.get("spiral_plan")
            if spiral:
                self.assertNotEqual(spiral["seller_symbol"], spiral["buyer_symbol"])
                self.assertIn(spiral["buyer_symbol"], {row["symbol"] for row in eligible_rows})
                self.assertGreater(spiral["premium_gap_pct"], 0.5)
                self.assertLessEqual(
                    spiral["seller_planned_qty"],
                    holdings[spiral["seller_symbol"]]["qty"] - sold.get(spiral["seller_symbol"], 0),
                )
                self.assertEqual(
                    sum(leg["quantity"] for leg in spiral["buyer_legs"]),
                    spiral["buyer_max_qty"],
                )

    def test_stress_weird_inventory_many_priority_buckets(self):
        data = snapshot(eligible=("0050", "006208", "00875"), shares=20)
        plan = build_strategy_plan(data, portfolio(0, {
            "1111": {"qty": 999, "avg_cost": 150},
            "2222": {"qty": 1001, "avg_cost": 108},
            "3333": {"qty": 1999, "avg_cost": 80},
            "0050": {"qty": 2001, "avg_cost": 90},
            "006208": {"qty": 1, "avg_cost": 80},
            "00875": {"qty": 1000, "avg_cost": 95},
        }), {
            "1111": quote(board_bid=100, odd_bid=99),
            "2222": quote(board_bid=100, odd_bid=101),
            "3333": quote(board_bid=100, odd_bid=100),
            "0050": quote(board_bid=101, odd_bid=100.5),
            "006208": quote(board_bid=102, odd_bid=102),
            "00875": quote(board_bid=99, odd_bid=101),
        })
        priorities = [row["priority"] for row in plan["rotation_candidates"]]
        self.assertEqual(priorities, sorted(priorities, key=lambda p: int(p[1])))
        sold = {}
        for leg in plan["planned_sells"]:
            sold[leg["symbol"]] = sold.get(leg["symbol"], 0) + leg["quantity"]
        for symbol, qty in sold.items():
            self.assertLessEqual(qty, plan["holdings"][[x["symbol"] for x in plan["holdings"]].index(symbol)]["qty"])

    def test_stress_p4_mostly_nav_blocked(self):
        data = snapshot(eligible=("0050", "006208", "00875"), shares=50)
        plan = build_strategy_plan(data, portfolio(0, {
            "0050": {"qty": 1500, "avg_cost": 90},
            "006208": {"qty": 1500, "avg_cost": 90},
            "00875": {"qty": 1500, "avg_cost": 90},
        }), {
            "0050": quote(board_bid=99, odd_bid=99),
            "006208": quote(board_bid=101, odd_bid=99),
            "00875": quote(board_bid=99, odd_bid=101),
        })
        self.assertTrue(all(
            leg["limit_price"] >= 100
            for leg in plan["planned_sells"]
            if leg.get("reason_code") == "P4"
        ))

    def test_stress_only_one_buyable_symbol_has_nonzero_app_shares(self):
        rows = [
            {"symbol": "0050", "nav": 100, "app_shares": 0},
            {"symbol": "006208", "nav": 100, "app_shares": 0},
            {"symbol": "00875", "nav": 100, "app_shares": 7},
        ]
        data = {
            "snapshot_id": "weird-one-buy",
            "account": "fixture@example.invalid",
            "eligible_value_zone": {"stocks": rows},
            "raw_value_zone": {"stocks": rows},
            "warming_zone": {"cross_table": []},
        }
        plan = build_strategy_plan(data, portfolio(100_000, {}), {
            "0050": quote(), "006208": quote(), "00875": quote(),
        })
        buys = build_affordable_buys(plan, 100_000)
        self.assertEqual({leg["symbol"] for leg in buys}, {"00875"})

    def test_stress_share_boundaries_across_many_holdings(self):
        quantities = [1, 998, 999, 1000, 1001, 1999, 2000, 2001]
        holdings = {}
        quotes = {"0050": quote()}
        for idx, qty in enumerate(quantities):
            symbol = f"W{idx:03d}"
            holdings[symbol] = {"qty": qty, "avg_cost": 90}
            quotes[symbol] = quote(board_bid=100 + (idx % 2), odd_bid=101 - (idx % 2))
        plan = build_strategy_plan(snapshot(shares=10), portfolio(0, holdings), quotes)
        sold = {}
        for leg in plan["planned_sells"]:
            sold[leg["symbol"]] = sold.get(leg["symbol"], 0) + leg["quantity"]
            self.assertTrue(leg["market"] in ("整股", "零股"))
        for symbol, qty in sold.items():
            self.assertLessEqual(qty, holdings[symbol]["qty"])

    def test_stress_warming_sale_exceeds_rotation_target_by_large_margin(self):
        data = snapshot(eligible=("0050",), shares=1, warming=("0050",))
        plan = build_strategy_plan(data, portfolio(0, {
            "0050": {"qty": 5000, "avg_cost": 50},
        }), {"0050": quote()})
        self.assertEqual(plan["rotation_multiplier"], 5)
        self.assertGreater(plan["planned_sell_amount"], plan["rotation_target_amount"] * 100)

    def test_stress_spiral_seller_left_with_one_share(self):
        data = snapshot(eligible=("0050", "006208"), shares=10)
        data["app_adjustments"] = {"stocks": [{"symbol": "0050", "app_reduce_min_shares": 1}]}
        plan = build_strategy_plan(data, portfolio(100_000, {
            "0050": {"qty": 1, "avg_cost": 80},
        }), {
            "0050": quote(board_bid=102, odd_bid=102, board_ask=103, odd_ask=103),
            "006208": quote(board_bid=99, odd_bid=99, board_ask=99, odd_ask=99),
        })
        spiral = plan["spiral_plan"]
        if spiral:
            self.assertEqual(spiral["seller_planned_qty"], 1)
            self.assertEqual(sum(x["quantity"] for x in spiral["seller_legs"]), 1)

    def test_stress_spiral_multiple_sellers_multiple_unheld_buyers(self):
        data = snapshot(eligible=("0050", "006208", "00875"), shares=10)
        data["app_adjustments"] = {"stocks": [
            {"symbol": "0050", "app_reduce_min_shares": 30},
            {"symbol": "006208", "app_reduce_min_shares": 30},
        ]}
        plan = build_strategy_plan(data, portfolio(100_000, {
            "0050": {"qty": 100, "avg_cost": 80},
            "006208": {"qty": 100, "avg_cost": 80},
        }), {
            "0050": quote(board_bid=103, odd_bid=103, board_ask=104, odd_ask=104),
            "006208": quote(board_bid=102, odd_bid=102, board_ask=103, odd_ask=103),
            "00875": quote(board_bid=98, odd_bid=98, board_ask=98, odd_ask=98),
        })
        spiral = plan["spiral_plan"]
        self.assertIsNotNone(spiral)
        self.assertEqual(spiral["seller_symbol"], "0050")
        self.assertEqual(spiral["buyer_symbol"], "00875")

    def test_stress_high_buy_scale_at_one_forty_nine_ninety_nine_percent(self):
        plan = build_strategy_plan(
            snapshot(shares=100),
            portfolio(0, {"9999": {"qty": 10000, "avg_cost": 80}}),
            {"0050": quote(), "9999": quote()},
        )
        target = plan["rotation_target_amount"]
        for ratio in (0.01, 0.49, 0.99):
            with self.subTest(ratio=ratio):
                buys = build_affordable_buys(plan, 0, target * ratio)
                expected = 100 + int(100 * plan["rotation_multiplier"] * ratio)
                self.assertEqual(sum(x["quantity"] for x in buys), expected)

    def test_stress_cash_tiny_relative_to_nav(self):
        plan = build_strategy_plan(
            snapshot(eligible=("0050", "006208"), shares=100),
            portfolio(1, {}),
            {"0050": quote(), "006208": quote()},
        )
        self.assertEqual(build_affordable_buys(plan, 1), [])

    def test_stress_huge_cost_dispersion_and_mixed_markets(self):
        data = snapshot(eligible=("0050", "006208"), shares=30)
        plan = build_strategy_plan(data, portfolio(0, {
            "0050": {"qty": 2500, "avg_cost": 1},
            "006208": {"qty": 2500, "avg_cost": 1000},
            "9999": {"qty": 2500, "avg_cost": 500},
        }), {
            "0050": quote(board_bid=101, odd_bid=102),
            "006208": quote(board_bid=99, odd_bid=98),
            "9999": quote(board_bid=100, odd_bid=101),
        })
        for leg in plan["planned_sells"]:
            self.assertGreater(leg["quantity"], 0)
            self.assertGreater(leg["limit_price"], 0)

    def test_spiral_uses_positive_return_highest_premium_seller_and_app_quantity(self):
        data = snapshot(eligible=("0050", "006208"), shares=10)
        data["app_adjustments"] = {"stocks": [
            {"symbol": "0050", "app_reduce_min_shares": 20},
        ]}
        plan = build_strategy_plan(data, portfolio(100_000, {
            "0050": {"qty": 100, "avg_cost": 90},
            "006208": {"qty": 100, "avg_cost": 110},
        }), {
            "0050": quote(board_bid=102, board_ask=103, odd_bid=102, odd_ask=103),
            "006208": quote(board_bid=99, board_ask=99, odd_bid=99, odd_ask=99),
        })
        spiral = plan["spiral_plan"]
        self.assertEqual(spiral["seller_symbol"], "0050")
        self.assertEqual(spiral["buyer_symbol"], "006208")
        self.assertEqual(spiral["seller_planned_qty"], 20)
        self.assertGreater(spiral["premium_gap_pct"], 0.5)


if __name__ == "__main__":
    unittest.main()

