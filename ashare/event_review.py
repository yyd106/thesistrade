"""Narrow evidence-based event rules; unknown events stay explicit requests for evidence.

Only a pure cash dividend is verified automatically, and only from the exchange's final
implementation announcement. A wrong pass is worse than a wrong block: a pass removes a buying
restriction and shifts the price history by the announced amount. So every rule below fails closed.

Rule v2 (0.15.3):

- Pages are rebuilt from their overlapping chunks, and whitespace is removed except between two
  numbers, so a table date is never glued to the next date or to a page number.
- Both exchange layouts are read: the SSE "相关日期" table with a per-share amount, and the SZSE
  sentence with a per-10-share amount. Sub-cent amounts stay exact. Any other per-share cash amount
  in the text (a special dividend, an adjusted amount) blocks.
- Explicit answers are read ("差异化分红送转：否"). Stock distributions and rights issues are
  recognised by what a real one must state (ratio, new-share listing, changed share count) in any
  sentence that does not open with a condition. Shares said not to take part, treasury shares
  mentioned anywhere (unless explicitly none), a dividend base that differs from the total share
  count, or a reference price recomputed over the total share count all count as a differentiated
  dividend.
- The ex-date must be the trading day after the record date, and when a quote from the ex-date
  exists, the exchange's reference price must match the announced amount.
- An announcement that only follows from a dividend (buyback price cap, conversion price,
  conversion suspension, reminder) is linked to that dividend's implementation announcement. Its
  own text is checked too, and a problem found there also blocks the dividend.

Differentiated dividends, stock distributions, rights issues, foreign-currency payouts and
cancellations stay unsupported. Every review that does not pass carries a resolution:
UNSUPPORTED (a rule is missing), EVIDENCE (an official text or data is missing), LINKED (waits for
the dividend it follows from) or WAIT (a date has not been reached).
"""
import copy
import re
import unicodedata
from datetime import date, datetime
from decimal import Decimal, ROUND_FLOOR
from .finance import cents
from .storage import normalize_time, CHUNK_STEP
from .calendar import local, trading_day, previous_trading_day
from .materiality import DIVIDEND_IMPLEMENTATION_ADDED
from .dividends import held_at_record

RULE_VERSION='cash_dividend_review_v2.2'
RESOLUTIONS=('UNSUPPORTED','EVIDENCE','LINKED','WAIT')  # when several apply, the first listed is reported

# PDF fonts sometimes yield Kangxi radicals (⽇ for 日) or full-width digits; fold only those.
FOLD={cp:unicodedata.normalize('NFKC',chr(cp)) for lo,hi in ((0x2E80,0x2FDF),(0xFF10,0xFF19),(0xFF21,0xFF3A),(0xFF41,0xFF5A))
      for cp in range(lo,hi+1) if unicodedata.normalize('NFKC',chr(cp))!=chr(cp)}

DATE=r'(?:(?<![\d/])(\d{4})/(\d{1,2})/(\d{1,2})(?![\d/])|(\d{4})年(\d{1,2})月(\d{1,2})日)'
RECORD=re.compile(r'股权登记日(?:及现金红利发放日)?(?:为|是)?[:：]?[（(]?'+DATE)
EX=re.compile(r'(?:除权除息日|除息日|除权[（(]息[）)]日)(?:为|是)?[:：]?[（(]?'+DATE)
OTHER_CLASS=re.compile(r'[HB]股')  # an H- or B-share date is not the A-share date
# SSE: 股份类别 | 股权登记日 | 最后交易日 | 除权（息）日 | 现金红利发放日, then the A股 row. The
# last trading day only applies to B shares and is a dash (sometimes dropped) for A shares.
TABLE=re.compile(r'股权登记日最后交易日除权[（(]息[）)]日现金红利发放日(?:A股)?')
CELL_DATE=re.compile(' ?'+DATE)
CELL_DASH=re.compile(r' ?[－\-—–―─]+')

AMOUNT=r'([0-9]+(?:\.[0-9]+)?)'
PER_SHARE=(re.compile(r'每股派(?:发)?(?:现金红利|现金股利)(?:人民币)?'+AMOUNT+r'元(?:人民币)?[（(]含税'),
           re.compile(r'A股每股现金红利(?:人民币)?'+AMOUNT+r'元'))
PER_TEN=(re.compile(r'每10股派(?:发)?(?:现金红利|现金股利|现金)?(?:人民币)?'+AMOUNT+r'元(?:人民币)?(?:现金)?(?:红利)?[（(]含税'),)
# Any cash amount paid per share or per ten shares, whatever the wording; must agree with the one above.
PAY=r'(?:派|发放|分配|分派|支付)'
ANY_ONE=re.compile(r'每股[^，,。；;]{0,4}?'+PAY+r'[^，,。；;]{0,16}?'+AMOUNT+'元')
ANY_TEN=re.compile(r'每(?:10|十)股[^，,。；;]{0,4}?'+PAY+r'[^，,。；;]{0,16}?'+AMOUNT+'元')
# An amount stated right after the words; after "=" a formula continues with other prices
# (e.g. the old buyback price cap), so an equals sign is not a statement of the amount.
STATED=re.compile(r'每股(?:现金)?(?:红利|股利|分红)(?:金额)?(?:为)?(?:人民币)?([0-9]+\.[0-9]+)元')
AFTER_TAX=re.compile(r'扣税后|税后|实际派发|扣缴|代扣|补缴')

