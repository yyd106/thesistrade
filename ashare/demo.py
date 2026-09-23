"""Synthetic fixtures are isolated from real public-data storage and explicitly labelled."""
from __future__ import annotations
import json
import uuid
from pathlib import Path
from .storage import Store,digest,json_write,normalize_time
from .finance import PaperLedger
from .research import begin_batch,make_snapshot,study,encode
from .slots import run_slot
from .paper import settle,mark_equity,account
from .review import run_review

SYMBOL='sz000333'


def put_quote(store,stamp,price=1000):
    stamp=normalize_time(stamp);row={'symbol':SYMBOL,'name':'合成演示股票（非真实行情）','price_cents':price,
        'prev_close_cents':1000,'observed_at':stamp}
    qid=digest(encode(row))[:24]
    with store.db:store.db.execute('INSERT OR IGNORE INTO quotes VALUES(?,?,?,?,?,?,?,?,?)',
        (qid,SYMBOL,row['name'],price,1000,stamp,stamp,'SYNTHETIC_FIXTURE','synthetic'))
    return qid


def seed(store,config,stamp='2026-09-15T09:00:00+08:00'):
    stamp=normalize_time(stamp);PaperLedger(store).initialize()
    doc,_=store.add_document(symbol=SYMBOL,kind='financial_data',title='合成测试公司报告',source='SYNTHETIC_FIXTURE',
        url='https://example.test/synthetic-report',published_at=stamp,first_seen_at=stamp,ready_at=stamp,
        pages=[(1,'这是一份合成测试资料。经营现金流稳定，专用于验证程序，不是美的集团真实公告。')],raw_path='synthetic',cloud_allowed=True)
    with store.db:store.db.execute('INSERT OR IGNORE INTO fundamental_records VALUES(?,?)',(doc,encode({
        'status':'READY','gaps':[],'latest_period':'2026-06-30','periods':[],'provider':'SYNTHETIC_FIXTURE'})))
    batch,_=begin_batch(store,config)
    checks=[{'source':s,'symbol':SYMBOL,'status':'OK','detail':'SYNTHETIC_FIXTURE','checked_at':stamp}
        for s in ('tencent_daily','cninfo_catalog','cninfo_pdf','financials','market_comparison')]
    with store.db:
        store.db.execute('INSERT INTO batch_stocks VALUES(?,?,?,?,?)',(batch['id'],SYMBOL,stamp,'READY',encode(checks)))
        store.db.execute("UPDATE batches SET status='READY',finished_at=? WHERE id=?",(stamp,batch['id']))
        store.db.execute("UPDATE runs SET status='SUCCEEDED',finished_at=?,as_of=? WHERE id=?",(stamp,stamp,batch['id']))
        store.db.execute('CREATE TABLE IF NOT EXISTS market_features(run_id TEXT,symbol TEXT,created_at TEXT,raw_path TEXT,payload TEXT,PRIMARY KEY(run_id,symbol))')
        u={'last_complete_date':'2026-09-14','ma20_cents':1000,'ma60_cents':900,'close_cents':1000,
            'basis':'UNADJUSTED','raw_path':'SYNTHETIC_FIXTURE','series_hash':'synthetic','bars':[]}
        store.db.execute('INSERT INTO market_features VALUES(?,?,?,?,?)',(batch['id'],SYMBOL,stamp,'synthetic',encode({'unadjusted':u,
            'market_context':{'截至交易日':'2026-09-14','缺口':[],'说明':'SYNTHETIC_FIXTURE：量价及比较条件的合成输入'}})))
    put_quote(store,stamp)
    packet=make_snapshot(store,config,SYMBOL,batch['id'],stamp)
    return packet


def research_model(packet):
    def generate(*args):
        return {'summary':'合成场景：用于验证技术模拟链路。','stocks':[{'symbol':SYMBOL,'action':'WATCH',
            'analysis':'合成资料中的趋势条件成立，仅用于程序测试。','facts':[],
            'counterpoints':['没有投资含义'],'missing_fields':[],'next_checks':['测试后续Slot']}]}
    return generate


