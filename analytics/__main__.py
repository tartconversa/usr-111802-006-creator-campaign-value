"""本地演练入口：导入样本并输出活动归因、待核与结算分析。"""
import argparse
import json
from pathlib import Path
from .service import AnalyticsService


def main():
    parser = argparse.ArgumentParser(description="运营分析服务")
    parser.add_argument("--database", required=True)
    parser.add_argument("--fixture", default="fixtures/campaign_sample.json")
    args = parser.parse_args()
    sample = json.loads(Path(args.fixture).read_text(encoding="utf-8"))
    service = AnalyticsService(args.database)
    try:
        for subject in sample["entities"]:
            service.register(**subject)
        for alias in sample.get("aliases", []):
            service.link(**alias)
        for campaign in sample.get("campaigns", []):
            service.register_campaign(**campaign)
        for enrollment in sample.get("enrollments", []):
            service.enroll(**enrollment)
        for event in sample["events"]:
            service.ingest(event)

        if "campaigns" not in sample:
            print(json.dumps(service.summary(), ensure_ascii=False, sort_keys=True))
            return

        service.recompute_attribution(sample.get("recomputed_at"))
        print("== 活动分析（封存前） ==")
        print(json.dumps(service.campaign_analysis(), ensure_ascii=False,
                         sort_keys=True, indent=2))
        print("== 待核工作台 ==")
        print(json.dumps(service.pending_reviews(), ensure_ascii=False, indent=2))

        for resolution in sample.get("resolutions", []):
            service.resolve_review(**resolution)

        for settlement in sample.get("settlements", []):
            sealed = service.seal_settlement(**settlement)
            print(f"== 已封存结算单 {sealed['settlement_id']}（{sealed['period']}） ==")
            print(json.dumps(sealed, ensure_ascii=False, sort_keys=True, indent=2))

        for trace_order in sample.get("trace_orders", []):
            print(f"== 订单回查 {trace_order} ==")
            print(json.dumps(service.order_trace(trace_order), ensure_ascii=False,
                             sort_keys=True, indent=2))
    finally:
        service.close()


if __name__ == "__main__":
    main()
