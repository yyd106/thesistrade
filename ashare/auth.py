"""Local shared-account authentication. No default password or plaintext credential storage."""
import hashlib
import hmac
import secrets
from datetime import datetime,timedelta
from http.cookies import SimpleCookie,CookieError
from .storage import now,normalize_time,digest

COOKIE='thesistrade_session'
SESSION_SECONDS=12*3600
USERS={'admin':'ADMIN','guest':'GUEST'}


class AuthError(ValueError):
    def __init__(self,message,code=401):super().__init__(message);self.code=code


def hash_password(password,salt=None):
    if not isinstance(password,str) or not 12<=len(password)<=256:raise AuthError('密码请使用12至256个字符。',400)
    salt=salt or secrets.token_bytes(16)
    value=hashlib.scrypt(password.encode(),salt=salt,n=16384,r=8,p=1,dklen=32)
    return salt.hex()+':'+value.hex()


def matches(password,encoded):
    try:
        salt,_=encoded.split(':')
        return hmac.compare_digest(hash_password(password,bytes.fromhex(salt)),encoded)
    except (ValueError,TypeError):return False


def setup_required(store):return store.db.execute('SELECT count(*) FROM app_users').fetchone()[0]==0


def setup(store,admin_password,guest_password):
    if admin_password==guest_password:raise AuthError('Admin和Guest请使用不同密码。',400)
    values={'admin':hash_password(admin_password),'guest':hash_password(guest_password)}
    try:
        store.db.execute('BEGIN IMMEDIATE')
        if not setup_required(store):raise AuthError('已经完成首次设置；请登录。',409)
        store.db.executemany('INSERT INTO app_users VALUES(?,?,?,?)',[(u,USERS[u],p,now()) for u,p in values.items()])
        store.db.commit()
    except BaseException:store.db.rollback();raise


def login(store,username,password,remote,at=None):
    at=normalize_time(at or now());username=str(username).lower();remote=digest(remote)
    cutoff=normalize_time((datetime.fromisoformat(at)-timedelta(minutes=10)).isoformat())
    row=store.db.execute('SELECT * FROM app_users WHERE username=?',(username,)).fetchone()
    with store.db:
        store.db.execute('DELETE FROM login_attempts WHERE at<?',(cutoff,))
        count=store.db.execute('SELECT count(*) FROM login_attempts WHERE remote=?',(remote,)).fetchone()[0]
        if count>=10:raise AuthError('尝试过于频繁，请10分钟后再试。',429)
        # Count before deriving the password, including concurrent requests.
        store.db.execute('INSERT INTO login_attempts(remote,at) VALUES(?,?)',(remote,at))
    if not row or not matches(password,row['password_hash']):raise AuthError('账号或密码不正确。')
    token=secrets.token_urlsafe(32);csrf=secrets.token_urlsafe(32)
    expires=normalize_time((datetime.fromisoformat(at)+timedelta(seconds=SESSION_SECONDS)).isoformat())
    with store.db:
        store.db.execute('DELETE FROM login_attempts WHERE remote=?',(remote,))
        store.db.execute('DELETE FROM app_sessions WHERE expires_at<=?',(at,))
        store.db.execute('INSERT INTO app_sessions VALUES(?,?,?,?,?)',(digest(token),username,csrf,at,expires))
    return token,{'username':username,'role':row['role'],'csrf_token':csrf,'expires_at':expires}


def session(store,cookie,at=None):
    try:
        jar=SimpleCookie();jar.load(cookie or '');value=jar[COOKIE].value
    except (KeyError,ValueError,CookieError):return None
    row=store.db.execute('SELECT s.*,u.role FROM app_sessions s JOIN app_users u ON u.username=s.username WHERE token_hash=? AND expires_at>?',(digest(value),normalize_time(at or now()))).fetchone()
    return dict(row) if row else None


def logout(store,current):
    if current:
        with store.db:store.db.execute('DELETE FROM app_sessions WHERE token_hash=?',(current['token_hash'],))


def reset_password(store,username,password):
    username=username.lower()
    if username not in USERS:raise AuthError('账号须为admin或guest。',400)
    hashed=hash_password(password)
    with store.db:
        if not store.db.execute('SELECT 1 FROM app_users WHERE username=?',(username,)).fetchone():raise AuthError('请先完成首次设置。',400)
        store.db.execute('UPDATE app_users SET password_hash=?,updated_at=? WHERE username=?',(hashed,now(),username))
        store.db.execute('DELETE FROM app_sessions WHERE username=?',(username,))


def cookie_header(token=None,*,secure=False):
    # Cloud HTTPS sessions never send credentials over HTTP.
    return f'{COOKIE}={token or ""}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_SECONDS if token else 0}'+('; Secure' if secure else '')