def decision_model(action):
    return lambda *args:{'decisions':[{'symbol':SYMBOL,'action':action,'reason':'SYNTHETIC_FIXTURE：测试决策'}]}


def review_model(prompt,*args):
    packet=json.loads(prompt.split('<UNTRUSTED_REVIEW>',1)[1].split('</UNTRUSTED_REVIEW>',1)[0])
    return {'summary':'合成测试复盘：检查买入、T+1阻断与后续卖出链路。','lessons':[],
        'positions':[{'position_key':p['key'],'verdict':'PENDING' if p['research_ids'] else 'INSUFFICIENT',
            'reason':'仅为合成链路测试，不验证投资效果。','supported_points':[],'contradicted_points':[],
            'pending_points':['合成资料无真实经营验证。'],'next_check':'核对测试成交与成本。','research_ids':p['research_ids']}
            for p in packet['portfolio']['positions']]}


def run_demo(config):
    root=Path(config['data_dir'])/'demo'/uuid.uuid4().hex
    cfg={**config,'data_dir':str(root),'watchlist':[{'symbol':SYMBOL,'name':'合成测试股票'}],
        'scheduler_enabled':False,'mode':'paper','paper_max_fill_qty':10000,
        'paper_slippage_bps':0,'paper_max_stock_pct':20,'plan_max_age_hours':24}
    store=Store(root)
    try:
        packet=seed(store,cfg)
        research=study(store,cfg,packet,model_fn=research_model(packet),at='2026-09-15T09:01:00+08:00')
        def refresh(s,c,events):return {'quote_status':'OK','event_status':{SYMBOL:'OK'},'synthetic':True}
        put_quote(store,'2026-09-15T10:00:00+08:00')
        buy=run_slot(store,cfg,'2026-09-15T10:00:00+08:00',refresh_fn=refresh,model_fn=decision_model('BUY'),clock=lambda:'2026-09-15T10:00:00+08:00')
        put_quote(store,'2026-09-15T10:00:10+08:00')
        fills=settle(store,cfg,'2026-09-15T10:00:10+08:00')
        put_quote(store,'2026-09-15T10:30:00+08:00',935)
        t1=run_slot(store,cfg,'2026-09-15T10:30:00+08:00',refresh_fn=refresh,model_fn=decision_model('SELL'),clock=lambda:'2026-09-15T10:30:00+08:00')
        # Next-day research/plan: fixture data explicitly changed; old snapshot stays frozen.
        with store.db:
            r=store.db.execute('SELECT * FROM market_features').fetchone();f=json.loads(r['payload'])
            f['unadjusted']['last_complete_date']='2026-09-15'
            store.db.execute('INSERT INTO market_features VALUES(?,?,?,?,?)',('synthetic-next-day',SYMBOL,normalize_time('2026-09-16T09:00:00+08:00'),'synthetic',encode(f)))
        next_packet=make_snapshot(store,cfg,SYMBOL,at='2026-09-16T09:00:00+08:00')
        study(store,cfg,next_packet,model_fn=research_model(next_packet),at='2026-09-16T09:01:00+08:00')
        put_quote(store,'2026-09-16T10:00:00+08:00',935)
        sell=run_slot(store,cfg,'2026-09-16T10:00:00+08:00',refresh_fn=refresh,model_fn=decision_model('SELL'),clock=lambda:'2026-09-16T10:00:00+08:00')
        put_quote(store,'2026-09-16T10:00:10+08:00',935)
        fills+=settle(store,cfg,'2026-09-16T10:00:10+08:00')
        mark_equity(store,normalize_time('2026-09-16T15:00:00+08:00'))
        rev=run_review(store,cfg,end='2026-09-16T19:30:00+08:00',model_fn=review_model,clock=lambda:'2026-09-16T19:31:00+08:00')
        result={'label':'合成数据集成演示，不是真实行情或AI投资业绩','data_dir':str(root),
            'research':research,'buy_slot':buy,'t_plus_one_slot':t1,'sell_slot':sell,'fills':fills,'review':rev,
            'account':account(store,normalize_time('2026-09-16T19:31:00+08:00'))}
        json_write(root/'demo-result.json',result)
        return result
    finally:store.close()
