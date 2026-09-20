import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest

from serve import AppServer


class AccountTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / 'accounts.sqlite3'
        self.server = AppServer(('127.0.0.1', 0), Path(__file__).resolve().parents[1] / 'ui', self.db)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host = f'127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def request(self, method, path, body=None, cookie=None, extra=None):
        headers = {'Content-Type':'application/json','X-HokieFlow-Request':'1','Origin':f'http://{self.host}'}
        if cookie:
            headers['Cookie'] = cookie.split(';')[0]
        headers.update(extra or {})
        client = http.client.HTTPConnection(self.host)
        client.request(method, path, json.dumps(body) if body is not None else None, headers)
        response = client.getresponse()
        payload = response.read()
        result = (response.status, json.loads(payload), response.getheader('Set-Cookie'))
        client.close()
        return result

    def register(self, email='one@example.test'):
        return self.request('POST','/api/auth/register',{'name':'Test Hokie','email':email,'password':'Example-password-42'})

    def test_signup_login_logout_and_hash_storage(self):
        status, result, cookie = self.register()
        self.assertEqual(status, 200)
        self.assertIn('HttpOnly',cookie)
        self.assertIn('SameSite=Lax',cookie)
        self.assertIn('no-store', self._cache_header(cookie))
        with self.server.accounts.db() as db:
            user = db.execute('SELECT * FROM users').fetchone()
            session = db.execute('SELECT * FROM sessions').fetchone()
        self.assertNotEqual(user['password_hash'],'Example-password-42')
        self.assertNotIn(cookie.split('=',1)[1].split(';')[0],session['token_hash'])
        self.assertEqual(self.request('GET','/api/auth/me',cookie=cookie)[1]['user']['email'],'one@example.test')
        self.request('POST','/api/auth/logout',{},cookie)
        self.assertIsNone(self.request('GET','/api/auth/me',cookie=cookie)[1]['user'])
        status, _, new_cookie = self.request('POST','/api/auth/login',{'email':'ONE@example.test','password':'Example-password-42'})
        self.assertEqual(status,200)
        self.assertNotEqual(cookie,new_cookie)

    def _cache_header(self,cookie):
        client=http.client.HTTPConnection(self.host)
        client.request('GET','/api/auth/me',headers={'Cookie':cookie.split(';')[0]})
        response=client.getresponse();value=response.getheader('Cache-Control');response.read();client.close();return value

    def test_accounts_are_isolated_and_persist(self):
        _, first, cookie1=self.register()
        _, second, cookie2=self.register('two@example.test')
        data=first['data'];data['savedClass']={'code':'CS 2506','title':'Comp Org','building':'McBryde Hall','room':'100','time':'10:10 AM'}
        data['plans']=[{'id':'one','query':'Lunch','answer':{'feasible':False,'_time':{'is_replay':True}}}]
        status, saved, _=self.request('PUT','/api/account/data',{'data':data,'version':0,'user_id':second['user']['id']},cookie1)
        self.assertEqual(status,200)
        self.assertIsNone(self.request('GET','/api/auth/me',cookie=cookie2)[1]['data']['savedClass'])
        from hokieday.accounts import Accounts
        reopened=Accounts(self.db)
        token=cookie1.split('=',1)[1].split(';')[0]
        self.assertEqual(reopened.session(token)['data']['savedClass']['code'],'CS 2506')
        self.assertEqual(saved['version'],1)

    def test_rejects_unauthenticated_and_cross_origin_writes(self):
        self.assertEqual(self.request('PUT','/api/account/data',{'data':{},'version':0})[0],401)
        self.assertEqual(self.request('POST','/api/auth/register',{},extra={'Origin':'https://evil.example'})[0],403)
        self.assertEqual(self.request('POST','/api/auth/login',{},extra={'X-HokieFlow-Request':''})[0],403)
        self.assertEqual(self.request('GET','/api/auth/me',extra={'Host':'evil.example'})[0],403)

    def test_expired_session_cannot_save(self):
        _, result, cookie=self.register()
        with self.server.accounts.db() as db: db.execute('UPDATE sessions SET expires_at=0')
        self.assertEqual(self.request('PUT','/api/account/data',{'data':result['data'],'version':0},cookie)[0],401)

    def test_stale_version_and_invalid_data_rejected(self):
        _, result, cookie=self.register()
        body={'data':result['data'],'version':0}
        self.assertEqual(self.request('PUT','/api/account/data',body,cookie)[0],200)
        self.assertEqual(self.request('PUT','/api/account/data',body,cookie)[0],409)
        body['version']=1;body['data']['reduceMotion']='yes'
        self.assertEqual(self.request('PUT','/api/account/data',body,cookie)[0],400)

    def test_bad_password_duplicate_account_and_throttling(self):
        self.register()
        self.assertEqual(self.register()[0],409)
        self.assertEqual(self.request('POST','/api/auth/login',{'email':'one@example.test','password':'Wrong-password-42'})[0],401)
        for _ in range(9): self.request('POST','/api/auth/login',{'email':'broken','password':'x'})
        self.assertEqual(self.request('POST','/api/auth/login',{})[0],429)

    def test_schedule_save_isolation_validation_and_migration(self):
        _, first, cookie = self.register()
        _, _, other = self.register('other@example.test')
        data = first['data']
        entry = {'id':'event-one','title':'Study','kind':'event','location':'Library',
                 'start':'2026-09-20T14:00:00Z','end':'2026-09-20T15:00:00Z',
                 'repeat':'weekly','repeatUntil':'2026-12-20'}
        data['events'] = [entry]
        status, saved, _ = self.request('PUT','/api/account/data',{'data':data,'version':0},cookie)
        self.assertEqual(status,200)
        self.assertEqual(saved['data']['events'][0]['title'],'Study')
        self.assertEqual(self.request('GET','/api/auth/me',cookie=other)[1]['data']['events'],[])
        data['events'][0]['end'] = data['events'][0]['start']
        self.assertEqual(self.request('PUT','/api/account/data',{'data':data,'version':1},cookie)[0],400)
        data.pop('events')
        self.assertEqual(self.request('PUT','/api/account/data',{'data':data,'version':1},cookie)[0],200)

    def test_database_cannot_be_served_as_static_content(self):
        client=http.client.HTTPConnection(self.host)
        client.request('GET','/../runtime/hokieday.sqlite3')
        response=client.getresponse();self.assertEqual(response.status,404);response.read();client.close()


if __name__=='__main__': unittest.main()
