"""面向市场与财务的活动分析：同一套归因结论的两种读法。

- 市场视角：曝光→点击→订单漏斗、各创作者触达与归因订单。
- 财务视角：预算占用、已封存应付、未结算预算、待核金额与调整。
所有数字都从同一批不可变事件与同一套归因结论推导，不另设口径。
"""
from collections import defaultdict
from decimal import Decimal

from .attribution import ATTRIBUTED, PENDING_REVIEW, UNATTRIBUTED, INVALID, money_str


class CampaignAnalytics:
    def __init__(self, connection, store, entities, contracts, ledger, settlements):
        self.connection = connection
        self.store, self.entities = store, entities
        self.contracts, self.ledger, self.settlements = contracts, ledger, settlements

    def run(self, as_of=None):
        computed = self.ledger.compute(as_of)
        decisions = computed["decisions"]
        funnel = self._funnel(as_of)
        campaigns = {}
        for config in self.connection.execute(
                "SELECT * FROM campaigns ORDER BY campaign").fetchall():
            campaign = config["campaign"]
            related = [d for d in decisions if d["campaign"] == campaign]
            campaigns[campaign] = self._campaign_view(campaign, dict(config), related)
        return {
            "as_of": as_of,
            "funnel": funnel,
            "campaigns": campaigns,
            "order_status_counts": self._status_counts(decisions),
            "issues": computed["issues"],
        }

    def _funnel(self, as_of):
        counts = defaultdict(lambda: defaultdict(int))
        distinct = defaultdict(lambda: defaultdict(set))
        for event in self.store.current(as_of):
            if event["kind"] not in ("exposure", "click"):
                continue
            campaign = event["payload"].get("campaign")
            if not isinstance(campaign, str):
                continue
            counts[campaign][event["kind"]] += 1
            customer = self.entities.resolve(event["source"], event["entity"])
            if customer:
                distinct[campaign][f"{event['kind']}_customers"].add(customer)
        result = {}
        for campaign in sorted(set(counts) | set(distinct)):
            result[campaign] = {
                "exposures": counts[campaign].get("exposure", 0),
                "clicks": counts[campaign].get("click", 0),
                "exposed_customers": len(distinct[campaign].get("exposure_customers", set())),
                "clicked_customers": len(distinct[campaign].get("click_customers", set())),
            }
            exposures = result[campaign]["exposures"]
            result[campaign]["click_through_rate"] = (
                str(round(result[campaign]["clicks"] / exposures, 4)) if exposures else None)
        return result

    def _campaign_view(self, campaign, config, related):
        gross = sum((Decimal(d["gross"]) for d in related if d["gross"] is not None), Decimal("0"))
        refunded = sum((Decimal(d["refunded"]) for d in related), Decimal("0"))
        net = sum((Decimal(d["net"]) for d in related), Decimal("0"))

        by_status = self._status_counts(related)
        pending_amount = sum(
            (Decimal(d["net"]) for d in related if d["status"] == PENDING_REVIEW), Decimal("0"))
        unattributed_amount = sum(
            (Decimal(d["net"]) for d in related if d["status"] == UNATTRIBUTED), Decimal("0"))

        creator_rows = defaultdict(lambda: {
            "attributed_orders": 0, "gross": Decimal("0"), "refunded": Decimal("0"),
            "net": Decimal("0"), "payable_pending_settlement": Decimal("0"),
            "share": None,
        })
        for decision in related:
            if decision["status"] != ATTRIBUTED:
                continue
            row = creator_rows[decision["creator"]]
            row["attributed_orders"] += 1
            row["gross"] += Decimal(decision["gross"])
            row["refunded"] += Decimal(decision["refunded"])
            row["net"] += Decimal(decision["net"])
            row["payable_pending_settlement"] += Decimal(decision["payable"])
            row["share"] = self.contracts.share(campaign, decision["creator"])

        sealed = self._sealed_totals(campaign)
        budget = Decimal(config["budget"])
        committed = sealed["payable_after"]
        creators = {}
        for creator, row in sorted(creator_rows.items()):
            creators[creator] = {key: (money_str(value) if isinstance(value, Decimal) else value)
                                 for key, value in row.items()}
        return {
            "budget": config["budget"],
            "windows": {"click_days": config["click_window_days"],
                        "exposure_days": config["exposure_window_days"]},
            "orders": by_status,
            "amounts": {
                "gross": money_str(gross), "refunded": money_str(refunded),
                "net": money_str(net),
                "pending_review_net": money_str(pending_amount),
                "unattributed_net": money_str(unattributed_amount),
            },
            "creators": creators,
            "settlement": {
                "sealed_payable": money_str(sealed["payable_after"]),
                "period_adjustments": money_str(sealed["delta"]),
                "unsettled_budget": money_str(max(Decimal("0"), budget - committed)),
                "budget_committed": money_str(committed),
                "budget_remaining": money_str(max(Decimal("0"), budget - committed)),
            },
        }

    def _sealed_totals(self, campaign):
        rows = self.connection.execute(
            """SELECT sl.delta, sl.payable_after FROM settlement_lines sl
               JOIN settlements s ON s.settlement_id=sl.settlement_id
               WHERE sl.campaign=?""", (campaign,)).fetchall()
        totals = {"delta": Decimal("0"), "payable_after": Decimal("0")}
        for row in rows:
            totals["delta"] += Decimal(row["delta"])
            totals["payable_after"] += Decimal(row["payable_after"])
        return totals

    def _status_counts(self, decisions):
        counts = defaultdict(int)
        for decision in decisions:
            counts[decision["status"]] += 1
        return {status: counts.get(status, 0)
                for status in (ATTRIBUTED, PENDING_REVIEW, UNATTRIBUTED, INVALID)}

    def pending_book(self):
        """待核工作台：金额被隔离、不进结算，等待人工裁决。"""
        rows = self.ledger.pending_reviews()
        return [{
            "order_id": row["order_id"], "campaign": row["campaign"],
            "reason": row["reason"], "reason_code": row.get("reason_code"),
            "net": row["net"], "suggested_creator": row.get("suggested_creator"),
        } for row in rows]