# Sentences end at full stops and bullets, and before a numbered item when the stop is missing.
SENTENCE=re.compile(r'[。；;●■◆]|(?<![0-9])(?=[0-9]{1,2}、)|(?=[一二三四五六七八九十]{1,3}、)|(?=[（(][一二三四五六七八九十0-9]{1,2}[）)])')
LEAD=re.compile(r'^(?:[（(]?[0-9一二三四五六七八九十]{1,2}[）)、.．]|[①-⑳]|[、，,：:])+')
# Only a sentence that opens with a hypothetical marker is a condition; a condition in its tail is not.
OPENS_CONDITIONAL=re.compile(r'若|如果|如在|如遇|如有|如发生|如公司|如本|倘若|假设|假如')
NUM=r'[0-9一二三四五六七八九十]'
STOCK=re.compile(r'每(?:10|十)?股(?:派[^，,。；;]{0,20}?)?(?:送红股|送股|派送|送|转增|以资本公积金?转增)(?:股份)?'+NUM+
                 r'|(?:送红股|送股|转增|派送股票股利)[0-9,]+(?:\.[0-9]+)?股|新增无限售条件流通股份|股本(?:结构)?变动(?:情况)?表|股份变动(?:情况)?表'
                 r'|(?:分红|分配|送转|转增)后(?:公司)?总股本(?:将)?(?:增至|增加|变更为|为)[0-9]|所送[（(]?转?[）)]?股|送[（(]转[）)]股|送转股份|红股上市|每股转增比例|每股送股比例')
RIGHTS=re.compile(r'本次配股|配股(?:股权)?登记日|配股价格为?[0-9]|每(?:10|十)?股配(?:售)?'+NUM+r'|配股缴款|配股(?:股份)?上市')
HALT=r'(?:取消|终止|撤销|暂缓|延期|推迟|中止)'
CANCELLED=re.compile(HALT+r'(?:实施)?(?:本次|此次)?(?:的)?(?:权益分派|利润分配|利润分派|分红|派息)')
DIFF_LABEL=re.compile(r'(?:是否涉及)?差异化分红(?:送转)?[:：]?(是|否)')
DIFF_NEGATED=re.compile(r'(?:不涉及|不存在|未涉及|不适用|无)差异化分红(?:送转)?')
# Any shares said not to take part in this distribution: no ordinary pure-cash template says so.
EXCLUDED_SHARES=re.compile(r'(?:不|无权|无法)(?:参与|享有|参加|纳入)[^，,。；;]{0,10}?(?:利润|权益)?(?:分配|分派|分红)|不予分配')
# SZSE has no label. Shares held for a buyback do not take part, and the exchange then prices the
# ex-date from the cash spread over all shares; any of these wordings means that.
# The company holding its own shares, not investors holding them ("对于持有本公司股份的QFII" in every tax section).
TREASURY=re.compile(r'回购(?:专用|专户|账户|证券账户)|已回购|回购的股份|库存股|公司(?:通过[^，,。；;]{0,12}?)?(?:持有|所持)的?本公司(?:股份|股票)'
                    r'|自有股份|购回的股份'
                    r'|(?:剔除|扣除|扣减|减去|不含|不包括)(?:公司)?(?:已)?回购')
# A buyback notice always names the buyback account; there only a positive holding counts.
HELD=re.compile(r'(?:已回购|累计回购|回购专用证券账户(?:中)?(?:持有|的股份)|库存股)[^，,。；;]{0,16}?(?<![0-9,.])[1-9][0-9,]*股')
NO_TREASURY=re.compile(r'(?:剔除|扣除|扣减|减去)?(?:公司)?(?:已)?回购(?:专用证券账户|专用账户|专户)?(?:中|内)?(?:的|持有的|持有|持股)?(?:的)?(?:股份|股票)?(?:数量)?(?:为)?(?<![0-9,.])0股'
                       r'|回购(?:专用证券账户|专用账户|专户)(?:中|内)?(?:未|没有|不)持有|回购(?:专用证券账户|专用账户|专户)(?:中|内)?(?:无|没有)(?:股份|股票|持股)')
# The ex-date priced from cash spread over all shares (currency conversion of H/B payouts is not this).
REPRICED=re.compile(r'按[^，,。；;]{0,12}?总股本[^，,。；;]{0,8}?(?:折算|摊薄)|总股本为基数(?:折算|摊薄)'
                    r'|分摊到每一股|市值不变|虚拟分派|除权(?:除息|[（(]息[）)])参考价|除权除息价格计算|应以[0-9.]+元')
