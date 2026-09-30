import re

DEFAULTS = {
 'industry_enabled':False,'industry_policy':{},
 'dynamic_enabled':False,'dynamic_model_timeout_seconds':120,
 'collection_times': ['08:00','20:00'], 'review_time': '19:30',
 'slot_times': ['09:30','10:00','10:30','11:00','13:00','13:30','14:00','14:30'],
 'plan_max_age_hours': 12, 'slot_deadline_seconds': 240,
 'quote_max_age_seconds': 90, 'slot_model_timeout_seconds': 90,
 'slot_execution_mode':'MODEL',
 'scheduler_enabled': True, 'scheduler_poll_seconds': 20,
 'max_packet_chars': 45000, 'paper_max_stock_pct': 20, 'paper_max_gross_pct': 90,
 'paper_lot_size': 100, 'paper_commission_bps': 3, 'paper_min_fee_cents': 500,
 'paper_sell_tax_bps': 5, 'paper_slippage_bps': 5,
 'paper_max_fill_qty': 100, 'paper_order_ttl_seconds': 240,
 'paper_stop_loss_bps': 600, 'paper_take_profit_bps': 1000,
 'paper_entry_band_bps': 100,
 'strategy_version': 'paper_baseline_v1', 'ui_port': 8765,
 'external_news_enabled': True, 'external_news_articles_per_source': 3, 'external_news_lookback_days': 30,
 'research_topics': {}
 ,'document_recheck_hours':24,'pdf_revision_checks_per_stock':1,
 'comparison_peers':{},'business_keywords':{},'max_news_packet_pct':15
 ,'background_market_enabled':True,'quote_poll_seconds':30,'announcement_poll_seconds':120,
 'announcement_max_age_seconds':180,'research_attempts':2,
 'recovery_interval_seconds':900,'recovery_daily_limit':2,
 # Model identity is pinned explicitly; None keeps the CLI default (tests, demo).
 'model_name':None,'model_reasoning_effort':None,
 # Reuse a successful study when nothing material changed; 0 disables.
 'research_reuse_hours':0,
 'portfolio_refresh_minutes':55,
 'backup_hourly_keep':6,'backup_daily_keep':7,
 'disk_free_warn_gb':10,'db_size_warn_gb':5,
 'evaluation_enabled':True,'evaluation_time':'19:10','evaluation_horizon_days':20,
 'weekly_report_weekday':5,'weekly_report_time':'10:00','digest_time':'23:50',
 'shadow_books_enabled':True,'shadow_risk_per_trade_bps':50,'shadow_start_date':None,
 # Backup quote endpoint on the executing node; outages are recorded, never pushed to the user.
 'quote_fallback_enabled':True,
 # Evaluation batches: a manual request needs this many new trading days (--force overrides);
 # one is also cut automatically every N trading days after the digest (0 disables).
 'evaluation_min_trading_days':3,'evaluation_auto_trading_days':5,
 # Local supervision uses separate subscription sessions and never changes production rules.
 'supervision_enabled':True,'supervision_timeout_seconds':180,
 # Optional private summary backup and external reviewer exchange.
 'reports_sync_enabled':False,'reports_remote':None,'reports_repo_dir':None,'reports_ssh_key':None
}


