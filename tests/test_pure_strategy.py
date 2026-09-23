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
        plan = build_strategy_plan(data, portfolio(100_000, {
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

