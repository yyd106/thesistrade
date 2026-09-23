"""Per-stock successful reading receipts and bounded, source-backed research memory."""
import json


def context(store,symbol,stamp,documents):
    allowed={d['id']:d for d in documents if d['cloud_allowed']}
    read={r[0] for r in store.db.execute('SELECT chunk_id FROM learned_chunks WHERE symbol=? AND learned_at<=?',(symbol,stamp))}
    # Exact duplicate content at another URL is already learned for this stock.
    units={(r['content_hash'],r['ordinal']) for r in store.db.execute('''SELECT d.content_hash,c.ordinal
        FROM learned_chunks l JOIN chunks c ON c.id=l.chunk_id JOIN documents d ON d.id=c.doc_id
        WHERE l.symbol=? AND l.learned_at<=?''',(symbol,stamp))}
    prior=None;facts=[];seen=set()
    for s in store.db.execute('''SELECT s.*,sn.as_of,sn.packet_json FROM studies s
        JOIN snapshots sn ON sn.id=s.snapshot_id WHERE s.symbol=? AND s.created_at<=?
        AND s.model_status='SUCCEEDED' ORDER BY s.created_at DESC,s.rowid DESC''',(symbol,stamp)):
        result=json.loads(s['result_json']);packet=json.loads(s['packet_json'])
        stock=next((x for x in result.get('stocks',[]) if x['symbol']==symbol),None)
        if not stock:continue
        if prior is None:
            # Do not replay an aggregate summary after a source was replaced or permission revoked.
            source_ids=packet.get('memory_source_ids',[e['doc_id'] for e in packet.get('evidence',[])])
            safe=all(doc_id in allowed for doc_id in source_ids)
            prior={'study_id':s['id'],'as_of':s['as_of'],'completed_at':s['created_at'],
                'analysis':stock['analysis'][:1800] if safe else '部分原资料已修订、失效或不可用于云端，旧结论需重新核对。',
                'counterpoints':stock['counterpoints'][:3] if safe else [],
                'next_checks':stock['next_checks'][:3] if safe else [],
                'source_ids':source_ids if safe else [],
                    'claim_type':'历史研究观点，仅作为本轮比较基线，不能替代原始证据'}
            if safe and stock.get('decision'):prior['decision']=stock['decision']
        for f in stock.get('facts',[]):
            if f['evidence_id'] in seen or len(facts)>=24:continue
            row=store.db.execute('SELECT * FROM chunks WHERE id=?',(f['evidence_id'],)).fetchone()
            if not row or row['doc_id'] not in allowed or f['quote'] not in row['text']:continue
            d=allowed[row['doc_id']]
            facts.append({'evidence_id':row['id'],'doc_id':d['id'],'page':row['page'],'text':f['quote'],
                'symbol':d['symbol'],'title':d['title'],'url':d['url'],'kind':d['kind'],
                'published_at':d['published_at'],'ready_at':d['ready_at'],
                'claim_type':d['claim_type'],'use':'已核验历史摘录，仅沿用，不重复阅读整篇原文'})
            seen.add(row['id'])
    return read,units,prior,facts


def commit(store,packet,study_id,stamp):
    # Called in the same transaction that publishes a validated successful study.
    for cid in packet.get('learning',{}).get('new_chunk_ids',[]):
        if store.db.execute('SELECT 1 FROM chunks WHERE id=?',(cid,)).fetchone():
            store.db.execute('INSERT OR IGNORE INTO learned_chunks VALUES(?,?,?,?)',
                             (packet['symbol'],cid,study_id,stamp))
