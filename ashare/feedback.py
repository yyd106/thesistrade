"""Private strategy feedback; never consumed as market evidence or execution instructions."""
import re
import uuid
import json
from .storage import now

TOPICS={'strategy','data','experience','other'}
STATUSES={'NEW','REVIEWING','ADOPTED','DISMISSED'}


def submit(store,config,user,data):
    page=data.get('page');symbol=data.get('symbol');body=data.get('body','');nickname=data.get('nickname','')
    topic=data.get('topic','strategy');request_id=data.get('request_id','')
    if page not in ('home','stock'):raise ValueError('意见须来自首页或股票详情。')
    if page=='stock' and symbol not in {w['symbol'] for w in config['watchlist']}:raise ValueError('请选择当前自选股。')
    if page=='home':symbol=None
    if not isinstance(body,str) or not 3<=len(body.strip())<=4000:raise ValueError('意见请填写3至4000个字符。')
    if not isinstance(nickname,str) or len(nickname)>40:raise ValueError('昵称不超过40个字符。')
    if topic not in TOPICS:raise ValueError('请选择有效的意见分类。')
    if not isinstance(request_id,str) or not re.fullmatch(r'[a-zA-Z0-9-]{16,80}',request_id):raise ValueError('提交标识无效，请刷新页面。')
    study_id=data.get('study_id')
    if study_id and (page!='stock' or not store.db.execute('SELECT 1 FROM studies WHERE id=? AND symbol=?',(study_id,symbol)).fetchone()):raise ValueError('研究版本与股票不匹配。')
    store.db.execute('BEGIN IMMEDIATE')
    try:
        old=store.db.execute('SELECT id FROM feedback WHERE username=? AND request_id=?',(user['username'],request_id)).fetchone()
        if old:store.db.commit();return {'id':old[0],'reused':True}
        at=now();iid=uuid.uuid4().hex
        store.db.execute('INSERT INTO feedback VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (iid,request_id,user['username'],nickname.strip(),page,symbol,study_id,topic,body.strip(),at,'NEW','',at))
        store.db.commit();return {'id':iid,'reused':False}
    except BaseException:store.db.rollback();raise


def listing(store,status=None,symbol=None,offset=0):
    if status and status not in STATUSES:raise ValueError('筛选状态无效。')
    clauses=[];values=[]
    if status:clauses.append('status=?');values.append(status)
    if symbol:clauses.append('symbol=?');values.append(symbol)
    where=' WHERE '+' AND '.join(clauses) if clauses else ''
    total=store.db.execute('SELECT count(*) FROM feedback'+where,values).fetchone()[0]
    rows=[dict(r) for r in store.db.execute('SELECT id,username,nickname,page,symbol,study_id,topic,body,created_at,status,admin_note,updated_at FROM feedback'+where+' ORDER BY created_at DESC,rowid DESC LIMIT 50 OFFSET ?',values+[offset])]
    for row in rows:
        study=store.db.execute('SELECT created_at,result_json FROM studies WHERE id=? AND symbol=?',(row['study_id'],row['symbol'])).fetchone()
        row['study_at']=study['created_at'] if study else None
        report=json.loads(study['result_json']) if study and study['result_json'] else {}
        row['study_analysis']=next((s.get('analysis','') for s in report.get('stocks',[]) if s['symbol']==row['symbol']),'')
    return {'items':rows,'total':total,'offset':offset,'limit':50}


def update(store,data):
    state=data.get('status');note=data.get('admin_note','')
    if state not in STATUSES or not isinstance(note,str) or len(note)>4000:raise ValueError('处理状态或备注无效。')
    with store.db:
        changed=store.db.execute('UPDATE feedback SET status=?,admin_note=?,updated_at=? WHERE id=?',(state,note,now(),data.get('id'))).rowcount
    if not changed:raise ValueError('未找到这条意见。')
    return {'status':'SAVED'}
