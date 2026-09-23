"""Operator commands for signed migration and publication; never print private credentials."""
import argparse,json
from pathlib import Path
from .pipeline import load_config
from .storage import Store,now,json_write
from .cloud_protocol import create_keys,request,role
from .cloud_runtime import put,value
from . import cloud_ledger as ledger,cloud_sync as sync


def main():
    p=argparse.ArgumentParser(description='ThesisTrade signed local/cloud migration')
    p.add_argument('--config',required=True);p.add_argument('command',choices=['keys','bootstrap','sync','publish','activate','status'])
    a=p.parse_args();cfg=load_config(a.config)
    if a.command=='keys':
        print(json.dumps(create_keys(cfg['sync_key_file'])));return
    if role(cfg)!='research':raise ValueError('迁移操作要求配置本地为research角色，先停止旧版本执行服务')
    store=Store(cfg['data_dir'])
    try:
        if a.command=='bootstrap':
            store.backup();store.db.execute('BEGIN')
            packet=ledger.bootstrap_packet(store,cfg);store.db.commit()
            json_write(store.root/'workflow/cloud-sync/bootstrap/manifest.json',{'created_at':packet['created_at'],'ledger_version':packet['ledger_version'],'tables':{t:len(r) for t,r in packet['tables'].items()}})
            result=request(cfg,'/api/sync/bootstrap',packet)
            with store.db:
                put(store,'remote_ledger_version',result['ledger_version']);put(store,'ledger_cursors',result['cursors'])
                put(store,'remote_ledger_at',now());put(store,'migration_receipt',result)
            json_write(store.root/'workflow/cloud-sync/bootstrap/receipt.json',result)
        elif a.command=='sync':
            store.close();store=None;result=sync.sync_once(cfg)
        elif a.command=='publish':
            row=store.db.execute("SELECT id FROM portfolio_decisions WHERE status='ACTIVE' ORDER BY created_at DESC LIMIT 1").fetchone()
            if not row:raise ValueError('请先完成本地组合研究')
            sync.queue_publication(store,cfg,row[0]);result=sync.flush(store,cfg)
        elif a.command=='activate':
            # The signing host has already transferred execution ownership to the cloud.
            packet=sync.pull(store,cfg)
            result=request(cfg,'/api/sync/activate',{'ledger_version':packet['ledger_version'],'local_execution_disabled':True})
            json_write(store.root/'workflow/cloud-sync/activation.json',{'at':now(),**result})
        else:result={k:value(store,k) for k in ('last_sync','last_upload','remote_ledger_at','migration_receipt')}
        print(json.dumps(result,ensure_ascii=False))
    finally:
        if store:store.close()

if __name__=='__main__':main()