def validate_settings(config):
    if config.get('deployment_role','standalone') not in ('standalone','cloud','research'):raise ValueError('节点角色无效')
    if config.get('cloud_stale_policy','reduce_only') not in ('reduce_only','stop_all'):raise ValueError('研究断联策略无效')
    if config.get('investment_policy') not in (None, 'days_cash_v1'):
        raise ValueError('未知投资政策版本')
    if config.get('investment_policy'):
        if config.get('mode') != 'paper' or config.get('live_execution_enabled'):
            raise ValueError('当前投资政策仅允许无杠杆模拟账户')
        if len(config.get('watchlist', [])) > 36:
            raise ValueError('总观察额度40，须为四个固定资产保留名额')
    for k,v in DEFAULTS.items():
        config.setdefault(k,v)
    from .industry_research import policy as industry_policy
    industry_policy(config)
    if type(config['industry_enabled']) is not bool:raise ValueError('industry_enabled必须为布尔值')
    for k in ('collection_times','slot_times'):
        v = config[k]
        if not isinstance(v,list) or not v or len(set(v))!=len(v) or any(not isinstance(x,str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',x) for x in v):
            raise ValueError(k+'必须为不重复的HH:MM列表')
    if len(config['collection_times'])>24:
        raise ValueError('每天最多安排24次采集研究')
    times=sorted(int(x[:2])*60+int(x[3:]) for x in config['collection_times'])
    if len(times)>1 and min(b-a for a,b in zip(times,times[1:]+[times[0]+1440]))<60:
        raise ValueError('两次采集研究须至少间隔1小时')
    if config['slot_execution_mode'] not in ('MODEL','RULES'):
        raise ValueError('盘面执行方式须为MODEL或RULES')
    if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',config['review_time']):
        raise ValueError('review_time格式错误')
    ranges={'plan_max_age_hours':(1,24),'slot_deadline_seconds':(30,300),'quote_max_age_seconds':(1,300),
        'slot_model_timeout_seconds':(10,180),'scheduler_poll_seconds':(5,60),'max_packet_chars':(5000,100000),
        'paper_max_stock_pct':(1,20),'paper_max_gross_pct':(1,90),'paper_lot_size':(100,100),
        'paper_commission_bps':(0,100),'paper_min_fee_cents':(0,10000),'paper_sell_tax_bps':(0,100),
        'paper_slippage_bps':(0,100),'paper_max_fill_qty':(100,10000),'paper_order_ttl_seconds':(10,300),
        'paper_stop_loss_bps':(1,2000),'paper_take_profit_bps':(1,5000),'paper_entry_band_bps':(50,300),'ui_port':(1024,65535),
        'external_news_articles_per_source':(1,10),'external_news_lookback_days':(1,90)}
    for k,(low,high) in ranges.items():
        if type(config[k]) is not int or not low<=config[k]<=high:
            raise ValueError(f'{k}必须为{low}到{high}之间整数')
    slots=sorted(int(x[:2])*60+int(x[3:]) for x in config['slot_times'])
    if len(slots)>1 and config['slot_deadline_seconds']>=min(b-a for a,b in zip(slots,slots[1:]))*60:
        raise ValueError('单次盘面检查截止时间须早于下一次检查')
    if config['slot_execution_mode']=='MODEL' and config['slot_model_timeout_seconds']+10>=config['slot_deadline_seconds']:
        raise ValueError('盘面检查须给模型分析及执行核验留出时间')
    if config['paper_max_fill_qty']%100 or config['strategy_version']!='paper_baseline_v1':
        raise ValueError('模拟成交量须为100股倍数；只支持paper_baseline_v1')
    if config['paper_entry_band_bps']>=min(config['paper_stop_loss_bps'],config['paper_take_profit_bps']):
        raise ValueError('买入区间须位于止损和止盈参考价之间')
    if type(config['scheduler_enabled']) is not bool or config['timezone']!='Asia/Shanghai':
        raise ValueError('调度开关/时区错误')
    if type(config['dynamic_enabled']) is not bool or type(config['dynamic_model_timeout_seconds']) is not int or not 30<=config['dynamic_model_timeout_seconds']<=180:
        raise ValueError('动态开关或模型时限无效')
    from .events import TOPICS
    if type(config['external_news_enabled']) is not bool or not isinstance(config['research_topics'],dict):
        raise ValueError('外部新闻开关或研究主题配置错误')
    for symbol,topics in config['research_topics'].items():
        if not re.fullmatch(r'(?:sh|sz)\d{6}',symbol) or not isinstance(topics,list) or any(t not in TOPICS for t in topics):
            raise ValueError('股票研究主题须使用已定义的主题名称')
    for k,lo,hi in (('document_recheck_hours',1,168),('pdf_revision_checks_per_stock',0,3),('max_news_packet_pct',0,20)):
        if type(config[k]) is not int or not lo<=config[k]<=hi:raise ValueError(k+'超出允许范围')
    for k,lo,hi in (('quote_poll_seconds',10,60),('announcement_poll_seconds',60,300),
                   ('announcement_max_age_seconds',60,300),('research_attempts',1,2),
                   ('recovery_interval_seconds',300,3600),('recovery_daily_limit',1,3)):
        if type(config[k]) is not int or not lo<=config[k]<=hi:raise ValueError(k+'超出允许范围')
    if type(config['background_market_enabled']) is not bool:raise ValueError('后台行情开关格式错误')
    from .model import EFFORTS
    if config['model_name'] is not None and (not isinstance(config['model_name'],str) or not re.fullmatch(r'[A-Za-z0-9._:-]{1,64}',config['model_name'])):raise ValueError('model_name格式无效')
    if config['model_reasoning_effort'] is not None and config['model_reasoning_effort'] not in EFFORTS:raise ValueError('model_reasoning_effort须为'+'/'.join(EFFORTS))
    for k,lo,hi in (('research_reuse_hours',0,24),('portfolio_refresh_minutes',30,360),('backup_hourly_keep',1,48),
                   ('backup_daily_keep',0,60),('disk_free_warn_gb',1,1000),('db_size_warn_gb',1,1000),
                   ('evaluation_horizon_days',5,60),('weekly_report_weekday',0,6),('shadow_risk_per_trade_bps',10,200)):
        if type(config[k]) is not int or not lo<=config[k]<=hi:raise ValueError(f'{k}必须为{lo}到{hi}之间整数')
    for k in ('evaluation_enabled','shadow_books_enabled'):
        if type(config[k]) is not bool:raise ValueError(k+'须为true或false')
    for k in ('evaluation_time','weekly_report_time','digest_time'):
        if not isinstance(config[k],str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',config[k]):raise ValueError(k+'格式错误')
    for k,lo,hi in (('evaluation_min_trading_days',1,10),('evaluation_auto_trading_days',0,20)):
        if type(config[k]) is not int or not lo<=config[k]<=hi:raise ValueError(f'{k}必须为{lo}到{hi}之间整数')
    for k in ('quote_fallback_enabled','reports_sync_enabled','supervision_enabled'):
        if type(config[k]) is not bool:raise ValueError(k+'须为true或false')
    if type(config['supervision_timeout_seconds']) is not int or not 60<=config['supervision_timeout_seconds']<=300:
        raise ValueError('supervision_timeout_seconds须为60到300之间整数')
    if config['reports_remote'] is not None and (not isinstance(config['reports_remote'],str) or not re.fullmatch(r'git@github\.com:[A-Za-z0-9_.-]{1,39}/[A-Za-z0-9_.-]{1,100}\.git',config['reports_remote'])):
        raise ValueError('reports_remote须为 git@github.com:<owner>/<repo>.git')
    for k in ('reports_repo_dir','reports_ssh_key'):
        if config[k] is not None and (not isinstance(config[k],str) or not config[k].strip()):raise ValueError(k+'须为路径')
    if config['reports_sync_enabled'] and not config['reports_remote']:raise ValueError('启用报告仓库同步前须设置reports_remote')
    if config['shadow_start_date'] is not None and (not isinstance(config['shadow_start_date'],str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',config['shadow_start_date'])):raise ValueError('shadow_start_date须为YYYY-MM-DD')
    # Research must refresh before a plan expires, otherwise buying silently stops between rounds.
    if config.get('deployment_role')!='cloud' and len(times)>1:
        widest=max(b-a for a,b in zip(times,times[1:]+[times[0]+1440]))
        if widest>config['plan_max_age_hours']*60:
            raise ValueError('相邻两次采集研究的间隔须短于计划有效期（plan_max_age_hours），否则计划会在两轮研究之间过期')
    if not isinstance(config['comparison_peers'],dict) or not isinstance(config['business_keywords'],dict):raise ValueError('研究对照或关键词格式错误')
    for symbol,rows in config['comparison_peers'].items():
        if not re.fullmatch(r'(sh|sz)\d{6}',symbol) or not isinstance(rows,list) or len(rows)>5:raise ValueError('业务参考股配置错误')
        codes=[]
        for row in rows:
            if not isinstance(row,dict) or not re.fullmatch(r'(sh|sz)\d{6}',row.get('symbol','')) or not isinstance(row.get('name'),str) or not row['name']:raise ValueError('业务参考股格式错误')
            codes.append(row['symbol'])
        if symbol in codes or len(set(codes))!=len(codes):raise ValueError('业务参考股不能重复或包含自身')
    for symbol,terms in config['business_keywords'].items():
        if not re.fullmatch(r'(sh|sz)\d{6}',symbol) or not isinstance(terms,list) or len(terms)>20 or any(not isinstance(t,str) or not 2<=len(t)<=30 for t in terms):raise ValueError('公司业务关键词格式错误')
