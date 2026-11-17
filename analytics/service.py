"""协调来源事件、主体目录、现有摘要和报告发布，并提供活动归因与结算口径。"""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from .store import EventStore
from .identity import EntityDirectory
from .metrics import Metrics
from .decisions import DecisionBook
from .domain import CampaignContracts
from .attribution import AttributionLedger
from .settlement import SettlementBook
from .campaign_analytics import CampaignAnalytics


def utcnow_iso():
    return datetime.now(timezone.utc).isoformat()


class AnalyticsService:
    def __init__(self, database, config_path=None):
        if config_path is None:
            config_path = Path(__file__).resolve().parents[1] / "config" / "domain.json"
        self.config = json.loads(Path(config_path).read_text(encoding="utf-8"))
        self.connection = sqlite3.connect(database)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.store = EventStore(self.connection, self.config["event_kinds"])
        self.entities = EntityDirectory(self.connection)
        self.metrics = Metrics(self.store, self.entities)
        self.decisions = DecisionBook(self.connection)
        self.domain = CampaignContracts(self.connection)
        self.ledger = AttributionLedger(self.connection, self.store, self.entities, self.domain)
        self.settlements = SettlementBook(self.connection, self.ledger)
        self.campaigns = CampaignAnalytics(
            self.connection, self.store, self.entities, self.domain,
            self.ledger, self.settlements)
        self.connection.commit()

    def close(self):
        self.connection.close()

    def register(self, entity_id, label, group_id):
        with self.connection:
            return self.entities.register(entity_id, label, group_id)

    def link(self, source, external_id, entity_id):
        with self.connection:
            return self.entities.link(source, external_id, entity_id)

    def ingest(self, event):
        with self.connection:
            return self.store.ingest(event)

    def summary(self, as_of=None):
        return self.metrics.summary(as_of)

    def publish(self, report_id, created_at, title, as_of=None):
        body = {"as_of": as_of, "summary": self.summary(as_of)}
        with self.connection:
            return self.decisions.publish(report_id, created_at, title, body)

    # ------------------------------------------------------------------
    # 活动合同
    # ------------------------------------------------------------------
    def register_campaign(self, campaign, budget, click_window_days=7,
                          exposure_window_days=1):
        with self.connection:
            return self.domain.register_campaign(
                campaign, budget, click_window_days, exposure_window_days)

    def enroll(self, campaign, creator, share):
        with self.connection:
            return self.domain.enroll(campaign, creator, share)

    # ------------------------------------------------------------------
    # 归因：重算、待核裁决、回查
    # ------------------------------------------------------------------
    def recompute_attribution(self, decided_at=None):
        with self.connection:
            return self.ledger.recompute(decided_at or utcnow_iso())

    def campaign_analysis(self, as_of=None):
        return self.campaigns.run(as_of)

    def pending_reviews(self):
        return self.ledger.pending_reviews()

    def resolve_review(self, order_id, resolution, creator=None, note="",
                       decided_at=None):
        with self.connection:
            return self.ledger.resolve_review(
                order_id, resolution, creator, note, decided_at or utcnow_iso())

    def redecide(self, order_id, resolution, creator=None, note="", decided_at=None):
        with self.connection:
            return self.ledger.redecide(
                order_id, resolution, creator, note, decided_at or utcnow_iso())

    def order_trace(self, order_id):
        return self.settlements.order_timeline(order_id)

    def attribution_history(self, order_id):
        return self.ledger.history(order_id)

    # ------------------------------------------------------------------
    # 结算：期间封存、回查与验证
    # ------------------------------------------------------------------
    def seal_settlement(self, settlement_id, period, watermark=None, sealed_at=None):
        return self.settlements.seal(settlement_id, period, watermark, sealed_at)

    def get_settlement(self, settlement_id):
        return self.settlements.get(settlement_id)

    def list_settlements(self):
        return self.settlements.list_periods()

    def verify_settlement(self, settlement_id):
        return self.settlements.verify(settlement_id)
