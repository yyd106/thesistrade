"""Explicit public subscriptions. Publisher reputation is not event materiality."""
from urllib.parse import urlsplit

# (name, URL, source family, coverage, freshness warning after hours)
CATALOG=(
 ('白宫简报与声明','https://www.whitehouse.gov/briefings-statements/feed/','PRIMARY','美国政策声明与总统文字表态',168),
 ('美联储','https://www.federalreserve.gov/feeds/press_monetary.xml','PRIMARY','货币政策',1080),
 ('美联储讲话','https://www.federalreserve.gov/feeds/speeches.xml','PRIMARY','政策信号',336),
 ('欧洲央行','https://www.ecb.europa.eu/rss/press.html','PRIMARY','欧洲政策',336),
 ('英国央行','https://www.bankofengland.co.uk/rss/news','PRIMARY','英国政策',336),
 ('日本央行','https://www.boj.or.jp/en/rss/whatsnew.xml','PRIMARY','日本政策',336),
 ('EIA能源资讯','https://www.eia.gov/rss/todayinenergy.xml','PRIMARY','能源供需',168),
 ('BEA美国经济数据','https://apps.bea.gov/rss/rss.xml','PRIMARY','增长、消费与贸易',1080),
 ('BLS美国劳工统计','https://www.bls.gov/feed/bls_latest.rss','PRIMARY','通胀与就业',1080),
 ('FDA药品与医疗监管','https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/press-releases/rss.xml','PRIMARY','医药与医疗监管',336),
 ('美国出口管制公告','https://www.federalregister.gov/api/v1/documents.rss?conditions%5Bagencies%5D%5B%5D=industry-and-security-bureau','PRIMARY','出口限制与供应链',1080),
 ('WHO全球卫生','https://www.who.int/rss-feeds/news-english.xml','PRIMARY','公共卫生',336),
 ('Bloomberg市场','https://feeds.bloomberg.com/markets/news.rss','MEDIA','全球金融与产业',48),
 ('Financial Times','https://www.ft.com/rss/home','MEDIA','全球经济与产业',48),
 ('CNBC全球财经','https://www.cnbc.com/id/100003114/device/rss/rss.html','MEDIA','全球财经与企业',48),
 ('BBC世界新闻','https://feeds.bbci.co.uk/news/world/rss.xml','MEDIA','地缘与国际事件',48),
)
FEEDS=tuple((s[0],s[1]) for s in CATALOG)
META={s[0]:{'source':s[0],'url':s[1],'family':s[2],'coverage':s[3],'max_age_hours':s[4]} for s in CATALOG}
META.update({
 '新浪财经快讯':{'family':'WIRE','coverage':'中国及全球快讯','max_age_hours':48},
 '商务部新闻':{'family':'PRIMARY','coverage':'中国贸易政策','max_age_hours':336},
 '联合国新闻':{'family':'PRIMARY','coverage':'国际事件与人道局势','max_age_hours':168},
})
EXTRA_HOSTS={'www.whitehouse.gov','feeds.bloomberg.com','www.bloomberg.com','www.ft.com','www.cnbc.com','feeds.bbci.co.uk','www.bbc.co.uk','www.bbc.com','apps.bea.gov','www.bea.gov','www.bls.gov','www.fda.gov','www.federalregister.gov','www.who.int'}
GAPS=[{'source':'Trump 社媒 / 讲话直播','reason':'Truth Social 公开页对采集端返回403；尚无授权直连，也未接入直播转写。白宫文字声明与媒体报道可用。'},{'source':'Reuters / AP','reason':'尚未接入授权新闻流，不将媒体转述计为直连覆盖。'}]
def article_hosts(name):
 meta=META.get(name,{})
 host=urlsplit(meta.get('url','')).hostname
 aliases={'apps.bea.gov':{'apps.bea.gov','www.bea.gov'},'feeds.bbci.co.uk':{'www.bbc.co.uk','www.bbc.com'},'feeds.bloomberg.com':{'www.bloomberg.com'}}
 return aliases.get(host,{host}) if host else None
def family(name):return META.get(name,{}).get('family','WIRE')
def source_priority(name):return {'PRIMARY':4,'MEDIA':3,'WIRE':1}[family(name)]
def coverage(store,at):
 from datetime import datetime
 checks={r['source']:dict(r) for r in store.db.execute('SELECT * FROM dynamic_feed_checks')}
 health={r['source']:dict(r) for r in store.db.execute('SELECT * FROM macro_feed_health')}
 rows=[]
 for name,meta in META.items():
  check=checks.get(name,{});h=health.get(name,{});state=check.get('status','PENDING')
  if check and (datetime.fromisoformat(at)-datetime.fromisoformat(check['checked_at'])).total_seconds()>5400:state='OVERDUE'
  elif state=='OK' and h.get('latest_at') and (datetime.fromisoformat(at)-datetime.fromisoformat(h['latest_at'])).total_seconds()>meta['max_age_hours']*3600:state='STALE'
  rows.append({**meta,**check,'source':name,'status':state,'latest_at':h.get('latest_at'),'oldest_at':h.get('oldest_at'),'valid_count':h.get('valid_count',0)})
 return {'sources':rows,'gaps':GAPS,'available':sum(r['status']=='OK' for r in rows),'configured':len(rows),
  'note':'公开订阅提供标题与摘要；按源站实际条目采集，不代表已阅读全文或完整回补48小时。'}
