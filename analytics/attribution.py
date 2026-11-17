"""把曝光、点击、订单、退款解释为每笔订单唯一的归因结论。

口径要点：
- 一笔订单在任何时刻最多只有一个结算对象；确定性规则无法打破的多创作者
  触达进入待核，人工裁决后才可结算；无触达为未归因，永不结算。
- 归因是可重算的纯解释：来源事件保持不可变，每次重算完整重放事件流，
  结论的历史版本按订单留痕，人工裁决独立保存并优先于自动规则。
"""
import json
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

ATTRIBUTED = "attributed"          # 已归因，可进入结算
PENDING_REVIEW = "pending_review"  # 待核：多创作者等歧义，人工裁决前不可结算
UNATTRIBUTED = "unattributed"      # 未归因：无合格触达，不结算
INVALID = "invalid"                # 订单本身不符合分析口径

CENT = Decimal("0.01")


def parse_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def money(payload, field, issues, event_id):
    raw = payload.get(field)
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        issues.append({"event_id": event_id, "problem": f"{field}不是有效金额"})
        return None
    if not value.is_finite() or value < 0:
        issues.append({"event_id": event_id, "problem": f"{field}必须为非负有限数"})
        return None
    return value


def rounded(value):
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def money_str(value):
    return str(rounded(value))


