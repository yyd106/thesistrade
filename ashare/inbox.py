"""Import user-provided licensed reports with explicit metadata; no credential scraping."""
import json
from pathlib import Path
from .sources import pdf_pages


def import_inbox(store,config,run_id):
    root=store.root/'inbox';root.mkdir(exist_ok=True)
    total=0;errors=[]
    for manifest in sorted(root.glob('*.json')):
        scope='MARKET';title=manifest.name
        try:
            if manifest.stat().st_size>20000:raise ValueError('元数据文件过大')
            m=json.loads(manifest.read_text());file=(root/m['file']).resolve()
            if root.resolve() not in file.parents:raise ValueError('文件必须在inbox目录中')
            if file.stat().st_size>20_000_000:raise ValueError('文件超过20MB')
            if m['symbol'] not in {x['symbol'] for x in config['watchlist']}|{'MARKET'}:raise ValueError('证券不在自选股中')
            scope=m['symbol'];title=m.get('title') or manifest.name
            if m['kind'] not in ('broker_report','news','company_report'):raise ValueError('资料类型不支持')
            if not isinstance(m.get('title'),str) or not m['title']:raise ValueError('缺少标题')
            if type(m.get('cloud_allowed',False)) is not bool:raise ValueError('cloud_allowed必须为布尔值')
            source_url=m.get('source_url')
            if source_url:
                from .sources import check_url
                check_url(source_url)
            raw=file.read_bytes();suffix=file.suffix.lower()
            if suffix=='.pdf':pages=pdf_pages(raw);quality='pdf_text_layout_unverified'
            elif suffix in ('.txt','.md'):pages=[(None,raw.decode('utf-8'))];quality='user_supplied_text'
            else:raise ValueError('仅支持PDF/TXT/MD')
            _,created=store.add_document(symbol=m['symbol'],kind=m['kind'],title=m['title'],source='user_import',
                url=source_url or file.as_uri(),published_at=m['published_at'],pages=pages,raw_path=store.raw(raw,suffix),
                cloud_allowed=m.get('cloud_allowed',False),quality=quality,reference_period=m.get('reference_period'))
            total+=int(created)
            store.record_attempt('report_inbox_item',scope,'OK',resource_key=manifest.name,title=title,run_id=run_id)
            if scope!='MARKET':store.record_attempt('report_inbox_item','MARKET','OK',resource_key=manifest.name,run_id=run_id)
        except Exception as exc:
            errors.append(manifest.name+': '+str(exc)[:150])
            store.record_attempt('report_inbox_item',scope,'FAILED',str(exc),resource_key=manifest.name,title=str(title),run_id=run_id)
    store.check(run_id,'report_inbox',None,'PARTIAL' if errors else 'OK',f'新增{total}份；'+'；'.join(errors),track=False)
    if not errors:store.record_attempt('report_inbox','MARKET','OK',run_id=run_id)
