"""Narrow evidence-based event rules; unknown events stay explicit requests for evidence."""
import copy
import re
from datetime import date
from decimal import Decimal
from .finance import cents
from .storage import normalize_time
from .calendar import local

RULE_VERSION='cash_dividend_review_v1'

def cash_terms(text,symbol):
    compact=re.sub(r'\s+','',text);missing=[]
    if not re.search(r'证券代码[:：]?'+re.escape(symbol[2:]),compact):missing.append('公告正文中的证券代码须与本股票一致')
    if not re.search(r'不送红股',compact) or not re.search(r'不(?:以|进行|用)?资本公积(?:金)?转增股本',compact):
        missing.append('需明确是否送股或转增；当前自动核验仅支持纯现金分红')
    if re.search(r'差异化|配股|外币|取消(?:本次)?(?:权益|分红)',compact):missing.append('存在差异化、配股、外币或取消安排，需补充对应处理规则')
    amounts=set(re.findall(r'每10股派(?:发|送)现金红利([0-9]+(?:\.[0-9]+)?)元[（(]含税',compact))
    values={Decimal(x)*10 for x in amounts};cash=None
    if len(values)==1:
        value=next(iter(values))
        if value>0 and value==value.to_integral_value():cash=int(value)
    if cash is None:missing.append('需取得唯一明确的每10股税前现金分红金额（支持精确到每股分）')
    def get_date(label):
        found=set()
        for y,m,d in re.findall(label+r'(?:及现金红利发放日)?(?:为)?[:：]?(\d{4})年(\d{1,2})月(\d{1,2})日',compact):
            try:found.add(date(int(y),int(m),int(d)).isoformat())
            except ValueError:pass
        return next(iter(found)) if len(found)==1 else None
    record=get_date('股权登记日');ex=get_date('(?:除权除息日|除息日)')
    if not record:missing.append('需取得唯一的股权登记日')
    if not ex:missing.append('需取得唯一的除权除息日')
    if record and ex and record>=ex:missing.append('登记日与除息日的先后顺序不一致')
    return {'cash_per_share_cents':cash,'record_date':record,'ex_date':ex},missing

def evaluate(store,symbol,documents,chunks,features,at):
    from .materiality import document_policy
    reviews=[];adjustments=[];u=copy.deepcopy((features or {}).get('unadjusted') or {})
    for doc in documents:
        policy=document_policy(doc)
        if policy['level'] not in ('CORPORATE_ACTION','RISK'):continue
        result={'doc_id':doc['id'],'title':doc['title'],'url':doc['url'],'content_hash':doc['content_hash'],
            'rule_version':RULE_VERSION,'checked_at':at,'status':'NEEDS_EVIDENCE','missing':[],
            'evidence_ids':[c['id'] for c in chunks.get(doc['id'],[])], 'facts':{}}
        if policy['level']=='RISK':
            result['missing']=['需取得此事项的最新进展或正式结论原文，并确认是否仍有赔偿、冻结、处罚或经营影响；当前通用风险事项不能仅凭标题或已读状态自动解除']
        elif doc['source']!='cninfo' or doc['kind']!='company_report' or not doc['cloud_allowed']:
            result['missing']=['需要成功取得巨潮原始公告正文；标题、摘要或未经来源核对的导入文本不能自动核验']
        elif not re.search(r'权益分派实施|分红派息实施',doc['title']) or re.search(r'更正|取消|调整',doc['title']):
            result['missing']=['需要最终实施公告；方案、预案、更正或取消公告还需核对版本关系']
        else:
            text='\n'.join(c['text'] for c in chunks.get(doc['id'],[]))
            terms,missing=cash_terms(text,symbol);result['facts']=terms;result['missing']=missing
            if not missing:
                ex=terms['ex_date'];record=terms['record_date'];bars=u.get('bars',[])
                if ex>local(at).date().isoformat():missing.append('除息日尚未到达，不提前确认未来实施结果')
                if not u.get('last_complete_date') or u['last_complete_date']<ex:missing.append('等待除息日完成后的完整日线；不提前使用尚未发生的除息调整')
                if not any(b[0]==ex for b in bars) and not (len(bars)>=60 and bars[0][0]>ex):missing.append('日线中尚无除息日记录，需核对停牌、日期与价格来源')
                cutoff=normalize_time(record+'T23:59:59+08:00')
                qty=store.db.execute("SELECT coalesce(sum(CASE side WHEN 'BUY' THEN qty ELSE -qty END),0) FROM paper_fills WHERE symbol=? AND occurred_at<=?",(symbol,cutoff)).fetchone()[0]
                if qty>0:missing.append('策略在登记日持有股票；需先完成现金分红入账和成本核对，当前尚未实现该账务处理')
                if len(bars)<60 or u.get('basis')!='UNADJUSTED':missing.append('需要至少60个完整交易日的原始价格记录')
                if not missing:result['status']='VERIFIED';adjustments.append({**terms,'doc_id':doc['id']})
        reviews.append(result)
    dates=[a['ex_date'] for a in adjustments]
    if len(dates)!=len(set(dates)):
        for r in reviews:
            if r['status']=='VERIFIED':r['status']='NEEDS_EVIDENCE';r['missing'].append('同一除息日存在多份实施公告，需核对是否重复或修订')
        adjustments=[]
    if adjustments:
        original=copy.deepcopy(u)
        try:
            prices=[]
            for bar in u['bars']:
                value=cents(bar[2])-sum(a['cash_per_share_cents'] for a in adjustments if bar[0]<a['ex_date'])
                if value<=0:raise ValueError('调整后价格无效')
                bar[2]=str(Decimal(value)/100);prices.append(value)
            u.update(ma20_cents=sum(prices[-20:])//20,ma60_cents=sum(prices[-60:])//60,
                close_cents=prices[-1],basis='CASH_DIVIDEND_ADJUSTED',corporate_actions=adjustments,
                original_ma20_cents=original['ma20_cents'],original_ma60_cents=original['ma60_cents'])
        except (ValueError,KeyError,TypeError):
            u=original
            for r in reviews:
                if r['status']=='VERIFIED':r['status']='NEEDS_EVIDENCE';r['missing'].append('现金分红后的可比价格计算未通过，需核对日线')
    return reviews,u
