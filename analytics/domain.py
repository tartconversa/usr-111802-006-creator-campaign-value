"""创作者参加活动与结算比例的现有合同。"""
from decimal import Decimal
class CampaignContracts:
    def __init__(self, connection):
        self.connection = connection
        connection.execute("CREATE TABLE IF NOT EXISTS creators(campaign TEXT, creator TEXT, share TEXT, PRIMARY KEY(campaign,creator))")

    def enroll(self, campaign, creator, share):
        value = Decimal(str(share))
        if not 0 <= value <= 1: raise ValueError("结算比例超出范围")
        self.connection.execute("INSERT OR REPLACE INTO creators VALUES(?,?,?)", (campaign,creator,str(value)))

    def share(self, campaign, creator):
        row = self.connection.execute("SELECT share FROM creators WHERE campaign=? AND creator=?", (campaign,creator)).fetchone()
        return row[0] if row else None
