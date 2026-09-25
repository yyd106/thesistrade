"""macOS service management. Installs only this project's named per-user LaunchAgent."""
from __future__ import annotations
import os
import plistlib
import subprocess
import sys
import time
from pathlib import Path
from .storage import atomic_write

LABEL='local.dean.ashare-agent'


def plist_path():return Path.home()/'Library'/'LaunchAgents'/(LABEL+'.plist')


def loaded(service):
    return subprocess.run(['/bin/launchctl','print',service],capture_output=True,text=True).returncode==0


def wait_unloaded(service,timeout=30):
    """bootout returns before the old process has exited (ExitTimeOut is 20 seconds); loading the
    same label meanwhile fails with 'Bootstrap failed: 5: Input/output error'."""
    deadline=time.monotonic()+timeout
    while loaded(service):
        if time.monotonic()>=deadline:return False
        time.sleep(1)
    return True


def bootstrap(target,path,attempts=3):
    for attempt in range(attempts):
        p=subprocess.run(['/bin/launchctl','bootstrap',target,str(path)],capture_output=True,text=True)
        if not p.returncode or attempt==attempts-1 or 'Input/output error' not in p.stderr:return p
        time.sleep(5)
    return p


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
        if loaded(service):subprocess.run(['/bin/launchctl','bootout',service],capture_output=True,text=True)
        if not wait_unloaded(service):raise RuntimeError('旧服务30秒内未退出，未加载新服务；请稍后重试 ./agent service install')
        atomic_write(path,plistlib.dumps(content));path.chmod(0o600)
        p=bootstrap(target,path)
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
        stopped=wait_unloaded(service)
        return {'status':'UNINSTALLED','data_preserved':True,'stopped':stopped}
    raise ValueError('未知服务操作')
