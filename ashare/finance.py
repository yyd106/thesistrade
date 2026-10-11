"""只实现独立模拟账本和确定性资金规则，不连接券商。金额全部为整数分。"""
from __future__ import annotations
from decimal import Decimal, ROUND_HALF_UP
from .storage import now, digest


def cents(value):
    d = Decimal(str(value))
    if not d.is_finite():
        raise ValueError("金额必须有限")
    return int((d * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def withdrawal_tier(stage):
    if not isinstance(stage, int) or isinstance(stage, bool) or stage < 1:
        raise ValueError("档位必须为正整数")
    if stage == 1:
        return 15000000, 12000000
    if stage == 2:
        return 20000000, 15000000
    return (300000 + (stage - 3) * 100000) * 100, (200000 + (stage - 3) * 100000) * 100


def withdrawal_plan(equity_cents, stage):
    threshold, retain = withdrawal_tier(stage)
    trigger = equity_cents > threshold
    return {"stage": stage, "threshold_cents": threshold, "retain_cents": retain,
            "triggered": trigger, "planned_cents": equity_cents - retain if trigger else 0}


def total_profit(equity_cents, withdrawn_cents, external_capital_cents):
    return equity_cents + withdrawn_cents - external_capital_cents


def transfer_inside(store, at, amount_cents, reference):
    """Book an already-authorized whole simulated transfer in its caller's lock."""
    from fractions import Fraction
    from .paper import account_inside_transaction
    from . import portfolio_risk
    if not store.db.in_transaction:
        raise RuntimeError('提取记账需要资金事务')
    if not isinstance(amount_cents, int) or isinstance(amount_cents, bool) or amount_cents <= 0:
        raise ValueError('提取金额必须为正整数分')
    before = account_inside_transaction(store, at)
    risk = portfolio_risk.state(store)
    if 'capital_units' not in risk or risk.get('historical_cashflow_gap'):
        raise ValueError('单位净值依据不足')
    if amount_cents > before['cash_cents'] or amount_cents >= before['equity_cents']:
        raise ValueError('模拟可取现金不足')
    units = portfolio_risk.capital_units(risk)
    if units <= 0:
        raise ValueError('单位份额无效')
    portfolio_risk.adjust_withdrawal_inside(store, at, amount_cents)
    identity = digest(reference)
    store.db.execute('INSERT INTO paper_flows VALUES(?,?,?,?,?,?)',
                     (identity, before['id'], 'SIMULATED_WITHDRAWAL', -amount_cents, reference, at))
    store.db.execute('UPDATE paper_accounts SET cash_cents=cash_cents-?,withdrawn_cents=withdrawn_cents+? WHERE id=?',
                     (amount_cents, amount_cents, before['id']))
    after = account_inside_transaction(store, at)
    adjusted = portfolio_risk.state(store)
    if (after['cash_cents'] != before['cash_cents']-amount_cents
            or after['equity_cents'] != before['equity_cents']-amount_cents
            or after['withdrawn_cents'] != before['withdrawn_cents']+amount_cents
            or total_profit(after['equity_cents'], after['withdrawn_cents'], after['initial_cents']) !=
               total_profit(before['equity_cents'], before['withdrawn_cents'], before['initial_cents'])
            or Fraction(before['equity_cents'])/units != Fraction(after['equity_cents'])/portfolio_risk.capital_units(adjusted)
            or adjusted.get('halted') != risk.get('halted') or adjusted.get('peak_unit_nav') != risk.get('peak_unit_nav')):
        raise ValueError('模拟提取对账不一致')
    return {'flow_id': identity, 'amount_cents': amount_cents, 'before_cash_cents': before['cash_cents'],
            'after_cash_cents': after['cash_cents'], 'before_equity_cents': before['equity_cents'],
            'after_equity_cents': after['equity_cents'], 'withdrawn_cents': after['withdrawn_cents']}


class PaperLedger:
    def __init__(self, store):
        self.db = store.db
        self.store = store

    def initialize(self, account="DEMO_PAPER", initial_cents=10000000):
        if not 0 < initial_cents <= 10000000:
            raise ValueError("模拟初始本金必须在0至100000元之间")
        with self.db:
            old = self.db.execute("SELECT * FROM paper_accounts WHERE id=?", (account,)).fetchone()
            if old:
                if old["initial_cents"] != initial_cents:
                    raise ValueError("禁止隐式修改既有模拟本金")
                return
            self.db.execute("INSERT INTO paper_accounts VALUES(?,?,?,1,0)", (account, initial_cents, initial_cents))
            self.db.execute("INSERT INTO paper_flows VALUES(?,?,?,?,?,?)",
                            (digest(account + ":initial"), account, "SIMULATED_INITIAL", initial_cents, account + ":initial", now()))

    def plan(self, equity_cents, account="DEMO_PAPER"):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            a = self.db.execute("SELECT * FROM paper_accounts WHERE id=?", (account,)).fetchone()
            if not a:
                raise ValueError("模拟账户未初始化")
            if self.db.execute('SELECT 1 FROM withdrawal_cycles WHERE account_id=? AND stage=?', (account, a['current_stage'])).fetchone():
                raise ValueError('本档已由管理员模拟提取闭环管理，不能创建旧式计划')
            old = self.db.execute("SELECT * FROM paper_withdrawals WHERE account_id=? AND stage=?", (account, a["current_stage"])).fetchone()
            if old:
                self.db.commit()
                return dict(old)
            p = withdrawal_plan(equity_cents, a["current_stage"])
            if not p["triggered"]:
                self.db.commit()
                return p
            wid = digest(account + ":withdraw:" + str(p["stage"]))[:24]
            self.db.execute("INSERT INTO paper_withdrawals VALUES(?,?,?,?,?,?,?,0,'TRIGGERED',?,NULL)",
                            (wid, account, p["stage"], p["threshold_cents"], p["retain_cents"], equity_cents, p["planned_cents"], now()))
            self.db.commit()
            return dict(self.db.execute("SELECT * FROM paper_withdrawals WHERE id=?", (wid,)).fetchone())
        except BaseException:
            self.db.rollback()
            raise

    def reconcile_simulated_transfer(self, withdrawal_id, amount_cents, reference):
        """仅供隔离测试。只有完整模拟转出对账才推进；重复凭证不重复处理。"""
        if not reference or amount_cents <= 0:
            raise ValueError("需要正数金额及唯一模拟凭证")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            old = self.db.execute("SELECT * FROM paper_flows WHERE reference=?", (reference,)).fetchone()
            w = self.db.execute("SELECT * FROM paper_withdrawals WHERE id=?", (withdrawal_id,)).fetchone()
            if not w:
                raise ValueError("未知提取周期")
            if self.db.execute('SELECT 1 FROM withdrawal_cycles WHERE account_id=? AND stage=?', (w['account_id'], w['stage'])).fetchone():
                raise ValueError('本档已由管理员模拟提取闭环管理，旧测试入口不能记账')
            if old:
                if old["id"] != digest(withdrawal_id + ":" + reference) or old["amount_cents"] != -amount_cents:
                    raise ValueError("凭证重复但内容不一致")
                self.db.commit()
                return
            remaining = w["planned_cents"] - w["transferred_cents"]
            if amount_cents > remaining or w["status"] == "COMPLETED":
                raise ValueError("模拟转出超过未完成计划")
            a = self.db.execute("SELECT * FROM paper_accounts WHERE id=?", (w["account_id"],)).fetchone()
            if amount_cents > a["cash_cents"]:
                raise ValueError("模拟可取现金不足")
            from .portfolio_risk import adjust_withdrawal_inside
            adjust_withdrawal_inside(self.store, now(), amount_cents)
            transferred = w["transferred_cents"] + amount_cents
            complete = transferred == w["planned_cents"]
            self.db.execute("INSERT INTO paper_flows VALUES(?,?,?,?,?,?)", (digest(withdrawal_id + ":" + reference), w["account_id"], "SIMULATED_WITHDRAWAL", -amount_cents, reference, now()))
            self.db.execute("UPDATE paper_accounts SET cash_cents=cash_cents-?,withdrawn_cents=withdrawn_cents+?,current_stage=current_stage+? WHERE id=?",
                            (amount_cents, amount_cents, int(complete), w["account_id"]))
            self.db.execute("UPDATE paper_withdrawals SET transferred_cents=?,status=?,completed_at=? WHERE id=?",
                            (transferred, "COMPLETED" if complete else "WAITING_TRANSFER", now() if complete else None, withdrawal_id))
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