FOREIGN=re.compile(r'港币|港元|美元|外币|HKD|USD|HK\$',re.I)
BASE=re.compile(r'([0-9][0-9,]{3,})股为(?:分配|分红|派息)?基数')
TOTAL=re.compile(r'总股本(?:为)?([0-9][0-9,]{3,})股')

# An implementation announcement: the wording of rule v2.1, plus the wording added in 0.15.5 for
# titles such as "2026年中期利润分派A股实施公告" (the same addition materiality recognises).
FINAL=re.compile(r'(?:权益分派|分红派息|利润分配|现金分红|现金红利|股息|红利)(?:方案)?(?:的)?(?:派发)?实施|'+DIVIDEND_IMPLEMENTATION_ADDED)
PREFERRED=re.compile(r'优先股')  # a preferred-share dividend does not move the common stock
NOT_FINAL=re.compile(r'更正|取消|调整|补充|修订|修正|更新|终止|暂缓|延期|推迟|中止|重新|再次')
REVISION=re.compile(r'更正|补充|修订|修正|更新|重新|再次')
CANCEL_TITLE=re.compile(HALT)
DIVIDEND_TITLE=re.compile(r'权益分派|分红|派息|利润分配|利润分派|股息|除权除息')
FOLLOW_ON=re.compile(r'(?:调整|修正)[^，,。]{0,12}?(?:回购(?:股份)?价格|转股价格|行权价格|授予价格|认购价格)'
                     r'|(?:回购(?:股份)?价格(?:上限)?|转股价格|行权价格|授予价格)[^，,。]{0,6}?(?:调整|修正)|(?:停止|暂停|恢复)转股|转股(?:连续)?停牌'
                     r'|提示性公告|英文|摘要')
PERIOD=re.compile(r'(\d{4}年.{0,10}?)(?:权益分派|利润分配|利润分派|分红派息|现金分红|现金红利|分红|派息|股息|红利)')
LINK_DAYS=15  # a follow-on without dates links to the one implementation published this close to it
NEAR_DAYS=30  # two implementations this close together are treated as one distribution announced twice

DIFFERENTIATED=('差异化分红：回购专户等股份不参与分配，交易所按虚拟分派或按总股本折算的每股现金计算除权除息参考价，'
                '不能直接用每股现金调整均线；当前核验规则未支持，继续阻挡买入')
DIFFERENTIATED_BASE=('分配基数与公告中的总股本不一致，通常是剔除了回购专户股份，属于差异化分红；'
                     '当前核验规则未支持，继续阻挡买入')
DIFFERENTIATED_UNCLEAR='公告出现差异化分红的表述，但没有读到“差异化分红送转：否”这样的明确答案；当前核验规则未支持差异化分红，继续阻挡买入'


def tight(text):
    """Remove whitespace, except that two numbers stay separated by one space."""
    text=(text or '').translate(FOLD)
    return ' '.join(re.sub(r'\s+','',part) for part in re.split(r'(?<=\d)\s+(?=\d)',text))


def document_text(chunks):
    """Rebuild each page from its overlapping chunks, then join the pages. Plain concatenation
    repeats the overlap and splits a date or an amount at every chunk edge."""
    pages=[];parts=[];page=None;last=''
    for c in sorted(chunks,key=lambda c:c.get('ordinal') or 0):
        text=c['text'];overlap=len(last)-CHUNK_STEP  # the last window of a page can be shorter
        if parts and c.get('page')==page and overlap>0 and last[CHUNK_STEP:]==text[:overlap]:
            parts.append(text[overlap:])
        else:
            if parts:pages.append(''.join(parts))
            parts=[text];page=c.get('page')
        last=text
    if parts:pages.append(''.join(parts))
    return '\n'.join(pages)


def sentences(t):
    """Sentences that state something; those opening with a condition are hypothetical."""
    return [s for s in SENTENCE.split(t) if s and not OPENS_CONDITIONAL.match(LEAD.sub('',s))]


def _day(groups):
    y,m,d=groups[:3] if groups[0] else groups[3:6]
    try:return date(int(y),int(m),int(d)).isoformat()
    except (TypeError,ValueError):return None


def _table_dates(t):
    found=[]
    for header in TABLE.finditer(t):
        first=CELL_DATE.match(t,header.end())
        if not first:continue
        record=_day(first.groups());pos=first.end()
        dash=CELL_DASH.match(t,pos)
        if dash:
            ex=CELL_DATE.match(t,dash.end())
            if ex and CELL_DATE.match(t,ex.end()):found.append((record,_day(ex.groups())))
            continue
        rest=[]
        while len(rest)<3:
            m=CELL_DATE.match(t,pos)
            if not m:break
            rest.append(_day(m.groups()));pos=m.end()
        if len(rest)==2:found.append((record,rest[0]))  # dash dropped: 登记日, 除权（息）日, 发放日
    return found


def _labelled(pattern,t):
    return {_day(m.groups()) for m in pattern.finditer(t) if not OTHER_CLASS.search(t[max(0,m.start()-8):m.start()])}