class AttributionLedger:
    def __init__(self, connection, store, entities, contracts):
        self.connection = connection
        self.store, self.entities, self.contracts = store, entities, contracts
        connection.execute("""CREATE TABLE IF NOT EXISTS attribution_decisions(
            order_id TEXT PRIMARY KEY, campaign TEXT, customer TEXT,
            status TEXT, creator TEXT, basis TEXT, reason TEXT,
            gross TEXT, refunded TEXT, net TEXT, payable TEXT,
            snapshot TEXT NOT NULL, decided_at TEXT NOT NULL)""")
        connection.execute("""CREATE TABLE IF NOT EXISTS attribution_history(
            order_id TEXT NOT NULL, version INTEGER NOT NULL,
            status TEXT, creator TEXT, basis TEXT, reason TEXT,
            net TEXT, payable TEXT, snapshot TEXT NOT NULL, decided_at TEXT NOT NULL,
            PRIMARY KEY(order_id, version))""")
        connection.execute("""CREATE TABLE IF NOT EXISTS review_resolutions(
            order_id TEXT PRIMARY KEY, resolution TEXT, creator TEXT,
            note TEXT, decided_at TEXT NOT NULL)""")
        connection.execute("""CREATE TABLE IF NOT EXISTS review_resolution_history(
            order_id TEXT NOT NULL, version INTEGER NOT NULL,
            resolution TEXT, creator TEXT, note TEXT, decided_at TEXT NOT NULL,
            PRIMARY KEY(order_id, version))""")

    # ------------------------------------------------------------------ #
    # 纯重算：给定事件流，得出每笔订单当前的归因结论
    # ------------------------------------------------------------------ #
    def compute(self, as_of=None):
        issues = []
        orders = {}
        refunds = defaultdict(Decimal)
        refund_events = defaultdict(list)
        touches = defaultdict(list)

        for event in self.store.current(as_of):
            kind = event["kind"]
            payload = event["payload"]
            eid = event["event_id"]
            if kind in ("exposure", "click"):
                campaign = payload.get("campaign")
                creator = payload.get("creator")
                if not isinstance(campaign, str) or not campaign.strip():
                    issues.append({"event_id": eid, "problem": "触达缺少活动编号"})
                    continue
                if not isinstance(creator, str) or not creator.strip():
                    issues.append({"event_id": eid, "problem": "触达缺少创作者"})
                    continue
                customer = self.entities.resolve(event["source"], event["entity"])
                if customer is None:
                    issues.append({"event_id": eid, "problem": "顾客主体无法映射"})
                    continue
                touches[(campaign, customer)].append({
                    "source": event["source"], "event_id": eid, "kind": kind,
                    "creator": creator, "occurred_at": event["occurred_at"],
                    "observed_at": event["observed_at"],
                })
            elif kind == "order":
                order_id = payload.get("order_id")
                if not isinstance(order_id, str) or not order_id.strip():
                    issues.append({"event_id": eid, "problem": "订单事件缺少订单编号"})
                    continue
                if order_id in orders:
                    # 同一订单出现多条订单事件：口径无法自行确定
                    orders[order_id]["duplicate"] = True
                    orders[order_id]["gross"] = None
                    continue
                campaign = payload.get("campaign")
                if not isinstance(campaign, str) or not campaign.strip():
                    issues.append({"event_id": eid, "problem": "订单缺少活动编号"})
                    campaign = campaign if isinstance(campaign, str) else None
                amount = money(payload, "amount", issues, eid)
                customer = self.entities.resolve(event["source"], event["entity"])
                orders[order_id] = {
                    "order_id": order_id, "campaign": campaign,
                    "customer": customer, "gross": amount,
                    "occurred_at": event["occurred_at"],
                    "source_event": eid, "duplicate": False,
                }
            elif kind == "refund":
                order_id = payload.get("order_id")
                if not isinstance(order_id, str) or not order_id.strip():
                    issues.append({"event_id": eid, "problem": "退款事件缺少订单编号"})
                    continue
                amount = money(payload, "amount", issues, eid)
                if amount is not None:
                    refunds[order_id] += amount
                    refund_events[order_id].append(eid)

        for order_id in sorted(set(refunds) - set(orders)):
            issues.append({"order_id": order_id,
                           "problem": "退款找不到对应订单，未参与任何归因"})
        decisions = []
        for order_id in sorted(orders):
            order = orders[order_id]
            decision = self._decide(
                order, refunds.get(order_id, Decimal("0")),
                refund_events.get(order_id, []), touches, issues)
            decision = self._apply_resolution(decision, as_of)
            decisions.append(decision)
        return {"decisions": decisions, "issues": issues}

    def _apply_resolution(self, decision, as_of=None):
        """人工裁决优先于自动规则：裁决后即使事件修订也保持人工结论。"""
        row = self.connection.execute(
            "SELECT * FROM review_resolutions WHERE order_id=?",
            (decision["order_id"],)).fetchone()
        if row is None:
            return decision
        if as_of is not None and parse_time(row["decided_at"]) > parse_time(as_of):
            return decision
        decision["basis"] = "manual_review"
        decision["reason"] = None
        decision["snapshot"]["note"] = (
            f"人工裁决：{row['resolution']}（{row['note'] or '无备注'}，{row['decided_at']}）")
        if row["resolution"] == ATTRIBUTED:
            decision["status"] = ATTRIBUTED
            decision["creator"] = row["creator"]
            self._with_payable(decision, row["creator"], decision["campaign"])
        else:
            decision["status"] = UNATTRIBUTED
            decision["creator"] = None
            decision["payable"] = "0.00"
        return decision

    def _decide(self, order, refunded, refund_event_ids, touches, issues):
        order_id = order["order_id"]
        campaign = order["campaign"]
        gross = order["gross"]
        snapshot = {"order": order_id, "campaign": campaign,
                    "order_event": order["source_event"],
                    "gross": money_str(gross) if gross is not None else None,
                    "refunded": money_str(refunded), "path": [],
                    "refund_events": list(refund_event_ids), "note": ""}
        if order["duplicate"]:
            return self._pending(order, snapshot, gross or Decimal("0"), refunded,
                                 "同一订单存在多条订单事件", "duplicate_order")
        if gross is None:
            return self._invalid(order, snapshot, "订单金额无效")
        if order["customer"] is None:
            return self._invalid(order, snapshot, "订单顾客主体无法映射")
        config = self.contracts.campaign(campaign) if campaign else None
        if config is None:
            return self._pending(order, snapshot, gross, refunded,
                                 "订单引用的活动未登记", "unknown_campaign")

        order_time = parse_time(order["occurred_at"])
        click_cutoff = order_time - timedelta(days=config["click_window_days"])
        exposure_cutoff = order_time - timedelta(days=config["exposure_window_days"])
        path = []
        for touch in touches.get((campaign, order["customer"]), ()):
            touch_time = parse_time(touch["occurred_at"])
            if touch_time > order_time:
                in_window, reason = False, "after_order"
            elif touch["kind"] == "click" and touch_time >= click_cutoff:
                in_window, reason = True, "click_window"
            elif touch["kind"] == "exposure" and touch_time >= exposure_cutoff:
                in_window, reason = True, "exposure_window"
            else:
                in_window, reason = False, "outside_window"
            path.append({**touch, "eligible": in_window, "window_basis": reason})
        path.sort(key=lambda t: (t["occurred_at"], t["event_id"]))
        snapshot["path"] = path
        eligible = [t for t in path if t["eligible"]]
        if not eligible:
            return self._unattributed(order, snapshot, gross, refunded)

        # 有点击时最后一次点击优先；否则取最后一次曝光
        clicks = [t for t in eligible if t["kind"] == "click"]
        pool = clicks if clicks else [t for t in eligible if t["kind"] == "exposure"]
        if not pool:
            return self._unattributed(order, snapshot, gross, refunded)
        winner_time = max(t["occurred_at"] for t in pool)
        finalists = [t for t in pool if t["occurred_at"] == winner_time]
        creators = sorted({t["creator"] for t in finalists})
        basis = "last_click" if clicks else "last_exposure"
        if len(creators) > 1:
            snapshot["note"] = "同一时刻存在多位创作者的触达，规则无法打破"
            return self._pending(order, snapshot, gross, refunded,
                                 f"最后触达在创作者 {'、'.join(creators)} 之间并列",
                                 "creator_tie", basis)
        creator = creators[0]
        if self.contracts.share(campaign, creator) is None:
            return self._pending(order, snapshot, gross, refunded,
                                 f"创作者 {creator} 未参加该活动",
                                 "creator_not_enrolled", basis)
        return self._attributed(order, snapshot, gross, refunded, creator, basis)

    # ------------------------------------------------------------------ #
    # 结论组装
    # ------------------------------------------------------------------ #
    def _base(self, order, snapshot, gross, refunded, status, reason, basis=None):
        net = max(Decimal("0"), gross - refunded) if gross is not None else Decimal("0")
        return {
            "order_id": order["order_id"], "campaign": order["campaign"],
            "customer": order["customer"], "status": status,
            "creator": None, "basis": basis, "reason": reason,
            "gross": money_str(gross) if gross is not None else None,
            "refunded": money_str(refunded), "net": money_str(net),
            "payable": "0.00", "snapshot": snapshot,
        }

    def _with_payable(self, decision, creator, campaign):
        if creator is None:
            return decision
        share = self.contracts.share(campaign, creator)
        if share is None:
            decision["payable"] = "0.00"
            return decision
        decision["payable"] = money_str(Decimal(decision["net"]) * Decimal(share))
        return decision

    def _attributed(self, order, snapshot, gross, refunded, creator, basis):
        decision = self._base(order, snapshot, gross, refunded, ATTRIBUTED, None, basis)
        decision["creator"] = creator
        return self._with_payable(decision, creator, order["campaign"])

    def _pending(self, order, snapshot, gross, refunded, reason, code, basis=None):
        decision = self._base(order, snapshot, gross, refunded, PENDING_REVIEW, reason, basis)
        decision["reason_code"] = code
        # 给出规则建议的创作者，供人工裁决参考
        eligible = [t for t in snapshot["path"] if t["eligible"]]
        if eligible:
            decision["suggested_creator"] = sorted({t["creator"] for t in eligible})
        return decision

    def _unattributed(self, order, snapshot, gross, refunded):
        snapshot["note"] = "回看窗口内没有任何合格触达"
        return self._base(order, snapshot, gross, refunded, UNATTRIBUTED,
                          "回看窗口内无合格触达")

    def _invalid(self, order, snapshot, reason):
        decision = self._base(order, snapshot, Decimal("0"), Decimal("0"), INVALID, reason)
        decision["gross"] = None
        return decision

    # ------------------------------------------------------------------ #
    # 留痕重算与人工裁决
    # ------------------------------------------------------------------ #
    def recompute(self, decided_at, as_of=None):
        """重算并留痕。as_of 为封存水位时，只采用该时点前已观测到的事件，
        且水位之后作出的人工裁决不影响该期间。"""
        result = self.compute(as_of)
        for decision in result["decisions"]:
            self._persist(decision, decided_at)
        return result

    def _persist(self, decision, decided_at):
        order_id = decision["order_id"]
        snapshot = json.dumps(decision["snapshot"], ensure_ascii=False, sort_keys=True)
        prior = self.connection.execute(
            "SELECT * FROM attribution_decisions WHERE order_id=?", (order_id,)).fetchone()
        signature = (decision["status"], decision["creator"], decision["basis"],
                     decision["reason"], decision["net"], decision["payable"], snapshot)
        if prior is not None:
            old = (prior["status"], prior["creator"], prior["basis"], prior["reason"],
                   prior["net"], prior["payable"], prior["snapshot"])
            if old == signature:
                return False
            version = self.connection.execute(
                "SELECT COALESCE(MAX(version),0)+1 FROM attribution_history WHERE order_id=?",
                (order_id,)).fetchone()[0]
            self.connection.execute(
                "INSERT INTO attribution_history "
                "(order_id,version,status,creator,basis,reason,net,payable,snapshot,decided_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (order_id, version, prior["status"], prior["creator"], prior["basis"],
                 prior["reason"], prior["net"], prior["payable"],
                 prior["snapshot"], prior["decided_at"]))
        self.connection.execute(
            "INSERT OR REPLACE INTO attribution_decisions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (order_id, decision["campaign"], decision["customer"], decision["status"],
             decision["creator"], decision["basis"], decision["reason"],
             decision["gross"], decision["refunded"], decision["net"],
             decision["payable"], snapshot, decided_at))
        return True

    def resolve_review(self, order_id, resolution, creator, note, decided_at,
                       _allow_redecide=False):
        order = self.connection.execute(
            "SELECT * FROM attribution_decisions WHERE order_id=?", (order_id,)).fetchone()
        if order is None:
            raise ValueError("订单尚无归因结论，无法裁决")
        already = self.connection.execute(
            "SELECT 1 FROM review_resolutions WHERE order_id=?", (order_id,)).fetchone()
        if already and not _allow_redecide:
            raise ValueError("订单已裁决，改判请使用再裁决接口")
        if not already and order["status"] != PENDING_REVIEW:
            raise ValueError("仅待核订单可人工裁决")
        if resolution == ATTRIBUTED:
            if not isinstance(creator, str) or not creator.strip():
                raise ValueError("归因裁决必须指定创作者")
            if self.contracts.share(order["campaign"], creator) is None:
                raise ValueError("创作者未参加该活动，不能裁决归因")
        elif resolution != UNATTRIBUTED:
            raise ValueError("裁决结果只能是已归因或未归因")
        self.connection.execute(
            "INSERT OR REPLACE INTO review_resolutions VALUES(?,?,?,?,?)",
            (order_id, resolution, creator if resolution == ATTRIBUTED else None,
             note, decided_at))
        version = self.connection.execute(
            "SELECT COALESCE(MAX(version),0)+1 FROM review_resolution_history "
            "WHERE order_id=?", (order_id,)).fetchone()[0]
        self.connection.execute(
            "INSERT INTO review_resolution_history VALUES(?,?,?,?,?,?)",
            (order_id, version, resolution,
             creator if resolution == ATTRIBUTED else None, note, decided_at))
        return True

    def redecide(self, order_id, resolution, creator, note, decided_at):
        """活动归属纠正：仅对已人工裁决过的订单改判，保留完整裁决留痕。"""
        prior = self.connection.execute(
            "SELECT 1 FROM review_resolutions WHERE order_id=?", (order_id,)).fetchone()
        if prior is None:
            raise ValueError("仅已人工裁决的订单可改判")
        return self.resolve_review(order_id, resolution, creator, note, decided_at,
                                   _allow_redecide=True)

    def current_decisions(self):
        rows = self.connection.execute(
            "SELECT * FROM attribution_decisions ORDER BY order_id").fetchall()
        return [self._row_to_decision(row) for row in rows]

    def decision(self, order_id):
        row = self.connection.execute(
            "SELECT * FROM attribution_decisions WHERE order_id=?", (order_id,)).fetchone()
        return self._row_to_decision(row) if row else None

    def _row_to_decision(self, row):
        data = {key: row[key] for key in row.keys()}
        data["snapshot"] = json.loads(row["snapshot"])
        return data

    def pending_reviews(self):
        rows = self.connection.execute(
            """SELECT d.* FROM attribution_decisions d
               WHERE d.status=? AND NOT EXISTS(
                   SELECT 1 FROM review_resolutions r WHERE r.order_id=d.order_id)
               ORDER BY d.order_id""",
            (PENDING_REVIEW,)).fetchall()
        return [self._row_to_decision(row) for row in rows]

    def history(self, order_id):
        rows = self.connection.execute(
            "SELECT * FROM attribution_history WHERE order_id=? ORDER BY version",
            (order_id,)).fetchall()
        versions = [dict(row) | {"snapshot": json.loads(row["snapshot"])} for row in rows]
        current = self.decision(order_id)
        return {"order_id": order_id, "versions": versions, "current": current}

    def resolution_history(self, order_id):
        rows = self.connection.execute(
            "SELECT * FROM review_resolution_history WHERE order_id=? ORDER BY version",
            (order_id,)).fetchall()
        return [dict(row) for row in rows]
