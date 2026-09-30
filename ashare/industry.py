"""Evidence-versioned industry hypotheses; membership is not an execution permit."""
from __future__ import annotations
import json
from datetime import datetime, timedelta
from decimal import Decimal
from .storage import digest, normalize_time, now

VERSION = 'industry_v1'
DOMAINS = {
    'ai': {'name': 'AI 算力基础设施', 'terms': ['数据中心', '液冷', '光模块', '服务器']},
    'power': {'name': '电力与能源基础设施', 'terms': ['变压器', '电网', '输配电', '储能']},
    'robotics': {'name': '机器人与工业自动化', 'terms': ['机器人', '减速器', '伺服', '工业自动化']},
    'semiconductor': {'name': '半导体设备材料与先进封装', 'terms': ['半导体', '先进封装', '晶圆', '光刻']},
    'medical': {'name': '医疗设备与医院采购', 'terms': ['医疗设备', '医疗器械', '医院', '诊断设备']},
}
METHODS = {'1': '二级供应链传导', '2': '扩张中的瓶颈', '3': '资本开支顺序', '8': '政策到实际采购'}
STAGES = ('POLICY','BUDGET','FUNDING','TENDER','AWARD','CONTRACT','DELIVERY','ACCEPTANCE','PAYMENT','CANCELLED')
SCHEMA = '''
INSERT OR IGNORE INTO metadata VALUES('industry_schema_version','1');
CREATE TABLE IF NOT EXISTS industry_hypotheses(
 id TEXT PRIMARY KEY, thesis_key TEXT NOT NULL, previous_id TEXT REFERENCES industry_hypotheses(id),
 symbol TEXT NOT NULL, domain TEXT NOT NULL, method TEXT NOT NULL, created_at TEXT NOT NULL,
 review_at TEXT NOT NULL, expires_at TEXT NOT NULL, state TEXT NOT NULL, fingerprint TEXT NOT NULL,
 payload_json TEXT NOT NULL, UNIQUE(thesis_key,fingerprint));
CREATE INDEX IF NOT EXISTS industry_thesis_versions ON industry_hypotheses(thesis_key,created_at);
CREATE INDEX IF NOT EXISTS industry_company ON industry_hypotheses(symbol,created_at);
CREATE TABLE IF NOT EXISTS industry_facts(
 id TEXT PRIMARY KEY, hypothesis_id TEXT NOT NULL REFERENCES industry_hypotheses(id),
 kind TEXT NOT NULL, entity TEXT NOT NULL, product TEXT NOT NULL, project_id TEXT NOT NULL,
 lot TEXT NOT NULL, metric TEXT NOT NULL, unit TEXT NOT NULL, period TEXT NOT NULL,
 doc_id TEXT NOT NULL REFERENCES documents(id), chunk_id TEXT NOT NULL REFERENCES chunks(id),
 ready_at TEXT NOT NULL, payload_json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS industry_project ON industry_facts(project_id,lot,metric,ready_at);
CREATE TABLE IF NOT EXISTS industry_memberships(
 id INTEGER PRIMARY KEY, symbol TEXT NOT NULL, at TEXT NOT NULL, membership TEXT NOT NULL,
 tier TEXT NOT NULL, buy_eligible INTEGER NOT NULL, fingerprint TEXT NOT NULL, payload_json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS industry_member_lookup ON industry_memberships(symbol,at);
CREATE TABLE IF NOT EXISTS industry_coverage(
 id INTEGER PRIMARY KEY, cycle_id TEXT NOT NULL, domain TEXT NOT NULL, source TEXT NOT NULL,
 started_at TEXT NOT NULL, finished_at TEXT NOT NULL, status TEXT NOT NULL, detail TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS industry_steps(
 cycle_id TEXT NOT NULL, step TEXT NOT NULL, status TEXT NOT NULL, updated_at TEXT NOT NULL,
 payload_json TEXT NOT NULL, PRIMARY KEY(cycle_id,step));
CREATE TABLE IF NOT EXISTS industry_forecasts(
 id TEXT PRIMARY KEY, hypothesis_id TEXT NOT NULL REFERENCES industry_hypotheses(id), symbol TEXT NOT NULL,
 created_at TEXT NOT NULL, due_at TEXT NOT NULL, payload_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS industry_outcomes(
 id TEXT PRIMARY KEY, forecast_id TEXT NOT NULL REFERENCES industry_forecasts(id), checked_at TEXT NOT NULL,
 status TEXT NOT NULL, payload_json TEXT NOT NULL);
'''


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def latest(store, at, symbol=None):
    rows=store.db.execute('''SELECT h.* FROM industry_hypotheses h WHERE h.created_at<=?
        AND NOT EXISTS(SELECT 1 FROM industry_hypotheses n WHERE n.thesis_key=h.thesis_key
        AND n.created_at<=? AND (n.created_at>h.created_at OR (n.created_at=h.created_at AND n.rowid>h.rowid)))''',(at,at))
    return [{**dict(r), 'payload': json.loads(r['payload_json'])} for r in rows if symbol is None or r['symbol']==symbol]