def dividend_dates(t):
    """The one A-share record date and ex-date the text states, from sentences and SSE tables;
    None when none or several are found."""
    table=_table_dates(t)
    records=_labelled(RECORD,t)|{r for r,_ in table}
    exs=_labelled(EX,t)|{e for _,e in table}
    records.discard(None);exs.discard(None)
    return (next(iter(records)) if len(records)==1 else None),(next(iter(exs)) if len(exs)==1 else None)


def pretax(t):
    """Each sentence without its after-tax part: from an after-tax keyword to the end of the
    enclosing parenthesis, or to the end of the sentence outside parentheses."""
    out=[]
    for s in SENTENCE.split(t):
        kept=[];pos=0
        while True:
            m=AFTER_TAX.search(s,pos)
            if not m:
                kept.append(s[pos:]);break
            kept.append(s[pos:m.start()])
            depth=sum(ch in '（(' for ch in s[:m.start()])-sum(ch in '）)' for ch in s[:m.start()])
            if depth<=0:break
            i=m.end()
            while i<len(s) and depth>0:
                depth+=(s[i] in '（(')-(s[i] in '）)');i+=1
            pos=i
        out.append(''.join(kept))
    return out


def per_share_cash(t):
    """Pre-tax cash per share in yuan (exact), from per-share or per-10-share statements."""
    pre=pretax(t)
    values={Decimal(x) for s in pre for p in PER_SHARE for x in p.findall(s)}|{Decimal(x)/10 for s in pre for p in PER_TEN for x in p.findall(s)}
    value=next(iter(values)) if len(values)==1 else None
    return (value if value is not None and 0<value<100 else None),values


def stated_amounts(t):
    """Every pre-tax per-share RMB cash amount the text states, whatever the wording."""
    found=set()
    for s in pretax(t):
        for pattern,scale in ((ANY_ONE,1),(ANY_TEN,10),(STATED,1))+tuple((p,1) for p in PER_SHARE)+tuple((p,10) for p in PER_TEN):
            found|={Decimal(m.group(1))/scale for m in pattern.finditer(s) if not FOREIGN.search(m.group(0))}
    return found


def other_amounts(t,cash):
    """Per-share cash amounts that differ from the verified one."""
    return sorted(v for v in stated_amounts(t) if v!=cash)


def differentiated(t,follow=False):
    """A follow-on (e.g. a buyback price-cap notice) names the buyback account by nature, so there
    only a positive holding counts; an implementation announcement may not mention it at all."""
    answers=set(DIFF_LABEL.findall(t))
    rest=DIFF_NEGATED.sub('',DIFF_LABEL.sub('',t))
    excluded=any(EXCLUDED_SHARES.search(s) and not re.search(r'[HB]股|港股',s) for s in SENTENCE.split(t))
    if '是' in answers or REPRICED.search(t) or excluded:return DIFFERENTIATED
    # Over the whole text: a conditional or factual wrapper around a treasury holding changes nothing.
    if (HELD if follow else TREASURY).search(NO_TREASURY.sub('',t)):return DIFFERENTIATED
    bases={b.replace(',','') for b in BASE.findall(t)};totals={x.replace(',','') for x in TOTAL.findall(t)}
    if bases and totals and not bases<=totals:return DIFFERENTIATED_BASE
    if '差异化' in rest:return DIFFERENTIATED_UNCLEAR
    return None


def content_problems(t,follow=False):
    """What in the text rules out a pure cash dividend, as (resolution, message) pairs."""
    problems=[];firm=sentences(t)
    if any(STOCK.search(s) for s in firm):problems.append(('UNSUPPORTED','本次分配包含送股或资本公积金转增；当前核验规则只支持纯现金分红，送转未支持'))
    if any(RIGHTS.search(s) for s in firm):problems.append(('UNSUPPORTED','公告写有配股安排；当前核验规则未支持配股'))
    if any('外币' in s and not re.search('[HB]股',s) for s in firm):problems.append(('UNSUPPORTED','涉及外币派发；当前核验规则未支持'))
    if any(CANCELLED.search(s) for s in firm):problems.append(('EVIDENCE','公告涉及取消、终止、暂缓或延期本次分红，需核对最新公告'))
    diff=differentiated(t,follow)
    if diff:problems.append(('UNSUPPORTED',diff))
    return problems


def calendar_problem(record,ex):
    if trading_day(record) is None or trading_day(ex) is None:
        # Years before the stored calendar: only a weak check is possible.
        gap=(date.fromisoformat(ex)-date.fromisoformat(record)).days
        if not 1<=gap<=12 or date.fromisoformat(ex).weekday()>=5:
            return f'读到的登记日{record}与除息日{ex}相隔{gap}天，不像相邻交易日，日期可能读错；当前核验规则未支持这种情况'
        return None
    if not trading_day(ex) or previous_trading_day(ex)!=record:
        return f'读到的登记日{record}与除息日{ex}不满足“除息日是登记日后的第一个交易日”，日期可能读错；当前核验规则未支持这种情况'
    return None


