"""macOS service management. Installs only this project's named per-user LaunchAgent."""
from __future__ import annotations
import os
import plistlib
import subprocess
import sys
from pathlib import Path
from .storage import atomic_write

LABEL='local.dean.ashare-agent'


def plist_path():return Path.home()/'Library'/'LaunchAgents'/(LABEL+'.plist')


def manage(config,action):
    if sys.platform!='darwin':raise RuntimeError('此服务命令仅支持macOS；其他系统使用 ./agent serve')
    root=Path(config['data_dir']);logs=root/'logs';logs.mkdir(parents=True,exist_ok=True)
    project=Path(__file__).resolve().parents[1]
    target='gui/'+str(os.getuid());service=target+'/'+LABEL;path=plist_path()
    if action=='install':
        content={'Label':LABEL,'ProgramArguments':[sys.executable,'-m','ashare.cli','--config',config['config_path'],'serve'],
            'WorkingDirectory':str(project),'RunAtLoad':True,'KeepAlive':True,'ThrottleInterval':30,
            'StandardOutPath':str(logs/'service.log'),'StandardErrorPath':str(logs/'service-error.log'),
            'EnvironmentVariables':{'PATH':'/Applications/ChatGPT.app/Contents/Resources:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin','PYTHONUNBUFFERED':'1'},
            'ProcessType':'Background','ExitTimeOut':20}
        path.parent.mkdir(parents=True,exist_ok=True)
        if path.exists():
            existing=plistlib.loads(path.read_bytes())
            if existing.get('WorkingDirectory')!=str(project):raise RuntimeError('同名服务属于其他目录，未覆盖')
            subprocess.run(['/bin/launchctl','bootout',service],capture_output=True,text=True)
        atomic_write(path,plistlib.dumps(content));path.chmod(0o600)
        p=subprocess.run(['/bin/launchctl','bootstrap',target,str(path)],capture_output=True,text=True)
        if p.returncode:raise RuntimeError('服务文件已生成但加载失败：'+p.stderr.strip()+'；见SETUP.md')
        return {'status':'INSTALLED','plist':str(path),'url':'http://127.0.0.1:'+str(config['ui_port']),'logs':str(logs)}
    if action=='status':
        p=subprocess.run(['/bin/launchctl','print',service],capture_output=True,text=True)
        detail=[line.strip() for line in p.stdout.splitlines() if line.strip().startswith(('state =','pid =','runs =','last exit code ='))]
        return {'installed':path.exists(),'loaded':p.returncode==0,'detail':detail if p.returncode==0 else p.stderr.strip()}
    if action=='restart':
        p=subprocess.run(['/bin/launchctl','kickstart','-k',service],capture_output=True,text=True)
        if p.returncode:raise RuntimeError(p.stderr.strip())
        return {'status':'RESTARTED'}
    if action=='uninstall':
        if path.exists():
            existing=plistlib.loads(path.read_bytes())
            if existing.get('WorkingDirectory')!=str(project):raise RuntimeError('服务目录不匹配，未删除')
            subprocess.run(['/bin/launchctl','bootout',service],capture_output=True,text=True)
            path.unlink()
        return {'status':'UNINSTALLED','data_preserved':True}
    raise ValueError('未知服务操作')
