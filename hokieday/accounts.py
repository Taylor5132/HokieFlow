"""SQLite-backed account service. No campus-source or planner behavior is changed."""
from datetime import datetime, date
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

ITERATIONS = 600_000
SESSION_SECONDS = 7 * 24 * 60 * 60
EMPTY_DATA = {'savedClass': None, 'reduceMotion': False, 'plans': [], 'events': []}


class AccountError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def password_hash(password, salt):
    return hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), ITERATIONS).hex()


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


class Accounts:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL,
                    name TEXT NOT NULL, salt TEXT NOT NULL, password_hash TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
                    expires_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS user_data (
                    user_id TEXT PRIMARY KEY REFERENCES users(id),
                    data TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 0
                );
            ''')
        self.path.chmod(0o600)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    def credentials(self, body):
        email, password = body.get('email'), body.get('password')
        if not isinstance(email, str) or len(email) > 254 or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email.strip()):
            raise AccountError('Enter a valid email address.')
        if not isinstance(password, str) or not 12 <= len(password) <= 128:
            raise AccountError('Use a password with 12–128 characters.')
        return email.strip().lower(), password

    def register(self, body):
        email, password = self.credentials(body)
        name = body.get('name', '')
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 60:
            raise AccountError('Enter a name with 1–60 characters.')
        salt, uid = secrets.token_hex(16), secrets.token_hex(16)
        hashed = password_hash(password, salt)
        try:
            with self.db() as db:
                db.execute('INSERT INTO users VALUES (?,?,?,?,?,?)', (uid, email, name.strip(), salt, hashed, int(time.time())))
                db.execute('INSERT INTO user_data VALUES (?,?,0)', (uid, json.dumps(EMPTY_DATA)))
        except sqlite3.IntegrityError:
            raise AccountError('Unable to create this account. Try signing in instead.', 409)
        return self.create_session(uid)

    def login(self, body):
        email, password = self.credentials(body)
        with self.db() as db:
            user = db.execute('SELECT * FROM users WHERE email=?', (email,)).fetchone()
        # Perform equal-cost derivation even if the account does not exist.
        salt = user['salt'] if user else '00' * 16
        candidate = password_hash(password, salt)
        if not user or not hmac.compare_digest(candidate, user['password_hash']):
            raise AccountError('Email or password did not match.', 401)
        return self.create_session(user['id'])

    def create_session(self, uid):
        token = secrets.token_urlsafe(32)
        with self.db() as db:
            db.execute('DELETE FROM sessions WHERE expires_at<=?', (int(time.time()),))
            db.execute('INSERT INTO sessions VALUES (?,?,?)', (token_hash(token), uid, int(time.time()) + SESSION_SECONDS))
        return token

    def logout(self, token):
        with self.db() as db:
            db.execute('DELETE FROM sessions WHERE token_hash=?', (token_hash(token),))

    def session(self, token):
        with self.db() as db:
            row = db.execute('''SELECT u.id,u.name,u.email,d.data,d.version FROM sessions s
                JOIN users u ON s.user_id=u.id JOIN user_data d ON d.user_id=u.id
                WHERE s.token_hash=? AND s.expires_at>?''', (token_hash(token), int(time.time()))).fetchone()
        if not row:
            raise AccountError('Please log in to save your data.', 401)
        return {'user': {'id': row['id'], 'name': row['name'], 'email': row['email']}, 'data': {**EMPTY_DATA, **json.loads(row['data'])}, 'version': row['version']}

    def save(self, token, body):
        session = self.session(token)
        data, version = body.get('data'), body.get('version')
        if not isinstance(data, dict) or type(version) is not int:
            raise AccountError('Invalid account data.')
        data = {**data, 'events': data.get('events', [])}
        if set(data) != {'savedClass', 'reduceMotion', 'plans', 'events'} or type(data['reduceMotion']) is not bool:
            raise AccountError('Invalid account settings.')
        course = data['savedClass']
        if course is not None:
            limits = {'code':30,'title':80,'building':100,'room':20,'time':30}
            if not isinstance(course, dict) or set(course) != set(limits) or any(not isinstance(course[k], str) or len(course[k]) > n for k,n in limits.items()) or not course['code'].strip() or not course['building'].strip():
                raise AccountError('Check the saved class fields.')
        plans = data['plans']
        if not isinstance(plans, list) or len(plans) > 20:
            raise AccountError('You can save up to 20 plans.')
        for plan in plans:
            if not isinstance(plan, dict) or set(plan) != {'id','query','answer'} or not isinstance(plan['id'],str) or len(plan['id'])>80 or not isinstance(plan['query'],str) or len(plan['query'])>1000 or not isinstance(plan['answer'],dict):
                raise AccountError('Invalid saved plan.')
        events = data['events']
        if not isinstance(events, list) or len(events) > 200:
            raise AccountError('You can save up to 200 schedule entries.')
        ids = set()
        for event in events:
            required = {'id','title','kind','location','start','end','repeat','repeatUntil'}
            if not isinstance(event, dict) or set(event) != required:
                raise AccountError('Invalid schedule entry.')
            for field, limit in {'id':80,'title':100,'location':150,'start':40,'end':40}.items():
                if not isinstance(event[field], str) or len(event[field]) > limit:
                    raise AccountError('Invalid schedule field.')
            if not event['id'] or event['id'] in ids or not event['title'].strip() or event['kind'] not in ('class','event') or event['repeat'] not in ('none','weekly'):
                raise AccountError('Check the event title, type and repeat setting.')
            ids.add(event['id'])
            try:
                start = datetime.fromisoformat(event['start'].replace('Z','+00:00'))
                end = datetime.fromisoformat(event['end'].replace('Z','+00:00'))
                if start.tzinfo is None or end.tzinfo is None or end <= start:
                    raise ValueError()
                if event['repeat'] == 'weekly':
                    until = date.fromisoformat(event['repeatUntil'])
                    if until < start.date() or until.year > start.year + 5:
                        raise ValueError()
                elif event['repeatUntil'] is not None:
                    raise ValueError()
            except (ValueError, TypeError):
                raise AccountError('Check event dates and the repeat end date.')
        encoded = json.dumps(data, allow_nan=False)
        if len(encoded.encode()) > 180_000:
            raise AccountError('Saved data is too large. Remove an older plan.', 413)
        with self.db() as db:
            result = db.execute('UPDATE user_data SET data=?,version=version+1 WHERE user_id=? AND version=?', (encoded, session['user']['id'], version))
            if result.rowcount != 1:
                raise AccountError('Your account changed in another tab. Reload before saving again.', 409)
        return self.session(token)