def _yuan(value):
    return format(value.normalize(),'f')


def code_matches(t,symbol):
    return bool(re.search(r'(?:(?:证券|股票|A股)代码|(?:Stock|Securities|A-?Share)Code)[:：]?'+re.escape(symbol[2:]),t,re.I))


def cash_terms(text,symbol):
    """Facts of a pure cash dividend, and the problems that stop automatic verification as
    (resolution, message) pairs."""
    t=tight(text);problems=[]
    if not code_matches(t,symbol):problems.append(('EVIDENCE','公告正文中的证券代码须与本股票一致'))
    problems+=content_problems(t)
    cash,seen=per_share_cash(t)
    if cash is None:
        problems.append(('UNSUPPORTED',('读到多个不同的税前每股现金分红：'+'、'.join(sorted(_yuan(v) for v in seen))+' 元' if len(seen)>1 else
             '未读出税前每股现金分红')+'；支持“每股派发现金红利X元（含税）”“A股每股现金红利X元”“每10股派X元（含税）”，正文已取得时属于解析规则未支持'))
    else:
        other=other_amounts(t,cash)
        if other:
            problems.append(('UNSUPPORTED','正文还写有其他每股派现金额（'+'、'.join(_yuan(v) for v in other)+
                             ' 元），可能是特别分红或调整后的金额；当前核验规则未支持'))
    record,ex=dividend_dates(t)
    if not record:problems.append(('UNSUPPORTED','未读出唯一的股权登记日；支持“股权登记日为X年X月X日”及上交所“相关日期”表格，正文已取得时属于解析规则未支持'))
    if not ex:problems.append(('UNSUPPORTED','未读出唯一的除权除息日；支持“除权除息日为X年X月X日”及上交所“相关日期”表格，正文已取得时属于解析规则未支持'))
    if record and ex:
        problem=calendar_problem(record,ex)
        if problem:problems.append(('UNSUPPORTED',problem))
    fen=cash*100 if cash is not None else None
    terms={'cash_per_share':_yuan(cash) if cash is not None else None,
           'cash_per_share_cents':int(fen) if fen is not None and fen==fen.to_integral_value() else None,
           'record_date':record,'ex_date':ex}
    return terms,problems


def follow_on(title):
    """An announcement that only follows from a dividend, e.g. the buyback price cap adjusted after it.
    A cancellation, postponement or revision is never one."""
    title=tight(title)
    return bool(DIVIDEND_TITLE.search(title) and FOLLOW_ON.search(title) and not CANCEL_TITLE.search(title) and not REVISION.search(title))


def period(title):
    """The distribution period a title names, normalised (2025年年度 = 2025年度, 年中期 = 年半年度);
    None when it names none."""
    m=PERIOD.search(tight(title).replace('A股',''))
    if not m:return None
    p=m.group(1)
    for old,new in (('年度中期','年中期'),('年年度','年度'),('年度末期','年度'),('年末期','年度'),('上半年','半年度'),('前三季度','三季度'),
                    ('第一季度','一季度'),('第三季度','三季度'),('首次','第一次')):
        p=p.replace(old,new)
    p=re.sub(r'第([1-9])次',lambda m:'第'+'一二三四五六七八九'[int(m.group(1))-1]+'次',p)
    return re.sub(r'年中期$','年半年度',p)


def same_distribution(a,b,halt=False):
    """Whether two titles may name one distribution; a title without a period may name any.
    For a halt or revision (halt=True) it fails closed: within one year every distribution other
    than the annual one may be the one meant (a postponement of "2026年中期分红" must reach
    "2026年第一次中期权益分派实施公告"). Duplicates and follow-on links keep exact periods, so two
    interim dividends of one year stay two distributions."""
    pa,pb=period(a),period(b)
    if pa is None or pb is None or pa==pb:return True
    if not halt:return False
    annual=lambda p:p[4:]=='年度'
    return pa[:4]==pb[:4] and annual(pa)==annual(pb)


def _close(result,problems):
    result['missing']=[m for _,m in problems]
    kinds={k for k,_ in problems}
    result['resolution']=next((k for k in RESOLUTIONS if k in kinds),None)


def _block(result,kind,message):
    result['status']='NEEDS_EVIDENCE';result['missing'].append(message)
    kinds={kind,result['resolution']}-{None}
    result['resolution']=next(k for k in RESOLUTIONS if k in kinds)


