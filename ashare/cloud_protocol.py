"""Signed, replay-resistant local research requests. The cloud holds only a public key."""
import base64,gzip,json,os,secrets,urllib.request,urllib.error
from datetime import datetime,timezone
from pathlib import Path
from .storage import normalize_time,now,digest

MAX_BODY=16_000_000
MAX_JSON=64_000_000


def role(config):return config.get('deployment_role','standalone')
def canonical(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
def packed(value):return gzip.compress(canonical(value),mtime=0)
def unpack(raw):
    import io
    if len(raw)>MAX_BODY:raise ValueError('同步数据过大')
    with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:data=stream.read(MAX_JSON+1)
    if len(data)>MAX_JSON:raise ValueError('解压数据过大')
    result=json.loads(data)
    if not isinstance(result,dict):raise ValueError('同步结构错误')
    return result


def credentials(config):
    path=config.get('sync_key_file')
    if not path:raise ValueError('尚未配置本地签名私钥')
    return json.loads(Path(path).read_text())


def create_keys(path):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding,PrivateFormat,PublicFormat,NoEncryption
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    key=Ed25519PrivateKey.generate()
    value={'key_id':secrets.token_hex(12),'private_key':base64.b64encode(key.private_bytes(Encoding.Raw,PrivateFormat.Raw,NoEncryption())).decode(),
           'public_key':base64.b64encode(key.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw)).decode()}
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as f:json.dump(value,f)
    return {k:value[k] for k in ('key_id','public_key')}


def message(path,stamp,nonce,raw):return ('thesistrade-sync-v1\nPOST\n'+path+'\n'+stamp+'\n'+nonce+'\n'+digest(raw)).encode()


def signed_headers(config,path,raw,at=None,nonce=None):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    c=credentials(config);stamp=normalize_time(at or now());nonce=nonce or secrets.token_hex(20)
    key=Ed25519PrivateKey.from_private_bytes(base64.b64decode(c['private_key'],validate=True))
    return {'Content-Type':'application/octet-stream','X-Research-Key':c['key_id'],'X-Research-Time':stamp,'X-Research-Nonce':nonce,
            'X-Research-Signature':base64.b64encode(key.sign(message(path,stamp,nonce,raw))).decode()}


def verify(config,path,raw,headers,at):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    if role(config)!='cloud':raise ValueError('不是云端同步接收节点')
    key_id=config.get('sync_key_id');public=config.get('sync_public_key')
    if not key_id or not public or headers.get('X-Research-Key')!=key_id:raise ValueError('无效研究端身份')
    stamp=headers.get('X-Research-Time','');nonce=headers.get('X-Research-Nonce','')
    if len(nonce)!=40 or any(c not in '0123456789abcdef' for c in nonce):raise ValueError('无效请求序号')
    if abs((datetime.fromisoformat(normalize_time(at))-datetime.fromisoformat(normalize_time(stamp))).total_seconds())>300:raise ValueError('签名请求超时')
    try:Ed25519PublicKey.from_public_bytes(base64.b64decode(public,validate=True)).verify(base64.b64decode(headers.get('X-Research-Signature',''),validate=True),message(path,stamp,nonce,raw))
    except Exception:raise ValueError('研究端签名验证失败') from None
    return nonce


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):raise ValueError('同步地址不允许重定向')


def request(config,path,payload):
    from urllib.parse import urlsplit
    url=config.get('sync_cloud_url','').rstrip('/');u=urlsplit(url)
    if u.scheme!='https' and not (config.get('sync_allow_loopback') and u.scheme=='http' and u.hostname in ('127.0.0.1','localhost')):raise ValueError('同步仅允许HTTPS')
    if u.username or u.password or u.query or u.fragment or u.path not in ('','/'):raise ValueError('云端地址格式无效')
    raw=packed(payload);req=urllib.request.Request(url+path,data=raw,headers=signed_headers(config,path,raw),method='POST')
    try:
        with urllib.request.build_opener(NoRedirect).open(req,timeout=45) as response:answer=response.read(MAX_BODY+1)
        return unpack(answer)
    except urllib.error.HTTPError as exc:
        # Never include signed headers, passwords, or private key material in diagnostics.
        raise RuntimeError('云端同步拒绝请求 HTTP '+str(exc.code)+': '+exc.read(500).decode('utf-8','replace')) from None
