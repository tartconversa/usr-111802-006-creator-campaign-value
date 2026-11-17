"""按期间封存结算依据，并把封存后的变化表达为后续期间的调整。

口径要点：
- 结算单是哈希封存的不可变证据：封存时刻的订单结论、触达路径与金额
  全部随单留痕，已封存内容永不改写。
- 订单一旦进入结算单即被冻结：之后的退款、事件修订、归属纠正都不
  回改旧单，而是在新期间生成调整行（追回为负、补付为正）。
- 待核与未归因订单不占用预算；人工裁决后在其后的期间结算或调整。
"""
import hashlib
import json
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal

from .decisions import encode
from .attribution import ATTRIBUTED, PENDING_REVIEW, UNATTRIBUTED, INVALID, money_str


def utcnow_iso():
    return datetime.now(timezone.utc).isoformat()


class SettlementBook:
    def __init__(self, connection, ledger):
        self.connection = connection
        self.ledger = ledger
        connection.execute("""CREATE TABLE IF NOT EXISTS settlements(
            settlement_id TEXT PRIMARY KEY, period TEXT NOT NULL,
            watermark TEXT NOT NULL, sealed_at TEXT NOT NULL,
            body TEXT NOT NULL, digest TEXT NOT NULL)""")
        connection.execute("""CREATE TABLE IF NOT EXISTS settlement_lines(
            settlement_id TEXT NOT NULL, line_no INTEGER NOT NULL,
            order_id TEXT NOT NULL, campaign TEXT, creator TEXT,
            kind TEXT NOT NULL, gross TEXT, refunded TEXT, net TEXT,
            delta TEXT NOT NULL, payable_before TEXT NOT NULL, payable_after TEXT NOT NULL,
            evidence TEXT NOT NULL,
            PRIMARY KEY(settlement_id, line_no),
            FOREIGN KEY(settlement_id) REFERENCES settlements(settlement_id))""")

    def seal(self, settlement_id, period, watermark=None, sealed_at=None):
        sealed_at = sealed_at or utcnow_iso()
        if watermark is None:
            watermark = sealed_at
        with self.connection:
            if self.connection.execute(
                    "SELECT 1 FROM settlements WHERE period=?", (period,)).fetchone():
                raise ValueError("该期间已封存结算单")
            latest = self.connection.execute(
                "SELECT MAX(watermark) AS w FROM settlements").fetchone()["w"]
            if latest is not None and watermark < latest:
                raise ValueError("封存水位不得早于已有结算单")

            # 结算行只采用封存水位前已观测到的事件与已作出的裁决
            watermark_view = self.ledger.compute(watermark)
            decisions = {d["order_id"]: d for d in watermark_view["decisions"]}
            # 当前归因表按全量最新事件留痕，不被水位视图覆盖
            self.ledger.recompute(sealed_at)
            baseline = self._baselines()

            lines = []
            for order_id in sorted(decisions):
                decision = decisions[order_id]
                if order_id in baseline:
                    lines.extend(self._adjustment_lines(decision, baseline[order_id]))
                elif decision["status"] == ATTRIBUTED:
                    lines.append(self._line(decision, "settlement", decision))
            for order_id in sorted(set(baseline) - set(decisions)):
                # 已结算订单在新事件流下消失（例如被修订删除）：全额追回
                lines.append(self._reversal_line(baseline[order_id]))

            body = {
                "settlement_id": settlement_id, "period": period,
                "watermark": watermark, "sealed_at": sealed_at,
                "line_count": len(lines),
                "lines": [{k: v for k, v in line.items() if k != "line_no"} for line in lines],
                "campaign_totals": self._campaign_totals(lines),
                "creator_totals": self._creator_totals(lines),
            }
            digest = self._digest(body)
            self.connection.execute(
                "INSERT INTO settlements VALUES(?,?,?,?,?,?)",
                (settlement_id, period, watermark, sealed_at, encode(body), digest))
            for line_no, line in enumerate(lines, start=1):
                self.connection.execute(
                    "INSERT INTO settlement_lines VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (settlement_id, line_no, line["order_id"], line["campaign"],
                     line["creator"], line["kind"], line["gross"], line["refunded"],
                     line["net"], line["delta"], line["payable_before"],
                     line["payable_after"], encode(line["evidence"])))
        return self.get(settlement_id)

    def _baselines(self):
        """每个已结算订单最近一次出现的结算行，即该订单当前的冻结基线。"""
        rows = self.connection.execute(
            """SELECT sl.* FROM settlement_lines sl
               JOIN settlements s ON s.settlement_id=sl.settlement_id
               ORDER BY s.sealed_at, sl.line_no""").fetchall()
        latest = {}
        for row in rows:
            latest[row["order_id"]] = row
        return latest

    def _adjustment_lines(self, decision, base):
        lines = []
        before = Decimal(base["payable_after"])
        if decision["status"] == ATTRIBUTED:
            after = Decimal(decision["payable"])
            if base["creator"] == decision["creator"] and before != after:
                # 同一创作者：退款或净额修订带来的差额（通常为负，即追回）
                lines.append(self._line(decision, "refund_adjustment", decision, before, after))
            elif base["creator"] != decision["creator"]:
                # 归属纠正：先从原创作者追回，再向新创作者补付
                lines.append(self._reversal_line(base, decision))
                if after != 0:
                    lines.append(self._line(decision, "attribution_adjustment",
                                            decision, Decimal("0"), after))
        elif decision["status"] in (PENDING_REVIEW, INVALID):
            if before != 0:
                lines.append(self._hold_line(decision, base, before))
        else:  # UNATTRIBUTED
            if before != 0:
                lines.append(self._reversal_line(base, decision))
        return lines

    def _line(self, decision, kind, evidence_decision, before=None, after=None):
        payable = Decimal(decision["payable"])
        before = payable if before is None else Decimal(before)
        after = payable if after is None else Decimal(after)
        return {
            "order_id": decision["order_id"], "campaign": decision["campaign"],
            "creator": decision["creator"], "kind": kind,
            "gross": decision["gross"], "refunded": decision["refunded"],
            "net": decision["net"], "delta": money_str(after - before),
            "payable_before": money_str(before), "payable_after": money_str(after),
            "evidence": evidence_decision["snapshot"],
        }

    def _reversal_line(self, base, decision=None):
        before = Decimal(base["payable_after"])
        evidence = json.loads(base["evidence"])
        evidence["reversal_reason"] = (
            "订单在最新事件流下不再归属原创作者" if decision is not None
            else "订单事件在最新修订中消失")
        return {
            "order_id": base["order_id"], "campaign": base["campaign"],
            "creator": base["creator"], "kind": "reversal",
            "gross": base["gross"], "refunded": base["refunded"], "net": base["net"],
            "delta": money_str(-before), "payable_before": money_str(before),
            "payable_after": "0.00", "evidence": evidence,
        }

    def _hold_line(self, decision, base, before):
        return {
            "order_id": base["order_id"], "campaign": base["campaign"],
            "creator": base["creator"], "kind": "hold_reversal",
            "gross": decision["gross"], "refunded": decision["refunded"],
            "net": decision["net"], "delta": money_str(-before),
            "payable_before": money_str(before), "payable_after": "0.00",
            "evidence": {**decision["snapshot"],
                         "hold_reason": f"订单转入{decision['status']}：{decision['reason']}"},
        }

    def _campaign_totals(self, lines):
        totals = defaultdict(lambda: {"delta": Decimal("0"), "payable_after": Decimal("0")})
        for line in lines:
            totals[line["campaign"]]["delta"] += Decimal(line["delta"])
            totals[line["campaign"]]["payable_after"] += Decimal(line["payable_after"])
        return {campaign: {key: money_str(value) for key, value in values.items()}
                for campaign, values in sorted(totals.items())}

    def _creator_totals(self, lines):
        totals = defaultdict(lambda: {"delta": Decimal("0"), "payable_after": Decimal("0")})
        for line in lines:
            if line["creator"] is None:
                continue
            key = f"{line['campaign']}|{line['creator']}"
            totals[key]["delta"] += Decimal(line["delta"])
            totals[key]["payable_after"] += Decimal(line["payable_after"])
        return {key: {**values, "campaign": key.split("|")[0],
                      "creator": key.split("|")[1],
                      "delta": money_str(values["delta"]),
                      "payable_after": money_str(values["payable_after"])}
                for key, values in sorted(totals.items())}

    def _digest(self, body):
        return hashlib.sha256(encode(body).encode("utf-8")).hexdigest()

    def get(self, settlement_id):
        row = self.connection.execute(
            "SELECT * FROM settlements WHERE settlement_id=?", (settlement_id,)).fetchone()
        if row is None:
            return None
        body = json.loads(row["body"])
        lines = self.connection.execute(
            "SELECT * FROM settlement_lines WHERE settlement_id=? ORDER BY line_no",
            (settlement_id,)).fetchall()
        body["digest"] = row["digest"]
        body["stored_lines"] = [
            {key: line[key] for key in line.keys() if key not in ("settlement_id", "line_no")}
            | {"evidence": json.loads(line["evidence"])}
            for line in lines]
        return body

    def list_periods(self):
        rows = self.connection.execute(
            "SELECT settlement_id,period,watermark,sealed_at,digest FROM settlements "
            "ORDER BY sealed_at").fetchall()
        return [dict(row) for row in rows]

    def verify(self, settlement_id):
        """重新计算摘要哈希，回查已封存结算依据是否被改动。"""
        row = self.connection.execute(
            "SELECT body,digest FROM settlements WHERE settlement_id=?",
            (settlement_id,)).fetchone()
        if row is None:
            raise ValueError("结算单不存在")
        actual = hashlib.sha256(row["body"].encode("utf-8")).hexdigest()
        return {"settlement_id": settlement_id, "intact": actual == row["digest"],
                "digest": row["digest"]}

    def order_timeline(self, order_id):
        """一笔订单跨期间的完整回查链：当前结论 + 每次结算/调整记录。"""
        rows = self.connection.execute(
            """SELECT s.period, s.sealed_at, sl.* FROM settlement_lines sl
               JOIN settlements s ON s.settlement_id=sl.settlement_id
               WHERE sl.order_id=? ORDER BY s.sealed_at, sl.line_no""",
            (order_id,)).fetchall()
        return {
            "order_id": order_id,
            "current": self.ledger.decision(order_id),
            "history": self.ledger.history(order_id)["versions"],
            "resolutions": self.ledger.resolution_history(order_id),
            "settlements": [{key: row[key] for key in row.keys()
                             if key not in ("evidence",)}
                            | {"evidence": json.loads(row["evidence"])} for row in rows],
        }