def reference_problem(store,symbol,terms,bars,at):
    """The exchange publishes the ex-date's reference price (the quote's previous close that day).
    It must equal the prior close minus the cash, unless the source did not adjust it at all."""
    ex=terms['ex_date'];prior=[b for b in bars if b[0]==terms['record_date']]
    if not prior:return None
    try:
        row=store.db.execute('SELECT prev_close_cents FROM quotes WHERE symbol=? AND observed_at>=? AND observed_at<=? AND first_seen_at<=? AND prev_close_cents>0 ORDER BY observed_at LIMIT 1',
                             (symbol,normalize_time(ex+'T09:30:00+08:00'),normalize_time(ex+'T15:00:00+08:00'),at)).fetchone()
    except Exception:
        return None
    if not row:return None
    before=Decimal(cents(prior[-1][2]));expected=before-Decimal(terms['cash_per_share'])*100;reference=Decimal(row[0])
    # The exchange rounds prior close minus cash to the cent; a differentiated dividend uses a smaller virtual amount.
    if abs(reference-expected)<=Decimal('0.5') or reference==before:return None
    return (f'除息日交易所参考价 {_yuan(reference/100)} 元，与前收盘 {_yuan(before/100)} 元减去每股现金 {terms["cash_per_share"]} 元不符，'
            '可能另有送转、特别分红或日期读错；当前核验规则未支持')


def _timing(store,symbol,terms,u,today,at,credits=False):
    """Conditions outside the announcement: the date has passed, bars exist, the market priced the
    ex-date as announced, and shares held at the record date can be accounted for: only a ledger
    that credits cash dividends (0.15.5 cloud, or a standalone install) can hold them."""
    problems=[];ex=terms['ex_date'];record=terms['record_date'];bars=u.get('bars',[])
    if ex>today:problems.append(('WAIT','除息日尚未到达，不提前确认未来实施结果'))
    if not u.get('last_complete_date') or u['last_complete_date']<ex:
        problems.append(('WAIT','等待除息日完成后的完整日线；不提前使用尚未发生的除息调整'))
    elif not any(b[0]==ex for b in bars) and not (len(bars)>=60 and bars[0][0]>ex):
        # Only once the ex-date's bar is due; before that its absence is the wait above.
        problems.append(('EVIDENCE','日线中尚无除息日记录，需核对停牌、日期与价格来源'))
    if held_at_record(store,symbol,record)>0 and not credits:problems.append(('UNSUPPORTED','策略在登记日持有股票；需先完成现金分红入账和成本核对，当前尚未实现该账务处理'))
    if len(bars)<60 or u.get('basis')!='UNADJUSTED':problems.append(('EVIDENCE','需要至少60个完整交易日的原始价格记录'))
    if (trading_day(ex) is None or trading_day(record) is None) and bars and ex>=bars[0][0]:
        problems.append(('UNSUPPORTED','交易日历未覆盖登记日或除息日所在年份，不能核对日期后调整价格'))
    if not problems:
        mismatch=reference_problem(store,symbol,terms,bars,at)
        if mismatch:problems.append(('UNSUPPORTED',mismatch))
    return problems


def _floor(value):
    return int(value.to_integral_value(rounding=ROUND_FLOOR))


def _after(a,b,low,high):
    """Whether b falls between `low` and `high` days after a; False when either is missing."""
    try:days=(datetime.fromisoformat(b.replace('Z','+00:00'))-datetime.fromisoformat(a.replace('Z','+00:00'))).total_seconds()/86400
    except (AttributeError,TypeError,ValueError):return False
    return low<=days<=high


