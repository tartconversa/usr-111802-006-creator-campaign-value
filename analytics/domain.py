"""创作者参加活动、活动预算与结算比例的合同。"""
from decimal import Decimal


class CampaignContracts:
    def __init__(self, connection):
        self.connection = connection
        connection.execute("""CREATE TABLE IF NOT EXISTS campaigns(
            campaign TEXT PRIMARY KEY, budget TEXT NOT NULL,
            click_window_days INTEGER NOT NULL, exposure_window_days INTEGER NOT NULL)""")
        connection.execute("""CREATE TABLE IF NOT EXISTS creators(
            campaign TEXT, creator TEXT, share TEXT,
            PRIMARY KEY(campaign,creator))""")

    def register_campaign(self, campaign, budget, click_window_days=7, exposure_window_days=1):
        if not isinstance(campaign, str) or not campaign.strip():
            raise ValueError("活动编号不能为空")
        value = Decimal(str(budget))
        if not value.is_finite() or value < 0:
            raise ValueError("活动预算无效")
        if type(click_window_days) is not int or type(exposure_window_days) is not int:
            raise ValueError("回看窗口必须为整数天")
        if click_window_days < 1 or exposure_window_days < 1:
            raise ValueError("回看窗口必须为正整数")
        prior = self.connection.execute(
            "SELECT budget,click_window_days,exposure_window_days FROM campaigns WHERE campaign=?",
            (campaign,)).fetchone()
        if prior:
            if tuple(prior) != (str(value), click_window_days, exposure_window_days):
                raise ValueError("活动已登记为其他预算或窗口")
            return False
        self.connection.execute(
            "INSERT INTO campaigns VALUES(?,?,?,?)",
            (campaign, str(value), click_window_days, exposure_window_days))
        return True

    def campaign(self, campaign):
        row = self.connection.execute(
            "SELECT campaign,budget,click_window_days,exposure_window_days FROM campaigns WHERE campaign=?",
            (campaign,)).fetchone()
        return dict(row) if row else None

    def enroll(self, campaign, creator, share):
        value = Decimal(str(share))
        if not 0 <= value <= 1:
            raise ValueError("结算比例超出范围")
        self.connection.execute("INSERT OR REPLACE INTO creators VALUES(?,?,?)",
                                (campaign, creator, str(value)))

    def share(self, campaign, creator):
        row = self.connection.execute(
            "SELECT share FROM creators WHERE campaign=? AND creator=?",
            (campaign, creator)).fetchone()
        return row[0] if row else None
