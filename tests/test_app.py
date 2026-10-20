"""Regression tests use real HTTP requests and a disposable SQLite database."""
import concurrent.futures
import http.cookiejar
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None

class AppTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server=app.ThreadingHTTPServer(('127.0.0.1',0),app.App)
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True)
        cls.thread.start()
        cls.base='http://127.0.0.1:'+str(cls.server.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close();cls.thread.join()

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='toolstock-tests-')
        app.DATA_DIR=Path(self.temp.name)
        app.UPLOAD_DIR=app.DATA_DIR/'photos'
        app.DB_PATH=app.DATA_DIR/'test.sqlite3'
        app.init_db()
        self.cookies=http.cookiejar.CookieJar()
        self.client=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookies),NoRedirect())
        self.request('/setup',dict(username='testadmin',password='test-password-123',password2='test-password-123'))
        self.request('/login',dict(username='testadmin',password='test-password-123'))
        self.csrf=self.scalar('SELECT csrf FROM sessions')
        self.post('/admin/machine',name='Станок первый')
        self.post('/admin/machine',name='Станок второй')
        self.post('/admin/worker',name='Иванов Иван',personnel_no='001',machine_ids=['1','2'])

    def tearDown(self):
        self.temp.cleanup()

    def request(self,path,data=None,client=None):
        raw=urllib.parse.urlencode(data,doseq=True).encode() if data is not None else None
        try:
            response=(client or self.client).open(self.base+path,raw,timeout=30)
        except urllib.error.HTTPError as error:
            response=error
        with response:
            return response.code,response.read().decode('utf-8'),dict(response.headers)

    def post(self,path,request_id=None,**values):
        return self.request(path,dict(csrf=self.csrf,request_id=request_id or uuid.uuid4().hex,**values))

    def scalar(self,sql,args=()):
        with app.db() as con:
            row=con.execute(sql,args).fetchone()
            return row[0] if row else None

    def product(self,name='Фреза тестовая',**values):
        data=dict(item_class='consumable',category='Прочее',name=name,unit='шт',min_qty='5',price_per_unit='100',model='TEST',diameter='10')
        data.update(values)
        result=self.post('/product/save',**data)
        self.assertEqual(303,result[0],result[1])
        return self.scalar('SELECT MAX(id) FROM products')

    def receipt(self,pid,qty=10,**kw):
        return self.post('/transaction/receipt',product_id=pid,qty=qty,**kw)

    def issue(self,pid,qty=1,**values):
        data=dict(product_id=pid,qty=qty,worker_id=1,machine_id=1,state='new');data.update(values)
        return self.post('/transaction/issue',**data)

    def settle(self,pid,qty=1,**values):
        data=dict(product_id=pid,qty=qty,worker_id=1,machine_id=1,state_from='new',action='return');data.update(values)
        return self.post('/transaction/settle',**data)

    def balance(self,pid,state='new',**kw):
        with app.db() as con:return app.balance(con,pid,state,**kw)

    def regrind(self):
        pid=self.product();self.receipt(pid,5);self.issue(pid,2);self.settle(pid,2,action='regrind')
        self.assertEqual(303,self.post('/transaction/regrind/send',product_id=pid,qty=2)[0])
        self.assertEqual(303,self.post('/transaction/regrind/receive',product_id=pid,qty=2,diameter='9,8',l1='19,5')[0])
        target=self.scalar('SELECT MAX(id) FROM products')
        outgoing=self.scalar("SELECT id FROM movements WHERE kind='regrind_receive' AND product_id=?",(pid,))
        incoming=self.scalar("SELECT id FROM movements WHERE kind='regrind_receive' AND product_id=?",(target,))
        return pid,target,outgoing,incoming

    def test_full_stock_cycle(self):
        p=self.product();self.receipt(p,10);self.issue(p,4);self.settle(p,2)
        self.assertEqual(6,self.balance(p));self.assertEqual(2,self.balance(p,'used'))
        self.assertEqual(2,self.balance(p,worker_id=1,machine_id=1))
        self.assertEqual(400,self.issue(p,7)[0])
        self.assertEqual(400,self.settle(p,3)[0])
        self.assertEqual(303,self.post('/transaction/writeoff',product_id=p,qty=1,state='used',note='Брак')[0])
        self.assertEqual(1,self.balance(p,'used'))

    def test_cancel_regrind_from_either_side(self):
        for side in ('outgoing','incoming'):
            p,target,outgoing,incoming=self.regrind()
            result=self.post('/movement/cancel',id=outgoing if side=='outgoing' else incoming,reason='Ошибка приёмки')
            self.assertEqual(303,result[0],result[1])
            self.assertEqual(2,self.balance(p,'regrind'))
            self.assertEqual(0,self.balance(target,'sharpened'))
            self.assertEqual(2,self.scalar('SELECT COUNT(*) FROM movements WHERE id IN (?,?) AND voided_at IS NOT NULL',(outgoing,incoming)))
            self.assertEqual(2,self.scalar('SELECT COUNT(*) FROM movement_changes WHERE movement_id IN (?,?)',(outgoing,incoming)))

    def test_edit_regrind_updates_both_parts(self):
        p,target,outgoing,incoming=self.regrind()
        self.assertEqual(303,self.post('/movement/edit',id=incoming,qty=1,reason='Уточнение количества',note='Принята одна')[0])
        self.assertEqual(1,self.balance(p,'regrind'));self.assertEqual(1,self.balance(target,'sharpened'))
        self.assertEqual([1,1],[self.scalar('SELECT qty FROM movements WHERE id=?',(i,)) for i in (outgoing,incoming)])

    def test_regrind_cancel_rolls_back_after_target_consumed(self):
        p,target,outgoing,incoming=self.regrind()
        self.issue(target,2,state='sharpened')
        self.assertEqual(400,self.post('/movement/cancel',id=outgoing,reason='Ошибка приёмки')[0])
        self.assertEqual(0,self.balance(p,'regrind'));self.assertEqual(0,self.balance(target,'sharpened'))
        self.assertEqual(0,self.scalar('SELECT COUNT(*) FROM movement_changes'))
        self.assertIsNone(self.scalar('SELECT voided_at FROM movements WHERE id=?',(outgoing,)))

    def test_regrind_edit_rolls_back_after_target_consumed(self):
        p,target,outgoing,incoming=self.regrind();self.issue(target,2,state='sharpened')
        self.assertEqual(400,self.post('/movement/edit',id=outgoing,qty=1,reason='Уточнение',note='')[0])
        self.assertEqual(2,self.scalar('SELECT qty FROM movements WHERE id=?',(incoming,)))
        self.assertEqual(0,self.scalar('SELECT COUNT(*) FROM movement_changes'))

    def test_concurrent_writeoff(self):
        p=self.product();self.receipt(p,3)
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            results=list(pool.map(lambda _:self.post('/transaction/writeoff',product_id=p,qty=3,state='new',note='Брак')[0],range(2)))
        self.assertEqual([303,400],sorted(results));self.assertEqual(0,self.balance(p))

    def test_concurrent_settlement(self):
        p=self.product();self.receipt(p,1);self.issue(p,1)
        with concurrent.futures.ThreadPoolExecutor(2) as pool:results=list(pool.map(lambda _:self.settle(p)[0],range(2)))
        self.assertEqual([303,400],sorted(results));self.assertEqual(1,self.balance(p,'used'))

    def test_concurrent_regrind_send(self):
        p=self.product();self.receipt(p,1);self.issue(p,1);self.settle(p,action='regrind')
        with concurrent.futures.ThreadPoolExecutor(2) as pool:results=list(pool.map(lambda _:self.post('/transaction/regrind/send',product_id=p,qty=1)[0],range(2)))
        self.assertEqual([303,400],sorted(results));self.assertEqual(1,self.balance(p,'regrind'))

    def test_bulk_regrind_snapshot_and_duplicate(self):
        p=self.product();self.receipt(p,3);self.issue(p,3);self.settle(p,2,action='regrind')
        old_snapshot=json.dumps([[p,2.0]],separators=(',',':'))
        self.settle(p,1,action='regrind')
        self.assertEqual(400,self.post('/transaction/regrind/send-all',queue_snapshot=old_snapshot)[0])
        self.assertEqual(3,self.balance(p,'regrind_queue'))
        token=uuid.uuid4().hex;data=json.dumps([[p,3.0]],separators=(',',':'))
        self.assertEqual(303,self.post('/transaction/regrind/send-all',queue_snapshot=data,request_id=token)[0])
        self.assertEqual(303,self.post('/transaction/regrind/send-all',queue_snapshot=data,request_id=token)[0])
        self.assertEqual(3,self.balance(p,'regrind'));self.assertEqual(0,self.balance(p,'regrind_queue'))

    def test_concurrent_regrind_receive(self):
        p=self.product();self.receipt(p,1);self.issue(p,1);self.settle(p,action='regrind');self.post('/transaction/regrind/send',product_id=p,qty=1)
        with concurrent.futures.ThreadPoolExecutor(2) as pool:results=list(pool.map(lambda _:self.post('/transaction/regrind/receive',product_id=p,qty=1)[0],range(2)))
        self.assertEqual([303,400],sorted(results));self.assertEqual(0,self.balance(p,'regrind'))
        self.assertEqual(2,self.scalar('SELECT COUNT(*) FROM products'))

    def test_concurrent_issue(self):
        p=self.product();self.receipt(p,1)
        with concurrent.futures.ThreadPoolExecutor(2) as pool:results=list(pool.map(lambda _:self.issue(p)[0],range(2)))
        self.assertEqual([303,400],sorted(results));self.assertEqual(0,self.balance(p))

    def test_duplicate_submission_is_idempotent(self):
        p=self.product();token=uuid.uuid4().hex
        first=self.receipt(p,3,request_id=token);second=self.receipt(p,3,request_id=token)
        self.assertEqual(303,first[0]);self.assertEqual(303,second[0]);self.assertEqual(first[2]['Location'],second[2]['Location'])
        self.assertEqual(3,self.balance(p))
        self.assertEqual(400,self.receipt(p,4,request_id=token)[0]);self.assertEqual(3,self.balance(p))

    def test_concurrent_duplicate_submission(self):
        p=self.product();token=uuid.uuid4().hex
        with concurrent.futures.ThreadPoolExecutor(2) as pool:results=list(pool.map(lambda _:self.receipt(p,3,request_id=token)[0],range(2)))
        self.assertEqual([303,303],results);self.assertEqual(3,self.balance(p))

    def test_missing_request_id_rejected(self):
        p=self.product()
        self.assertEqual(400,self.request('/transaction/receipt',dict(csrf=self.csrf,product_id=p,qty=1))[0]);self.assertEqual(0,self.balance(p))

    def test_negative_balance_not_hidden(self):
        p=self.product()
        with app.db() as con:
            actor=con.execute('SELECT * FROM users LIMIT 1').fetchone()
            app.movement(con,p,'writeoff_stock',1,actor,state_from='new')
        self.assertEqual(-1,self.balance(p));self.assertIn('Ошибка учёта',self.request('/catalog')[1])

    def test_cyrillic_search_and_filters(self):
        p=self.product(manufacturer='Завод',location='А-12',diameter='9,8',radius='0,8')
        for term in ['фреза','ФРЕЗА','Фреза']:
            self.assertIn('Фреза тестовая',self.request('/catalog?q='+urllib.parse.quote(term))[1])
        self.assertIn('Фреза тестовая',self.request('/catalog?diameter_from=9.5&diameter_to=10&manufacturer='+urllib.parse.quote('завод'))[1])
        self.assertNotIn('Фреза тестовая',self.request('/catalog?diameter_from=11')[1])
        params=json.loads(self.scalar('SELECT params_json FROM products WHERE id=?',(p,)))
        self.assertEqual('0.8',params['radius'])
        self.assertNotIn('Фреза тестовая',self.request('/catalog?state=new')[1]);self.receipt(p,1)
        self.assertIn('Фреза тестовая',self.request('/catalog?state=new')[1])

    def test_fractional_dimensions_integer_tooth_count(self):
        self.assertEqual('0.8',app.normalized_tool_params({'radius':'0.8'})['radius'])
        self.assertEqual('9.8',app.normalized_tool_params({'diameter':'9,8'})['diameter'])
        with self.assertRaises(ValueError):app.normalized_tool_params({'z':'2.5'})
        with self.assertRaises(ValueError):app.normalized_tool_params({'radius':'nan'})

    def test_script_json_escapes_html_boundary(self):
        payload='</script><script>window.x=1</script>&\u2028'
        encoded=app.script_json({'name':payload})
        self.assertNotIn('<',encoded);self.assertEqual(payload,json.loads(encoded)['name'])
        self.post('/admin/machine',name=payload);self.post('/admin/worker',name='Тест скрипта',machine_ids=['3'])
        page=self.request('/transactions')[1]
        self.assertNotIn('"name": "</script>',page)

    def test_reports_have_expected_filter_ids(self):
        page=self.request('/reports')[1]
        self.assertRegex(page,r'<select name="machine"[^>]*id=report-machine')
        self.assertRegex(page,r'<select name="worker"[^>]*id=report-worker')

    def test_writeoff_requires_reason(self):
        p=self.product();self.receipt(p,3)
        self.assertEqual(400,self.post('/transaction/writeoff',product_id=p,qty=1,state='new')[0]);self.assertEqual(3,self.balance(p))
        self.issue(p);self.assertEqual(400,self.settle(p,action='writeoff')[0]);self.assertEqual(1,self.balance(p,worker_id=1,machine_id=1))

    def test_receipt_cancel_protects_later_operations(self):
        p=self.product();self.receipt(p,3);mid=self.scalar('SELECT MAX(id) FROM movements');self.issue(p)
        self.assertEqual(400,self.post('/movement/cancel',id=mid,reason='Ошибка поступления')[0]);self.assertEqual(2,self.balance(p))

    def test_worker_permissions_and_csrf(self):
        self.post('/admin/user',username='testworker',password='test-password-123',role='WORKER',worker_id='1')
        jar=http.cookiejar.CookieJar();client=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar),NoRedirect())
        self.request('/login',dict(username='testworker',password='test-password-123'),client)
        self.assertEqual(403,self.request('/admin',client=client)[0]);self.assertEqual(403,self.request('/transactions',client=client)[0])
        self.assertEqual(200,self.request('/dashboard',client=client)[0])
        self.assertEqual(403,self.request('/transaction/receipt',dict(product_id=1,qty=2))[0])

    def test_inactive_station_cannot_receive_issue(self):
        p=self.product();self.receipt(p,1);self.post('/admin/machine/delete',id=1)
        self.assertEqual(400,self.issue(p)[0]);self.assertEqual(1,self.balance(p))

    def test_init_repeatable_preserves_records(self):
        p=self.product();self.receipt(p,3)
        # Earlier releases have the same stock schema but no deduplication table.
        with app.db() as con:con.execute('DROP TABLE submitted_forms')
        app.init_db();app.init_db()
        self.assertEqual(3,self.balance(p));self.assertEqual(1,self.scalar('SELECT COUNT(*) FROM movements'))
        self.assertEqual('submitted_forms',self.scalar("SELECT name FROM sqlite_master WHERE name='submitted_forms'"))
        self.assertEqual(1,self.scalar('SELECT COUNT(*) FROM users'))

    def test_purchase_csv(self):
        p=self.product();self.assertEqual(303,self.post('/purchasing/create',**{f'qty_{p}':5})[0])
        rid=self.scalar('SELECT MAX(id) FROM purchase_requests')
        self.assertIn('500',self.request('/purchasing?export=1&id='+str(rid))[1])
        self.assertEqual(200,self.request('/reports?export=balances')[0]);self.assertEqual(200,self.request('/reports?export=history')[0])

    def test_operation_navigation_and_prefill(self):
        p=self.product();page=self.request('/transactions?op=issue&product_id='+str(p))[1]
        self.assertIn('data-op="issue" aria-current="page"',page)
        self.assertIn('"product": {"id": '+str(p),page)
        self.assertIn('app.js',page)

if __name__=='__main__':unittest.main(verbosity=2)