def evaluate(store,symbol,documents,chunks,features,at,credits=False):
    from .materiality import document_policy
    u=copy.deepcopy((features or {}).get('unadjusted') or {})
    results={};mains={};attached=[];revised=[];today=local(at).date().isoformat()
    for doc in documents:
        policy=document_policy(doc)
        if policy['level'] not in ('CORPORATE_ACTION','RISK'):continue
        title=tight(doc['title'])
        result={'doc_id':doc['id'],'title':doc['title'],'url':doc['url'],'content_hash':doc['content_hash'],
            'rule_version':RULE_VERSION,'checked_at':at,'status':'NEEDS_EVIDENCE','missing':[],'resolution':None,
            'evidence_ids':[c['id'] for c in chunks.get(doc['id'],[])],'facts':{}}
        results[doc['id']]=result
        if policy['level']=='RISK':
            problems=[('UNSUPPORTED','需取得此事项的最新进展或正式结论原文，并确认是否仍有赔偿、冻结、处罚或经营影响；当前通用风险事项不能仅凭标题或已读状态自动解除')]
        elif doc['source']!='cninfo' or doc['kind']!='company_report' or not doc['cloud_allowed']:
            problems=[('EVIDENCE','需要成功取得巨潮原始公告正文；标题、摘要或未经来源核对的导入文本不能自动核验')]
        elif PREFERRED.search(title) and not re.search(r'权益分派|分红派息|除权|除息',title):
            problems=[('EVIDENCE','优先股股息公告不影响普通股价格，但当前规则不自动核验，需确认与本股票的关系')]
        elif follow_on(title):
            attached.append(doc);continue
        elif not FINAL.search(title) or NOT_FINAL.search(title):
            problems=[('EVIDENCE','需要最终实施公告；方案、预案、更正、补充或取消公告还需核对版本关系')]
            if CANCEL_TITLE.search(title) or REVISION.search(title):revised.append(doc['title'])
        else:
            terms,problems=cash_terms(document_text(chunks.get(doc['id'],[])),symbol);result['facts']=terms
            mains[doc['id']]=doc
            if not problems:
                problems=_timing(store,symbol,terms,u,today,at,credits)
                if not problems:result['status']='VERIFIED'
        _close(result,problems)
    main_results=[results[i] for i in mains]
    # Follow-on announcements: link each to its dividend. A problem in its own text blocks both.
    links={}
    for doc in attached:
        result=results[doc['id']];t=tight(document_text(chunks.get(doc['id'],[])))
        record,ex=dividend_dates(t)
        result['facts']={'follows':'DIVIDEND','record_date':record,'ex_date':ex}
        if ex or record:
            linked=[r for r in main_results if (ex and r['facts'].get('ex_date')==ex) or (not ex and r['facts'].get('record_date')==record)]
        else:
            # A notice without dates comes before (or with) the implementation it announces.
            linked=[results[i] for i,d in mains.items() if same_distribution(d['title'],doc['title'])
                    and _after(doc.get('published_at'),d.get('published_at'),-2,LINK_DAYS)]
        content=content_problems(t,follow=True)
        own=([] if code_matches(t,symbol) or re.search(r'英文|English',doc['title']) else [('EVIDENCE','公告正文中的证券代码须与本股票一致')])+content
        if len(linked)==1:
            main=linked[0];links[doc['id']]=main;result['facts']['linked_doc_id']=main['doc_id']
            cash=Decimal(main['facts']['cash_per_share']) if main['facts'].get('cash_per_share') else None
            differs=sorted(v for v in stated_amounts(t) if cash is not None and v!=cash)
            reveal=list(content)
            if differs:
                reveal.append(('UNSUPPORTED','本公告写明的每股现金（'+'、'.join(_yuan(v) for v in differs)+
                               f" 元）与实施公告的 {main['facts']['cash_per_share']} 元不一致，可能是差异化分红或金额调整；当前核验规则未支持"))
                own.append(reveal[-1])
            for kind,message in reveal[:1]:  # what the follow-on reveals about the dividend blocks it too
                _block(main,kind,f"随附公告《{doc['title']}》：{message}")
        _close(result,own)
    for title in revised:
        # A cancellation, postponement or revision of the same distribution (or of an unnamed one)
        # voids the dividend's facts.
        for r in main_results:
            if same_distribution(title,r['title'],halt=True):
                _block(r,'EVIDENCE',f'存在同一次分配的取消、暂缓、更正或补充公告《{title}》，需核对最新版本')
    verified=[r for r in main_results if r['status']=='VERIFIED']
    repeated=set()
    for i,a in enumerate(verified):
        for b in verified[i+1:]:
            gap=abs((date.fromisoformat(a['facts']['ex_date'])-date.fromisoformat(b['facts']['ex_date'])).days)
            if gap<NEAR_DAYS or same_distribution(a['title'],b['title']):repeated|={a['doc_id'],b['doc_id']}
    for r in verified:
        # Two announcements for one distribution: a revision or a duplicate, never two adjustments.
        if r['doc_id'] in repeated:_block(r,'EVIDENCE','同一次分配可能存在多份实施公告（分配期相同，或除息日相隔不到30天），需核对是否重复或修订')
    adjustments=[{**r['facts'],'doc_id':r['doc_id']} for r in main_results if r['status']=='VERIFIED']
    if adjustments:
        original=copy.deepcopy(u)
        try:
            prices=[]
            for bar in u['bars']:
                shift=sum((Decimal(a['cash_per_share'])*100 for a in adjustments if bar[0]<a['ex_date']),Decimal(0))
                value=Decimal(cents(bar[2]))-shift
                if value<=0:raise ValueError('调整后价格无效')
                bar[2]=_yuan(value/100);prices.append(value)
            u.update(ma20_cents=_floor(sum(prices[-20:])/20),ma60_cents=_floor(sum(prices[-60:])/60),
                close_cents=_floor(prices[-1]),basis='CASH_DIVIDEND_ADJUSTED',corporate_actions=adjustments,
                original_ma20_cents=original['ma20_cents'],original_ma60_cents=original['ma60_cents'])
        except (ValueError,KeyError,TypeError,ArithmeticError):
            u=original
            for r in main_results:
                if r['status']=='VERIFIED':_block(r,'EVIDENCE','现金分红后的可比价格计算未通过，需核对日线')
    # A follow-on takes the final state of its dividend.
    for doc in attached:
        result=results[doc['id']]
        if result['missing']:continue
        main=links.get(doc['id'])
        facts=result['facts']
        if not main:
            if not facts['record_date'] and not facts['ex_date']:
                _close(result,[('UNSUPPORTED',f'本公告随分红实施而发布（如调整回购价格上限、暂停转股），但没有读出对应的股权登记日或除息日，{LINK_DAYS}天内也没有唯一一份实施公告可以关联；当前核验规则未支持')])
            else:
                _close(result,[('EVIDENCE',f"本公告随分红实施而发布，需先取得除息日{facts['ex_date'] or '（未读出）'}、登记日{facts['record_date'] or '（未读出）'}对应的唯一一份权益分派实施公告")])
        elif main['status']=='VERIFIED':
            result['status']='VERIFIED'
        else:
            _close(result,[('LINKED',f"本公告随分红实施而发布，本身不改变股价序列；随除息日{main['facts'].get('ex_date') or '（未读出）'}的实施公告一并核验，该实施公告尚未通过")])
    return [results[d['id']] for d in documents if d['id'] in results],u


