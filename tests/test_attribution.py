"""核验归因、待核裁决、结算封存与跨期间调整的完整口径。"""
import tempfile
import unittest
from pathlib import Path

from analytics import AnalyticsService
from analytics.attribution import (
    ATTRIBUTED, PENDING_REVIEW, UNATTRIBUTED, INVALID)


def event(event_id, kind, entity, occurred, payload, observed=None, revision=1):
    return {
        "source": "crm", "event_id": event_id, "revision": revision,
        "kind": kind, "entity": entity,
        "occurred_at": occurred, "observed_at": observed or occurred,
        "payload": payload,
    }


def touch(event_id, kind, customer, at, creator, campaign="camp-1"):
    return event(event_id, kind, customer, at,
                 {"campaign": campaign, "creator": creator})


def order(event_id, customer, at, order_id, amount, campaign="camp-1"):
    return event(event_id, "order", customer, at,
                 {"campaign": campaign, "order_id": order_id, "amount": amount})


def refund(event_id, customer, at, order_id, amount):
    return event(event_id, "refund", customer, at,
                 {"order_id": order_id, "amount": amount})


class AttributionCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = AnalyticsService(Path(self.tmp.name) / "data.sqlite3")
        self.service.register("cust-1", "顾客甲", "customer")
        self.service.register("cust-2", "顾客乙", "customer")
        for creator in ("creator-a", "creator-b", "creator-c"):
            self.service.register(creator, creator, "creator")
        self.service.link("crm", "cust-ext-1", "cust-1")
        self.service.link("crm", "cust-ext-2", "cust-2")
        self.service.register_campaign("camp-1", "10000", 7, 1)
        self.service.enroll("camp-1", "creator-a", "0.10")
        self.service.enroll("camp-1", "creator-b", "0.08")
        self.service.enroll("camp-1", "creator-c", "0.05")

    def tearDown(self):
        self.service.close()
        self.tmp.cleanup()

    def feed(self, *events):
        for item in events:
            self.service.ingest(item)

    def decisions(self, as_of=None):
        return {d["order_id"]: d
                for d in self.service.ledger.compute(as_of)["decisions"]}

    def test_last_click_wins_over_earlier_creator(self):
        self.feed(
            touch("exp-1", "exposure", "cust-ext-1", "2026-11-01T09:00:00+08:00", "creator-a"),
            touch("clk-1", "click", "cust-ext-1", "2026-11-02T10:00:00+08:00", "creator-a"),
            touch("clk-2", "click", "cust-ext-1", "2026-11-03T10:00:00+08:00", "creator-b"),
            order("ord-1", "cust-ext-1", "2026-11-04T10:00:00+08:00", "order-1", "1000.00"),
        )
        decision = self.decisions()["order-1"]
        self.assertEqual(decision["status"], ATTRIBUTED)
        self.assertEqual(decision["creator"], "creator-b")
        self.assertEqual(decision["basis"], "last_click")
        self.assertEqual(decision["payable"], "80.00")
        # 完整触达路径留痕，含窗口判定
        self.assertTrue(all("eligible" in t for t in decision["snapshot"]["path"]))

    def test_exposure_only_uses_last_exposure(self):
        self.feed(
            touch("exp-1", "exposure", "cust-ext-1", "2026-11-04T09:00:00+08:00", "creator-a"),
            touch("exp-2", "exposure", "cust-ext-1", "2026-11-04T09:30:00+08:00", "creator-c"),
            order("ord-1", "cust-ext-1", "2026-11-04T10:00:00+08:00", "order-1", "200.00"),
        )
        decision = self.decisions()["order-1"]
        self.assertEqual(decision["status"], ATTRIBUTED)
        self.assertEqual(decision["creator"], "creator-c")
        self.assertEqual(decision["basis"], "last_exposure")
        self.assertEqual(decision["payable"], "10.00")

    def test_touch_outside_window_is_unattributed(self):
        self.feed(
            touch("exp-1", "exposure", "cust-ext-1", "2026-11-01T09:00:00+08:00", "creator-a"),
            touch("clk-1", "click", "cust-ext-1", "2026-10-20T09:00:00+08:00", "creator-a"),
            order("ord-1", "cust-ext-1", "2026-11-04T10:00:00+08:00", "order-1", "200.00"),
        )
        decision = self.decisions()["order-1"]
        self.assertEqual(decision["status"], UNATTRIBUTED)
        self.assertEqual(decision["payable"], "0.00")

    def test_simultaneous_multi_creator_goes_pending_not_double_counted(self):
        at = "2026-11-06T20:00:00+08:00"
        self.feed(
            touch("clk-1", "click", "cust-ext-2", at, "creator-a"),
            touch("clk-2", "click", "cust-ext-2", at, "creator-b"),
            order("ord-2", "cust-ext-2", "2026-11-07T11:00:00+08:00", "order-2", "500.00"),
        )
        decision = self.decisions()["order-2"]
        self.assertEqual(decision["status"], PENDING_REVIEW)
        self.assertEqual(decision["reason_code"], "creator_tie")
        self.assertIsNone(decision["creator"])
        self.assertEqual(decision["payable"], "0.00")
        self.assertEqual(decision["suggested_creator"], ["creator-a", "creator-b"])
        # 待核订单不进入任何创作者的应付
        analysis = self.service.campaign_analysis()
        view = analysis["campaigns"]["camp-1"]
        self.assertEqual(view["orders"][PENDING_REVIEW], 1)
        self.assertEqual(view["amounts"]["pending_review_net"], "500.00")
        for creator_row in view["creators"].values():
            self.assertEqual(creator_row["attributed_orders"], 0)

    def test_unenrolled_winner_goes_pending(self):
        self.feed(
            touch("clk-1", "click", "cust-ext-1", "2026-11-02T10:00:00+08:00", "creator-x"),
            order("ord-1", "cust-ext-1", "2026-11-04T10:00:00+08:00", "order-1", "200.00"),
        )
        decision = self.decisions()["order-1"]
        self.assertEqual(decision["status"], PENDING_REVIEW)
        self.assertEqual(decision["reason_code"], "creator_not_enrolled")

    def test_manual_resolution_overrides_and_persists_through_recompute(self):
        at = "2026-11-06T20:00:00+08:00"
        self.feed(
            touch("clk-1", "click", "cust-ext-2", at, "creator-a"),
            touch("clk-2", "click", "cust-ext-2", at, "creator-b"),
            order("ord-2", "cust-ext-2", "2026-11-07T11:00:00+08:00", "order-2", "500.00"),
        )
        self.service.recompute_attribution("2026-11-08T00:00:00+00:00")
        self.service.resolve_review(
            "order-2", ATTRIBUTED, "creator-a", "直播为决定性触点",
            "2026-11-09T00:00:00+00:00")
        decision = self.decisions()["order-2"]
        self.assertEqual(decision["status"], ATTRIBUTED)
        self.assertEqual(decision["creator"], "creator-a")
        self.assertEqual(decision["basis"], "manual_review")
        self.assertEqual(decision["payable"], "50.00")
        # 再次重算（例如新事件到达）仍保持人工结论
        self.service.recompute_attribution("2026-11-10T00:00:00+00:00")
        self.assertEqual(self.decisions()["order-2"]["creator"], "creator-a")
        # 裁决作出之前的历史时点仍呈现待核
        historical = self.decisions("2026-11-08T12:00:00+00:00")["order-2"]
        self.assertEqual(historical["status"], PENDING_REVIEW)
        # 不能把订单裁决给未参加活动的创作者
        with self.assertRaises(ValueError):
            self.service.resolve_review("order-2", ATTRIBUTED, "creator-x", "x",
                                        "2026-11-09T01:00:00+00:00")

    def test_refund_before_settlement_reduces_payable(self):
        self.feed(
            touch("clk-1", "click", "cust-ext-1", "2026-11-02T10:00:00+08:00", "creator-a"),
            order("ord-1", "cust-ext-1", "2026-11-04T10:00:00+08:00", "order-1", "1000.00"),
            refund("ref-1", "cust-ext-1", "2026-11-05T10:00:00+08:00", "order-1", "400.00"),
        )
        decision = self.decisions()["order-1"]
        self.assertEqual(decision["gross"], "1000.00")
        self.assertEqual(decision["refunded"], "400.00")
        self.assertEqual(decision["net"], "600.00")
        self.assertEqual(decision["payable"], "60.00")

    def test_partial_refunds_accumulate_and_net_floors_at_zero(self):
        self.feed(
            touch("clk-1", "click", "cust-ext-1", "2026-11-02T10:00:00+08:00", "creator-a"),
            order("ord-1", "cust-ext-1", "2026-11-04T10:00:00+08:00", "order-1", "1000.00"),
            refund("ref-1", "cust-ext-1", "2026-11-05T10:00:00+08:00", "order-1", "600.00"),
            refund("ref-2", "cust-ext-1", "2026-11-06T10:00:00+08:00", "order-1", "700.00"),
        )
        decision = self.decisions()["order-1"]
        self.assertEqual(decision["refunded"], "1300.00")
        self.assertEqual(decision["net"], "0.00")
        self.assertEqual(decision["payable"], "0.00")

    def test_duplicate_order_events_go_pending(self):
        self.feed(
            touch("clk-1", "click", "cust-ext-1", "2026-11-02T10:00:00+08:00", "creator-a"),
            order("ord-1", "cust-ext-1", "2026-11-04T10:00:00+08:00", "order-1", "200.00"),
            order("ord-1b", "cust-ext-1", "2026-11-04T11:00:00+08:00", "order-1", "200.00"),
        )
        decision = self.decisions()["order-1"]
        self.assertEqual(decision["status"], PENDING_REVIEW)
        self.assertEqual(decision["reason_code"], "duplicate_order")

    def test_invalid_and_unknown_campaign_orders_are_flagged(self):
        self.feed(
            order("ord-1", "cust-ext-1", "2026-11-04T10:00:00+08:00", "order-1", "bad"),
            order("ord-2", "cust-ext-1", "2026-11-04T10:00:00+08:00", "order-2", "100.00",
                  campaign="camp-ghost"),
        )
        decisions = self.decisions()
        self.assertEqual(decisions["order-1"]["status"], INVALID)
        self.assertEqual(decisions["order-2"]["status"], PENDING_REVIEW)
        self.assertEqual(decisions["order-2"]["reason_code"], "unknown_campaign")

    def test_orphan_refund_is_issue_not_crash(self):
        self.feed(refund("ref-1", "cust-ext-1", "2026-11-05T10:00:00+08:00", "ghost", "10.00"))
        result = self.service.ledger.compute()
        self.assertTrue(any(i.get("order_id") == "ghost" for i in result["issues"]))
        self.assertEqual(result["decisions"], [])


class SettlementCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = AnalyticsService(Path(self.tmp.name) / "data.sqlite3")
        self.service.register("cust-1", "顾客甲", "customer")
        self.service.register("cust-2", "顾客乙", "customer")
        for creator in ("creator-a", "creator-b"):
            self.service.register(creator, creator, "creator")
        self.service.link("crm", "cust-ext-1", "cust-1")
        self.service.link("crm", "cust-ext-2", "cust-2")
        self.service.register_campaign("camp-1", "10000", 7, 1)
        self.service.enroll("camp-1", "creator-a", "0.10")
        self.service.enroll("camp-1", "creator-b", "0.08")

    def tearDown(self):
        self.service.close()
        self.tmp.cleanup()

    def feed(self, *events):
        for item in events:
            self.service.ingest(item)

    def test_seal_freezes_evidence_and_pending_is_excluded(self):
        at = "2026-11-06T20:00:00+08:00"
        self.feed(
            touch("clk-1", "click", "cust-ext-1", "2026-11-02T10:00:00+08:00", "creator-a"),
            order("ord-1", "cust-ext-1", "2026-11-04T10:00:00+08:00", "order-1", "1000.00"),
            touch("clk-2", "click", "cust-ext-2", at, "creator-a"),
            touch("clk-3", "click", "cust-ext-2", at, "creator-b"),
            order("ord-2", "cust-ext-2", "2026-11-07T11:00:00+08:00", "order-2", "500.00"),
        )
        sealed = self.service.seal_settlement(
            "st-1", "2026-11", "2026-11-30T16:00:00+00:00", "2026-12-01T10:00:00+00:00")
        # 仅已归因订单进入，待核订单被隔离
        kinds = {(line["order_id"], line["kind"]) for line in sealed["lines"]}
        self.assertEqual(kinds, {("order-1", "settlement")})
        self.assertEqual(sealed["campaign_totals"]["camp-1"]["payable_after"], "100.00")
        # 证据快照内保留完整触达路径
        evidence = sealed["lines"][0]["evidence"]
        self.assertTrue(evidence["path"])
        # 哈希可回查，且内容不可变
        self.assertTrue(self.service.verify_settlement("st-1")["intact"])
        with self.assertRaises(ValueError):
            self.service.seal_settlement(
                "st-again", "2026-11", "2026-12-02T16:00:00+00:00",
                "2026-12-03T10:00:00+00:00")

    def test_post_seal_refund_creates_next_period_adjustment(self):
        self.feed(
            touch("clk-1", "click", "cust-ext-1", "2026-11-02T10:00:00+08:00", "creator-a"),
            order("ord-1", "cust-ext-1", "2026-11-04T10:00:00+08:00", "order-1", "1000.00"),
        )
        self.service.seal_settlement(
            "st-1", "2026-11", "2026-11-30T16:00:00+00:00", "2026-12-01T10:00:00+00:00")
        # 封存后退款 400，旧单不动，新期间产生负调整
        self.feed(refund("ref-1", "cust-ext-1", "2026-12-10T10:00:00+08:00",
                         "order-1", "400.00"))
        second = self.service.seal_settlement(
            "st-2", "2026-12", "2026-12-31T16:00:00+00:00", "2027-01-05T10:00:00+00:00")
        self.assertEqual(len(second["lines"]), 1)
        line = second["lines"][0]
        self.assertEqual(line["kind"], "refund_adjustment")
        self.assertEqual(line["payable_before"], "100.00")
        self.assertEqual(line["payable_after"], "60.00")
        self.assertEqual(line["delta"], "-40.00")
        # 旧结算单原样保留，仍可验证
        first = self.service.get_settlement("st-1")
        self.assertEqual(first["lines"][0]["payable_after"], "100.00")
        self.assertTrue(self.service.verify_settlement("st-1")["intact"])
        # 订单时间线串起两个期间的同一笔变化
        timeline = self.service.order_trace("order-1")
        self.assertEqual([s["period"] for s in timeline["settlements"]],
                         ["2026-11", "2026-12"])

    def test_attribution_correction_claws_back_then_pays_new_creator(self):
        at = "2026-11-06T20:00:00+08:00"
        self.feed(
            touch("clk-a", "click", "cust-ext-2", at, "creator-a"),
            touch("clk-b", "click", "cust-ext-2", at, "creator-b"),
            order("ord-2", "cust-ext-2", "2026-11-07T11:00:00+08:00", "order-2", "500.00"),
        )
        # 11 月人工裁决给 A 并封存（A 10%：应付 50）
        self.service.recompute_attribution("2026-11-08T00:00:00+00:00")
        self.service.resolve_review("order-2", ATTRIBUTED, "creator-a", "首次裁决",
                                    "2026-11-09T00:00:00+00:00")
        self.service.seal_settlement(
            "st-1", "2026-11", "2026-11-30T16:00:00+00:00", "2026-12-01T10:00:00+00:00")
        # 12 月调查后改判给 B（B 8%：应付 40）：先追回 A 的 50，再补付 B 的 40
        self.service.redecide("order-2", ATTRIBUTED, "creator-b", "调查后改判B",
                              "2026-12-07T00:00:00+00:00")
        second = self.service.seal_settlement(
            "st-2", "2026-12", "2026-12-31T16:00:00+00:00", "2027-01-05T10:00:00+00:00")
        by_kind = {line["kind"]: line for line in second["lines"]}
        self.assertEqual(set(by_kind), {"reversal", "attribution_adjustment"})
        self.assertEqual(by_kind["reversal"]["creator"], "creator-a")
        self.assertEqual(by_kind["reversal"]["delta"], "-50.00")
        self.assertEqual(by_kind["attribution_adjustment"]["creator"], "creator-b")
        self.assertEqual(by_kind["attribution_adjustment"]["delta"], "40.00")
        # 期间合计净调整 -10
        self.assertEqual(second["campaign_totals"]["camp-1"]["delta"], "-10.00")
        # 旧结算单保持原样
        self.assertEqual(
            self.service.get_settlement("st-1")["lines"][0]["payable_after"], "50.00")
        # 两次裁决均留痕，可逐单回查
        resolutions = self.service.order_trace("order-2")["resolutions"]
        self.assertEqual([r["version"] for r in resolutions], [1, 2])
        self.assertEqual([r["creator"] for r in resolutions], ["creator-a", "creator-b"])

    def test_redecide_requires_prior_resolution(self):
        self.feed(
            touch("clk-1", "click", "cust-ext-1", "2026-11-02T10:00:00+08:00", "creator-a"),
            order("ord-1", "cust-ext-1", "2026-11-04T10:00:00+08:00", "order-1", "200.00"),
        )
        with self.assertRaises(ValueError):
            self.service.redecide("order-1", ATTRIBUTED, "creator-b", "无裁决先改判",
                                  "2026-12-07T00:00:00+00:00")

    def test_budget_views_and_resolved_pending_settles_later(self):
        at = "2026-11-06T20:00:00+08:00"
        self.feed(
            touch("clk-a", "click", "cust-ext-2", at, "creator-a"),
            touch("clk-b", "click", "cust-ext-2", at, "creator-b"),
            order("ord-2", "cust-ext-2", "2026-11-07T11:00:00+08:00", "order-2", "500.00"),
        )
        self.service.seal_settlement(
            "st-1", "2026-11", "2026-11-30T16:00:00+00:00", "2026-12-01T10:00:00+00:00")
        view = self.service.campaign_analysis()["campaigns"]["camp-1"]
        # 待核净额不占预算
        self.assertEqual(view["settlement"]["sealed_payable"], "0.00")
        self.assertEqual(view["settlement"]["budget_remaining"], "10000.00")
        # 裁决后在下一期间正常结算
        self.service.resolve_review("order-2", ATTRIBUTED, "creator-b", "改判B",
                                    "2026-12-08T00:00:00+00:00")
        second = self.service.seal_settlement(
            "st-2", "2026-12", "2026-12-31T16:00:00+00:00", "2027-01-05T10:00:00+00:00")
        self.assertEqual(second["lines"][0]["kind"], "settlement")
        self.assertEqual(second["lines"][0]["creator"], "creator-b")
        self.assertEqual(second["lines"][0]["payable_after"], "40.00")

    def test_deleted_settled_order_is_reversed(self):
        self.feed(
            touch("clk-1", "click", "cust-ext-1", "2026-11-02T10:00:00+08:00", "creator-a"),
            order("ord-1", "cust-ext-1", "2026-11-04T10:00:00+08:00", "order-1", "1000.00"),
        )
        self.service.seal_settlement(
            "st-1", "2026-11", "2026-11-30T16:00:00+00:00", "2026-12-01T10:00:00+00:00")
        self.service.ingest(dict(
            order("ord-1", "cust-ext-1", "2026-11-04T10:00:00+08:00",
                  "order-1", "1000.00"),
            revision=2, observed_at="2026-12-06T10:00:00+08:00", deleted=True))
        second = self.service.seal_settlement(
            "st-2", "2026-12", "2026-12-31T16:00:00+00:00", "2027-01-05T10:00:00+00:00")
        self.assertEqual(second["lines"][0]["kind"], "reversal")
        self.assertEqual(second["lines"][0]["delta"], "-100.00")

    def test_watermark_must_advance(self):
        self.feed(
            touch("clk-1", "click", "cust-ext-1", "2026-11-02T10:00:00+08:00", "creator-a"),
            order("ord-1", "cust-ext-1", "2026-11-04T10:00:00+08:00", "order-1", "1000.00"),
        )
        self.service.seal_settlement(
            "st-1", "2026-11", "2026-11-30T16:00:00+00:00", "2026-12-01T10:00:00+00:00")
        with self.assertRaises(ValueError):
            self.service.seal_settlement(
                "st-2", "2026-12", "2026-11-29T16:00:00+00:00", "2026-12-02T10:00:00+00:00")

    def test_tampered_settlement_is_detected(self):
        self.feed(
            touch("clk-1", "click", "cust-ext-1", "2026-11-02T10:00:00+08:00", "creator-a"),
            order("ord-1", "cust-ext-1", "2026-11-04T10:00:00+08:00", "order-1", "1000.00"),
        )
        self.service.seal_settlement(
            "st-1", "2026-11", "2026-11-30T16:00:00+00:00", "2026-12-01T10:00:00+00:00")
        self.assertTrue(self.service.verify_settlement("st-1")["intact"])
        # 直接篡改底层封存正文，回查必须发现
        self.service.connection.execute(
            "UPDATE settlements SET body=replace(body,'1000.00','1.00') WHERE settlement_id='st-1'")
        self.assertFalse(self.service.verify_settlement("st-1")["intact"])

    def test_unchanged_order_emits_no_adjustment_line(self):
        self.feed(
            touch("clk-1", "click", "cust-ext-1", "2026-11-02T10:00:00+08:00", "creator-a"),
            order("ord-1", "cust-ext-1", "2026-11-04T10:00:00+08:00", "order-1", "1000.00"),
        )
        self.service.seal_settlement(
            "st-1", "2026-11", "2026-11-30T16:00:00+00:00", "2026-12-01T10:00:00+00:00")
        second = self.service.seal_settlement(
            "st-2", "2026-12", "2026-12-31T16:00:00+00:00", "2027-01-05T10:00:00+00:00")
        self.assertEqual(second["lines"], [])
        self.assertEqual(second["campaign_totals"], {})

    def test_late_observed_refund_excluded_from_period_then_adjusts_next(self):
        # 退款发生在 11 月，但 12 月才被观测到：11 月水位封存时不计入
        self.feed(
            touch("clk-1", "click", "cust-ext-1", "2026-11-02T10:00:00+08:00", "creator-a"),
            order("ord-1", "cust-ext-1", "2026-11-04T10:00:00+08:00", "order-1", "1000.00"),
            event("ref-1", "refund", "cust-ext-1", "2026-11-15T10:00:00+08:00",
                  {"order_id": "order-1", "amount": "400.00"},
                  observed="2026-12-03T10:00:00+08:00"),
        )
        first = self.service.seal_settlement(
            "st-1", "2026-11", "2026-11-30T16:00:00+00:00", "2026-12-01T10:00:00+00:00")
        self.assertEqual(first["lines"][0]["payable_after"], "100.00")
        # 当前归因表仍按全量最新视图反映退款
        self.assertEqual(self.service.order_trace("order-1")["current"]["net"], "600.00")
        second = self.service.seal_settlement(
            "st-2", "2026-12", "2026-12-31T16:00:00+00:00", "2027-01-05T10:00:00+00:00")
        self.assertEqual(second["lines"][0]["kind"], "refund_adjustment")
        self.assertEqual(second["lines"][0]["delta"], "-40.00")
        self.assertEqual(second["lines"][0]["payable_after"], "60.00")


if __name__ == "__main__":
    unittest.main()