def evidence_current(store, h, at):
    ids={f['doc_id'] for f in h['payload']['facts']}
    current={d['id'] for d in store.documents_as_of(at) if d['id'] in ids}
    return ids==current


def state(store, h, at):
    if h['state']!='ACTIVE':return h['state']
    if at>=h['expires_at']:return 'ARCHIVED'
    if at>=h['review_at'] or not evidence_current(store,h,at):return 'REVIEW'
    return 'ACTIVE'


def save(store, proposal, at, identities):
    """Only grounded fields can meet a method's preliminary admission gate.

    Validation checks provenance, chronology and shape; extracted meaning remains a
    model interpretation and is shown as such, never as independently audited fact.
    """
    at=normalize_time(at);p=json.loads(encoded(proposal))
    if p['domain'] not in DOMAINS or p['method'] not in METHODS:raise ValueError('未知产业链或方法')
    spec=identities.get(p['symbol'])
    if not spec or spec.get('kind')!='STOCK' or spec.get('category') not in ('CN','US'):raise ValueError('证券身份未核验')
    for key in ('topic','thesis','next_check','invalidation'):
        if not isinstance(p.get(key),str) or not 1<=len(p[key])<=1200:raise ValueError('缺少假设字段 '+key)
    if not 1<=len(p.get('causal_chain',[]))<=6 or not p.get('counterpoints'):raise ValueError('须保留因果路径和反证')
    if p.get('state') not in ('ACTIVE','WAITING','INVALIDATED','REALIZED'):raise ValueError('假设状态错误')
    if not 1<=len(p.get('facts',[]))<=24:raise ValueError('须提供有界原文证据')
    docs={d['id']:d for d in store.documents_as_of(at)};facts=[]
    for f in p['facts']:
        c=store.db.execute('SELECT * FROM chunks WHERE id=?',(f['evidence_id'],)).fetchone()
        d=docs.get(c['doc_id']) if c else None
        quote=f.get('quote','')
        if not d or not d['cloud_allowed'] or not 4<=len(quote)<=500 or quote not in c['text']:raise ValueError('引用不是当时可用的原文')
        if d['kind'] in ('announcement_metadata','news_brief') or d['extraction_quality'] in ('metadata_only','ocr_required'):raise ValueError('只有标题或待核验正文不能形成事实')
        if f['kind'] not in ('RELATION','METRIC','MILESTONE','EXPOSURE','COUNTEREVIDENCE'):raise ValueError('事实类型错误')
        if f['claim_type'] not in ('DISCLOSED','GUIDANCE','ESTIMATE'):raise ValueError('必须区分披露、指引和估计')
        for key in ('entity','counterparty','product','project','owner','lot','metric','unit','period','value'):
            if not isinstance(f.get(key),str) or len(f[key])>600:raise ValueError('事实字段错误 '+key)
        if not f['entity']:f['entity']='UNKNOWN'
        if not f['period']:f['period']='UNKNOWN'
        if f['kind']=='MILESTONE' and f['metric'] not in STAGES:raise ValueError('项目节点无效')
        for k in ('effective_from','effective_until'):
            if f.get(k):f[k]=normalize_time(f[k])
        if f.get('effective_from') and f.get('effective_until') and f['effective_from']>=f['effective_until']:raise ValueError('业务有效期错误')
        f.update(doc_id=d['id'],ready_at=d['ready_at'],published_at=d['published_at'],acquired_at=d['first_seen_at'],
                 page=c['page'],url=d['url'],source=d['source'],quality=d['extraction_quality'],family_id=d['family_id'])
        f['project_id']=digest(encoded([f['owner'].strip(),f['project'].strip()]))[:24] if f['project'] and f['owner'] else ''
        original=store.db.execute('SELECT min(d.published_at) FROM chunks c JOIN documents d ON d.id=c.doc_id JOIN document_meta m ON m.doc_id=d.id WHERE instr(c.text,?)>0 AND m.ready_at<=? AND d.available_at<=?',(quote,at,at)).fetchone()[0]
        f['original_published_at']=original or d['published_at']
        facts.append(f)
    p['facts']=facts;p['name']=spec['name'];p['category']=spec['category'];p['rule_version']=VERSION
    usable=[f for f in facts if f['entity']!='UNKNOWN' and f['period']!='UNKNOWN' and (f['kind']!='RELATION' or f['counterparty'] and f['product']) and f['claim_type']=='DISCLOSED' and (not f.get('effective_from') or f['effective_from']<=at) and (not f.get('effective_until') or at<f['effective_until'])]
    gaps=list(p.get('missing',[]));method=p['method']
    if not any(f['entity'] in (p['symbol'],spec['name']) or f['counterparty'] in (p['symbol'],spec['name']) or docs[f['doc_id']]['symbol']==p['symbol'] for f in usable):gaps.append('证据尚未关联到该上市主体')
    if method=='1':
        edges=[f for f in usable if f['kind']=='RELATION']
        if not any(a is not b and a['counterparty']==b['entity'] and b['counterparty'] in (p['symbol'],spec['name']) for a in edges for b in edges):gaps.append('两跳供货链尚未由原文连接')
        if not any(f['kind']=='EXPOSURE' for f in usable):gaps.append('公司业务敞口待核实')
    if method=='2':
        metrics={f['metric'] for f in usable if f['kind']=='METRIC'}
        if not metrics&{'DEMAND','ORDERS'} or not metrics&{'CAPACITY','OUTPUT','LEAD_TIME','INVENTORY'}:gaps.append('需求与供给约束缺少配对证据')
        if not p.get('alternatives') or not p.get('profit_capture'):gaps.append('替代供给或利润归属待核实')
    if method in ('3','8'):
        milestones=[f for f in usable if f['kind']=='MILESTONE' and f['project_id']]
        projects={f['project_id'] for f in milestones}
        funded={'BUDGET','FUNDING'};procured={'TENDER','AWARD','CONTRACT','DELIVERY','ACCEPTANCE','PAYMENT'}
        if not any(any(f['project_id']==k and f['metric'] in funded for f in milestones) and any(f['project_id']==k and f['metric'] in procured for f in milestones) for k in projects):gaps.append('同一项目资金与采购节点未核实')
        if any(f['metric']=='CANCELLED' for f in milestones):gaps.append('项目存在取消证据')
    gate_gaps=gaps[len(p.get('missing',[])):]
    if p['state']=='ACTIVE' and gate_gaps:p['state']='WAITING'
    if any(f['entity']=='UNKNOWN' or f['period']=='UNKNOWN' for f in facts):gaps.append('部分事实的业务主体或期间尚未披露，未用于初步准入')
    p['missing']=list(dict.fromkeys(gaps));p['admission_gaps']=gate_gaps
    if method=='1':p['revenue_scenario']=revenue_scenario(facts)
    # Same event on another website does not renew its business clock. A genuine
    # new disclosure can advance it; rerunning a model on old text cannot.
    anchor=max(f['original_published_at'] for f in facts if f['claim_type']!='ESTIMATE') if any(f['claim_type']!='ESTIMATE' for f in facts) else min(f['original_published_at'] for f in facts)
    review=normalize_time((datetime.fromisoformat(anchor)+timedelta(days=7)).isoformat())
    review=min([review]+[f['effective_until'] for f in usable if f.get('effective_until')])
    expires=normalize_time((datetime.fromisoformat(anchor)+timedelta(days=30)).isoformat())
    key=digest(encoded([p['symbol'],p['domain'],method,p['topic'].strip()]))[:24]
    previous=next((h for h in latest(store,at) if h['thesis_key']==key),None)
    fp=digest(encoded(p));hid=digest(key+fp)[:24]
    existing=store.db.execute('SELECT id FROM industry_hypotheses WHERE thesis_key=? AND fingerprint=?',(key,fp)).fetchone()
    if existing:return existing[0]
    with store.db:
        store.db.execute('INSERT INTO industry_hypotheses VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(hid,key,previous['id'] if previous else None,p['symbol'],p['domain'],method,at,review,expires,p['state'],fp,encoded(p)))
        for i,f in enumerate(facts):
            store.db.execute('INSERT INTO industry_facts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(hid+':'+str(i),hid,f['kind'],f['entity'],f['product'],f['project_id'],f['lot'],f['metric'],f['unit'],f['period'],f['doc_id'],f['evidence_id'],f['ready_at'],encoded(f)))
        for i,f in enumerate(p.get('forecasts',[])):
            due=normalize_time(f['due_at'])
            if due<=at or not f['metric'] or not f['baseline'] or not f['unit'] or not f['period']:raise ValueError('经营预测须有基线、口径及未来期限')
            low,high=Decimal(f['low']),Decimal(f['high'])
            if not low.is_finite() or not high.is_finite() or low>high:raise ValueError('预测范围错误')
            fid=digest(encoded([key,f['metric'],f['unit'],f['period'],due]))[:24]
            store.db.execute('INSERT OR IGNORE INTO industry_forecasts VALUES(?,?,?,?,?,?)',(fid,hid,p['symbol'],at,due,encoded(f)))
    return hid


def links(store,at):
    """Adapter into the existing global allocator; never consumes a second seat."""
    groups={}
    for h in latest(store,at):
        p=h['payload'];s=state(store,h,at);symbol=h['symbol']
        item=groups.setdefault(symbol,{'asset':symbol,'symbol':symbol,'name':p['name'],'category':p['category'],'kind':'STOCK','unit':'元' if p['category']=='CN' else '美元','added_at':h['created_at'],'updated_at':h['created_at'],'links':[],'status':'WATCHING','strength':'MEDIUM','direction':'UP','conflicting':False,'indicator':None,'indicator_status':'NOT_CONNECTED'})
        # The allocator's seven-day review uses the publication anchor, not extraction time.
        anchor=max(f['original_published_at'] for f in p['facts'] if f['claim_type']!='ESTIMATE') if any(f['claim_type']!='ESTIMATE' for f in p['facts']) else min(f['original_published_at'] for f in p['facts'])
        item['links'].append({'event_id':'industry:'+h['id'],'headline':p['thesis'],'published_at':anchor,'catalyst_at':anchor,'qualification_review_at':h['review_at'],'url':p['facts'][0]['url'],'source':'公司及项目披露','horizon':'MONTHS','theme':DOMAINS[h['domain']]['name'],'status':'TRACKING' if s in ('ACTIVE','REVIEW','WAITING') else 'INVALIDATED','industry_state':s,'method':h['method'],
          'impact':{'direction':'UP','strength':'MEDIUM','logic_chain':[{'kind':'FACT' if i==0 else 'INFERENCE','statement':v,'news_id':'','quote':p['facts'][0]['quote'] if i==0 else ''} for i,v in enumerate(p['causal_chain'])],'conditions':[p['next_check']],'invalidation':p['invalidation']},
          'materiality':{'method_gate':True,'admitted':s=='ACTIVE','state':'ADMITTED' if s=='ACTIVE' else 'PENDING','reason':'产业方法初步证据核验','assessment':{'magnitude':'MEDIUM','direction':'UP'}}})
    return groups


def context(store,symbol,at):
    return [{k:h[k] for k in ('id','method','domain','review_at','expires_at')} | {'state':state(store,h,at),'thesis':h['payload']['thesis'],'next_check':h['payload']['next_check'],'invalidation':h['payload']['invalidation'],'missing':h['payload']['missing'],'counterpoints':h['payload']['counterpoints'],'facts':h['payload']['facts'],'forecasts':h['payload'].get('forecasts',[])} for h in sorted(latest(store,at,symbol),key=lambda h:h['created_at'],reverse=True)[:8]]


def scenario(parameters):
    """Decimal interval product, with explicit missing inputs; never a point estimate."""
    keys=('incremental_units','content_per_unit','unit_price','supplier_share')
    if any(parameters.get(k) is None for k in keys):return {'status':'MISSING','missing':[k for k in keys if parameters.get(k) is None]}
    low=high=Decimal(1)
    for k in keys:
        a,b=map(Decimal,parameters[k])
        if not a.is_finite() or not b.is_finite() or not 0<=a<=b or k=='supplier_share' and b>1:raise ValueError('情景参数范围错误')
        low*=a;high*=b
    return {'status':'SCENARIO','low':str(low),'high':str(high),'formula':'新增数量 × 单位用量 × 单价 × 供货份额','assumptions':parameters}


def view(store,config,at):
    from .universe import membership
    rows=membership(store,config,at)
    forecasts=[]
    for r in store.db.execute('SELECT * FROM industry_forecasts ORDER BY created_at DESC LIMIT 100'):
        outcome=store.db.execute('SELECT * FROM industry_outcomes WHERE forecast_id=? ORDER BY checked_at DESC,rowid DESC LIMIT 1',(r['id'],)).fetchone()
        forecasts.append({**dict(r),'payload':json.loads(r['payload_json']),'outcome':dict(outcome) if outcome else None})
    hypotheses=[]
    for h in latest(store,at):
        versions=[{'id':r['id'],'at':r['created_at'],'state':r['state'],'thesis':json.loads(r['payload_json'])['thesis']} for r in store.db.execute('SELECT id,created_at,state,payload_json FROM industry_hypotheses WHERE thesis_key=? AND created_at<=? ORDER BY created_at DESC,rowid DESC LIMIT 10',(h['thesis_key'],at))]
        hypotheses.append({**{k:v for k,v in h.items() if k!='payload_json'},'effective_state':state(store,h,at),'versions':versions})
    return {'version':VERSION,'enabled':bool(config.get('industry_enabled')),'domains':DOMAINS,'methods':METHODS,'members':rows,
      'hypotheses':hypotheses,
      'checks':[{**dict(r),'payload':json.loads(r['payload_json'])} for r in store.db.execute('SELECT s.* FROM industry_steps s WHERE NOT EXISTS(SELECT 1 FROM industry_steps n WHERE n.step=s.step AND n.updated_at>s.updated_at) ORDER BY updated_at DESC LIMIT 5')],
      'forecasts':forecasts,'coverage':[dict(r) for r in store.db.execute('SELECT * FROM industry_coverage ORDER BY id DESC LIMIT 30')],
      'storage':'本机 SQLite 与内容哈希原文档案；云端仅同步摘要及资格','history':[dict(r) for r in store.db.execute('SELECT symbol,at,membership,tier,buy_eligible FROM industry_memberships ORDER BY id DESC LIMIT 50')]}


def evaluate(store,at):
    """Forward operating outcomes, separate from security price performance."""
    results=[]
    for row in store.db.execute('SELECT * FROM industry_forecasts WHERE due_at<=?',(at,)).fetchall():
        prediction=json.loads(row['payload_json']);observed=[]
        for h in latest(store,at,row['symbol']):
            for f in h['payload']['facts']:
                if (f['claim_type']=='DISCLOSED' and f['ready_at']>row['created_at'] and f['metric']==prediction['metric'] and f['unit']==prediction['unit'] and f['period']==prediction['period'] and f['entity'] in (row['symbol'],h['payload']['name'])):
                    try:
                        number=Decimal(f['value'])
                        if number.is_finite():observed.append({'value':str(number),'doc_id':f['doc_id'],'evidence_id':f['evidence_id']})
                    except Exception:continue
        values={Decimal(o['value']) for o in observed}
        status='UNKNOWN' if not values else 'CONFLICT' if len(values)>1 else 'IN_RANGE' if Decimal(prediction['low'])<=next(iter(values))<=Decimal(prediction['high']) else 'OUTSIDE_RANGE'
        payload={'prediction':prediction,'observations':observed,'notice':'尚未取得可比经营披露不记作预测失败；价格表现另行评估'}
        oid=digest(encoded([row['id'],status,observed]))[:24]
        with store.db:store.db.execute('INSERT OR IGNORE INTO industry_outcomes VALUES(?,?,?,?,?)',(oid,row['id'],at,status,encoded(payload)))
        results.append({'forecast_id':row['id'],'status':status})
    return results


def revenue_scenario(facts):
    """Only dimensionally compatible, explicitly sourced intervals can be multiplied."""
    import re
    mapping={'incremental_units':('INCREMENTAL_UNITS','台'),'content_per_unit':('CONTENT_PER_UNIT','件/台'),'unit_price':('UNIT_PRICE','元/件'),'supplier_share':('SUPPLIER_SHARE','比例')}
    parameters={};sources={}
    for key,(metric,unit) in mapping.items():
        matches=[f for f in facts if f['metric']==metric and f['unit']==unit and f['claim_type']!='ESTIMATE']
        if len(matches)!=1:continue
        f=matches[0];m=re.fullmatch(r'(\d+(?:\.\d+)?)(?:\s*[-~至]\s*(\d+(?:\.\d+)?))?',f['value'])
        if not m:continue
        parameters[key]=[m[1],m[2] or m[1]];sources[key]={'evidence_id':f['evidence_id'],'period':f['period'],'claim_type':f['claim_type']}
    if len({s['period'] for s in sources.values()})>1:return {'status':'MISSING','missing':['参数期间不一致，不计算收入'],'sources':sources}
    return {**scenario(parameters),'sources':sources,'unit':'元','notice':'条件情景，不等于已确认收入；收入归属、成本和兑现时间另行核验'}