def resolution(review):
    """How an unverified review is resolved. Reviews written before rule v2 carry no resolution and
    are classified from their wording, as the follow-up list did before."""
    if not review or review.get('status')=='VERIFIED':return None
    if review.get('resolution'):return review['resolution']
    missing='；'.join(review.get('missing',[]))
    if re.search(r'尚未实现|未支持|处理规则|账务|通用风险事项',missing):return 'UNSUPPORTED'
    if re.search(r'尚未到达|等待除息日',missing):return 'WAIT'
    return 'EVIDENCE'


# A title that reads like a distribution being paid out but was not recognised as a corporate action;
# listed by the replay so an unrecognised wording is noticed (中国移动's "利润分派A股实施" was one).
PAYOUT_WORDS=re.compile(r'分红|派息|股息|红利|利润分[配派]|权益分派|送股|转增|除权|除息')
PAYOUT_VERBS=re.compile(r'实施|派发|发放')
NOT_PAYOUT=re.compile(r'预案|提议|议案|说明会|决议|法律意见|核查意见|独立意见|审核意见|规划|回报|承诺|差别化|实施后|子公司|参股|H股|B股|英文')


def unrecognised(store,symbol,coverage):
    found=[]
    for m in coverage:
        title=tight(m['title'])
        if m['importance']=='CORPORATE_ACTION' or not PAYOUT_WORDS.search(title) or not PAYOUT_VERBS.search(title) or NOT_PAYOUT.search(title):continue
        row=store.db.execute('SELECT symbol FROM documents WHERE id=?',(m['doc_id'],)).fetchone()
        if row and row[0]==symbol:found.append({'doc_id':m['doc_id'],'title':m['title'],'importance':m['importance']})
    return found


def check(store,config,symbols=None,at=None):
    """Replay the current rule over each stock's announcements, exactly as the next research would,
    without writing anything. Reports titles, statuses, reasons, dates and amounts; no document text.
    For a dividend on shares the account held at its record date it also shows the cash to be credited."""
    from .research import make_snapshot
    from .storage import now
    from .dividends import supported, amount_cents
    stamp=normalize_time(at or now())
    from .universe import company_targets
    names={w['symbol']:w['name'] for w in company_targets(store,config,stamp)}
    credits=supported(store,config)
    stocks=[]
    for symbol in symbols or list(names):
        item={'symbol':symbol,'name':names.get(symbol,symbol)}
        if symbol not in names:
            stocks.append({**item,'error':'不在自选股内；代码须带 sh 或 sz 前缀，例如 sz000651'});continue
        try:packet=make_snapshot(store,config,symbol,at=stamp,persist=False)
        except Exception as exc:
            stocks.append({**item,'error':str(exc)[:300]});continue
        coverage={m['doc_id']:m for m in packet['mandatory_coverage'] if m['critical']}
        reviews=[r for r in packet['event_reviews'] if coverage.get(r['doc_id'],{}).get('importance')=='CORPORATE_ACTION']
        listed=[]
        for r in reviews:
            entry={'doc_id':r['doc_id'],'title':r['title'],'status':r['status'],'resolution':resolution(r),'missing':r['missing'],'facts':r['facts']}
            facts=r['facts']
            if facts.get('record_date') and facts.get('cash_per_share'):
                qty=held_at_record(store,symbol,facts['record_date'])
                if qty>0:entry['held_at_record']={'qty':qty,'cash_cents':amount_cents(qty,facts['cash_per_share'])}
            listed.append(entry)
        stocks.append({**item,'blocks_buying':any(r['status']!='VERIFIED' for r in reviews),'reviews':listed,
            'unrecognised':unrecognised(store,symbol,packet['mandatory_coverage']),
            'risk_items':sum(1 for m in coverage.values() if m.get('importance')=='RISK')})
    kinds={}
    for s in stocks:
        for r in s.get('reviews',[]):
            key=r['resolution'] or 'VERIFIED';kinds[key]=kinds.get(key,0)+1
    return {'rule_version':RULE_VERSION,'at':stamp,'dividend_credit':credits,
            'summary':{'stocks':len(stocks),'blocked_by_corporate_actions':sum(1 for s in stocks if s.get('blocks_buying')),
                       'reviews':kinds,'errors':sum(1 for s in stocks if 'error' in s),
                       'unrecognised_titles':sum(len(s.get('unrecognised',[])) for s in stocks)},
            'stocks':stocks}
