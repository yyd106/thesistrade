"""Synthetic SEC transport/config tests: no live network, model, contact or production data."""
import copy
import html
import io
import json
import multiprocessing
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from contextlib import nullcontext
from datetime import datetime, timezone
from email.message import Message
from email.utils import format_datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ashare import build, config_ops, dynamic_sources, macro_sources, pipeline, sec_source, sources, us_company
from ashare.settings import validate_settings
from ashare.storage import Store, now

ROOT = Path(__file__).resolve().parents[1]
CONTACT = 'synthetic-research@example.invalid'
CONFIG = {'sec_enabled': True, 'sec_contact_email': CONTACT}


class Clock:
    def __init__(self): self.at = 1000.0
    def time(self): return self.at
    def sleep(self, seconds): self.at += seconds


class Budget:
    def __init__(self): self.calls = 0
    def reserve(self): self.calls += 1
    def serialized(self): return nullcontext()


class Response(io.BytesIO):
    def __init__(self, body=b'{}', url='https://data.sec.gov/data.json'):
        super().__init__(body)
        self.url, self.headers = url, {}


def reserve_in_process(root, queue):
    budget = sec_source.SharedBudget(root)
    with budget.serialized():
        budget.reserve()
        queue.put(time.monotonic())


class SECTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.network = patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('LIVE_NETWORK_FORBIDDEN'))
        self.network.start()

    def tearDown(self):
        self.network.stop()
        self.tmp.cleanup()

    def test_default_disabled_boolean_validation_and_private_key_forbidden(self):
        config = json.loads((ROOT/'config.example.json').read_text())
        validate_settings(config)
        self.assertIs(config['sec_enabled'], False)
        self.assertEqual(config_ops.classify('sec_enabled'), 'STRATEGY')
        self.assertEqual(config_ops.classify('sec_contact_email'), 'FORBIDDEN')
        for value in (1, 'true', None, []):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'sec_enabled'):
                validate_settings(dict(config, sec_enabled=value))

    def test_disabled_missing_and_invalid_contact_never_call_any_fetch(self):
        store = Store(self.root/'data')
        try:
            for config in ({}, {'sec_enabled':False,'sec_contact_email':CONTACT},
                           {'sec_enabled':True}, {'sec_enabled':True,'sec_contact_email':'  '},
                           {**CONFIG,'deployment_role':'cloud'}):
                fetch = Mock(side_effect=AssertionError('FETCH_FORBIDDEN'))
                with self.assertRaises(sec_source.SECUnavailable):
                    us_company.collect(store, 'US:TEST', now(), fetch_fn=fetch, config=config)
                packet = us_company.dossier(store, 'US:TEST', now(), refresh=True, config=config)
                self.assertNotEqual(packet['status'], 'READY')
                fetch.assert_not_called()
            self.assertEqual(store.db.execute('SELECT count(*) FROM data_attempts').fetchone()[0], 0)
        finally: store.close()

    def test_contact_validation_rejects_injection_and_never_echoes_value(self):
        config = json.loads((ROOT/'config.example.json').read_text())
        for value in (' ', 'not-an-email', 'a@b', 'a..b@example.invalid', 'a.@example.invalid', CONTACT+'\r\nX-Injected: yes', 'x'*255+'@example.invalid', {}, 1):
            with self.subTest(kind=type(value).__name__):
                self.assertIsNone(sec_source.contact({'sec_contact_email':value}))
                with self.assertRaisesRegex(ValueError, '^SEC联系方式格式无效；未回显私有值$'):
                    validate_settings(dict(config, sec_contact_email=value))

    def test_two_exact_hosts_get_contact_but_generic_fetch_cannot_bypass(self):
        requests = []
        class Opener:
            def open(self, req, timeout):
                requests.append(req)
                return Response(url=req.full_url)
        client = sec_source.Client(CONFIG, self.root, opener_factory=lambda handler:Opener(), budget=Budget())
        for host in ('www.sec.gov','data.sec.gov'):
            client.fetch('https://'+host+'/example.json')
            with self.assertRaisesRegex(ValueError, '独立受控客户端'):
                sources.fetch('https://'+host+'/example.json')
        self.assertEqual(len(requests), 2)
        for request in requests:
            self.assertEqual(request.get_header('User-agent'), 'ThesisTrade personal research '+CONTACT)
            self.assertIsNone(request.get_header('Referer'))

    def test_other_sources_keep_their_original_headers_and_fetch(self):
        opener = Mock()
        opener.open.return_value = Response(b'normal-source', 'https://www.cninfo.com.cn/example')
        with patch('ashare.sources.urllib.request.build_opener', return_value=opener):
            self.assertEqual(sources.fetch('https://www.cninfo.com.cn/example'), b'normal-source')
        request = opener.open.call_args.args[0]
        self.assertNotIn(CONTACT, str(request.header_items()))
        self.assertEqual(request.get_header('User-agent'), 'Mozilla/5.0 (personal research prototype)')

    def test_dynamic_sec_rss_and_redirect_bypass_are_rejected_non_sec_is_unchanged(self):
        with self.assertRaisesRegex(ValueError,'独立受控客户端'):
            dynamic_sources.fetch('https://www.sec.gov/news/pressreleases.rss')
        with self.assertRaisesRegex(ValueError,'独立受控客户端'):
            dynamic_sources.Redirect().redirect_request(urllib.request.Request('https://www.fda.gov/a'),None,302,'',{},'https://www.sec.gov/x')
        opener=Mock();opener.open.return_value=Response(b'normal-news','https://www.fda.gov/a')
        with patch('ashare.dynamic_sources.urllib.request.build_opener',return_value=opener):
            self.assertEqual(dynamic_sources.fetch('https://www.fda.gov/a'),b'normal-news')
        request=opener.open.call_args.args[0]
        self.assertEqual(request.get_header('User-agent'),'Mozilla/5.0')
        self.assertNotIn(CONTACT,str(request.header_items()))

    def test_bad_urls_rejected_before_transport(self):
        factory = Mock(side_effect=AssertionError('OPENER_FORBIDDEN'))
        client = sec_source.Client(CONFIG, self.root, opener_factory=factory, budget=Budget())
        for url in ('http://www.sec.gov/x','https://www.sec.gov.evil.invalid/x',
                    'https://www.sec.gov./x','https://user@www.sec.gov/x','https://@www.sec.gov/x',
                    'https://data.sec.gov:444/x','https://data.sec.gov:bad/x',
                    'https://www.cninfo.com.cn/x','https://www.sec.gov/x\nInjected'):
            with self.subTest(url=url), self.assertRaisesRegex(sec_source.SECUnavailable, '^SEC_URL_REJECTED$'):
                client.fetch(url)
        factory.assert_not_called()

    def test_redirect_host_boundary_and_every_allowed_hop_uses_budget(self):
        budget = Budget()
        redirect = sec_source.SECRedirect(budget)
        request = urllib.request.Request('https://www.sec.gov/a', headers={'User-Agent':CONTACT})
        for url in ('https://www.sec.gov/b', 'https://data.sec.gov/b'):
            result = redirect.redirect_request(request, None, 302, '', {}, url)
            self.assertEqual(result.get_header('User-agent'), CONTACT)
        self.assertEqual(budget.calls, 2)
        for url in ('https://www.cninfo.com.cn/x','https://evil.invalid/x'):
            with self.assertRaises(sec_source.SECUnavailable):
                redirect.redirect_request(request, None, 302, '', {}, url)
        with self.assertRaisesRegex(ValueError, '独立受控客户端'):
            sources.SafeRedirect().redirect_request(urllib.request.Request('https://www.cninfo.com.cn/a'), None, 302, '', {}, 'https://data.sec.gov/x')
        redirect.redirect_request(request, None, 302, '', {}, 'https://www.sec.gov/c')
        with self.assertRaisesRegex(sec_source.SECUnavailable, 'SEC_REDIRECT_LIMIT'):
            redirect.redirect_request(request, None, 302, '', {}, 'https://www.sec.gov/d')

    def test_403_429_no_immediate_retry_and_retry_after_shared_across_clients(self):
        for status in (403,429):
            root = self.root/str(status)
            clock = Clock()
            budget = sec_source.SharedBudget(root, clock=clock.time, sleep=clock.sleep)
            headers = Message();headers['Retry-After']='180'
            opener = Mock();opener.open.side_effect=urllib.error.HTTPError('https://www.sec.gov/x',status,CONTACT,headers,None)
            client=sec_source.Client(CONFIG,root,budget=budget,opener_factory=lambda handler:opener)
            with self.assertRaisesRegex(sec_source.SECUnavailable, '^SEC_HTTP_'+str(status)+'$'):
                client.fetch('https://www.sec.gov/x')
            self.assertEqual(opener.open.call_count,1)
            other=sec_source.SharedBudget(root,clock=clock.time,sleep=clock.sleep)
            with self.assertRaisesRegex(sec_source.SECUnavailable,'SEC_BACKOFF'):other.reserve()
            clock.sleep(179)
            with self.assertRaises(sec_source.SECUnavailable):other.reserve()
            clock.sleep(1);other.reserve()
            self.assertNotIn(CONTACT,(root/'cache/sec-budget.json').read_text())

    def test_retry_after_http_date_and_invalid_budget_fail_closed(self):
        clock=Clock();budget=sec_source.SharedBudget(self.root,clock=clock.time,sleep=clock.sleep)
        budget.backoff(format_datetime(datetime.fromtimestamp(1300,timezone.utc),usegmt=True))
        self.assertEqual(json.loads((self.root/'cache/sec-budget.json').read_text())['blocked_until'],1300)
        (self.root/'cache/sec-budget.json').write_text('broken')
        with self.assertRaisesRegex(sec_source.SECUnavailable,'SEC_BUDGET_UNAVAILABLE'):budget.reserve()

    def test_forward_clock_jump_cannot_remove_minimum_request_spacing(self):
        clock=Clock();budget=sec_source.SharedBudget(self.root,clock=clock.time,sleep=clock.sleep)
        with budget.serialized():budget.reserve()
        clock.at+=3600;before=clock.at
        with budget.serialized():budget.reserve()
        self.assertGreaterEqual(clock.at-before,sec_source.MIN_INTERVAL)

    def test_transient_retries_are_bounded_budgeted_and_errors_are_sanitized(self):
        budget=Budget();opener=Mock();opener.open.side_effect=OSError('request User-Agent '+CONTACT)
        client=sec_source.Client(CONFIG,self.root,budget=budget,opener_factory=lambda handler:opener)
        with patch('ashare.sec_source.time.sleep'),self.assertRaisesRegex(sec_source.SECUnavailable,'^SEC_NETWORK_FAILURE$'):
            client.fetch('https://www.sec.gov/x')
        self.assertEqual(opener.open.call_count,2);self.assertEqual(budget.calls,2)

    def test_private_echo_is_not_returned_or_persisted(self):
        encoded_json=json.dumps({'name':CONTACT}).replace('@',r'\u0040').replace('.',r'\u002e').encode()
        encoded_html=('<html>'+CONTACT.replace('@','&#64;').replace('.','&#46;')+'</html>').encode()
        for body in (CONTACT.encode(),encoded_json,encoded_html):
            opener=Mock();opener.open.return_value=Response(body)
            client=sec_source.Client(CONFIG,self.root,budget=Budget(),opener_factory=lambda handler:opener)
            with self.assertRaisesRegex(sec_source.SECUnavailable,'^SEC_PRIVATE_CONTACT_IN_RESPONSE$'):
                client.fetch('https://www.sec.gov/x')
        self.assertEqual(list(self.root.iterdir()),[])

    def test_private_echo_case_and_bounded_recursive_entity_decoding(self):
        for value in (CONTACT.upper(), CONTACT.replace('@', '&#64;'),
                      html.escape(CONTACT.upper().replace('@', '&#64;'))):
            with self.subTest(value=value), self.assertRaisesRegex(sec_source.SECUnavailable,'^SEC_PRIVATE_CONTACT_IN_RESPONSE$'):
                sec_source.reject_private_value({'title':value},CONTACT)
        nested=CONTACT.replace('@','&#64;')
        for _ in range(12):nested=html.escape(nested)
        with self.assertRaisesRegex(sec_source.SECUnavailable,'^SEC_RESPONSE_DECODING_LIMIT$'):
            sec_source.reject_private_value(nested,CONTACT)

    def test_sec_rss_disabled_is_explicit_and_enabled_uses_same_client_only(self):
        feed=('SEC监管公告','https://www.sec.gov/news/pressreleases.rss')
        stamp='2026-11-07T00:00:00+00:00'
        xml=b'<rss><channel><item><title>Synthetic SEC release</title><link>https://www.sec.gov/news/example</link><pubDate>Fri, 06 Nov 2026 23:00:00 GMT</pubDate><description>Synthetic disclosure.</description></item></channel></rss>'
        store=Store(self.root/'data')
        try:
            generic=Mock(side_effect=AssertionError('GENERIC_SEC_FORBIDDEN'))
            with patch('ashare.macro_sources.FEEDS',[feed]),patch('ashare.sec_source.Client') as client:
                result=macro_sources.collect(store,stamp,stamp,stamp,generic,config={'sec_enabled':False})
                client.assert_not_called()
            self.assertEqual(result['paused_sources'],{'SEC监管公告':'SEC_DISABLED'})
            self.assertEqual(result['failures'],[]);generic.assert_not_called()
            self.assertEqual(store.db.execute('SELECT status FROM dynamic_feed_checks').fetchone()[0],'SEC_DISABLED')
            with patch('ashare.macro_sources.FEEDS',[feed]),patch('ashare.sec_source.Client') as client:
                client.return_value.fetch.return_value=xml
                result=macro_sources.collect(store,'2026-11-06T00:00:00+00:00','2026-11-07T01:00:00+00:00','2026-11-07T01:00:00+00:00',generic,config=CONFIG)
                client.assert_called_once_with(CONFIG,store.root)
                client.return_value.fetch.assert_called_once_with(feed[1],max_bytes=4_000_000)
            self.assertEqual(result['added'],1);self.assertEqual(result['failures'],[])
            generic.assert_not_called()
        finally:store.close()

    def test_rss_multi_stage_entity_decoding_cannot_persist_contact(self):
        encoded=CONTACT.replace('@','&amp;#64;')
        xml=('<rss><channel><item><title>'+encoded+'</title><link>https://www.sec.gov/news/example</link><pubDate>Fri, 06 Nov 2026 23:00:00 GMT</pubDate><description>Synthetic.</description></item></channel></rss>').encode()
        opener=Mock();opener.open.return_value=Response(xml,'https://www.sec.gov/news/pressreleases.rss')
        client=sec_source.Client(CONFIG,self.root,budget=Budget(),opener_factory=lambda handler:opener)
        store=Store(self.root/'data')
        try:
            with patch('ashare.macro_sources.FEEDS',[('SEC监管公告','https://www.sec.gov/news/pressreleases.rss')]),patch('ashare.sec_source.Client',return_value=client):
                result=macro_sources.collect(store,'2026-11-06T00:00:00+00:00','2026-11-07T00:00:00+00:00','2026-11-07T00:00:00+00:00',config=CONFIG)
            self.assertEqual(result['added'],0)
            self.assertEqual(result['failures'],['SEC监管公告：SEC_PRIVATE_CONTACT_IN_RESPONSE'])
            self.assertEqual(store.db.execute('SELECT count(*) FROM dynamic_news').fetchone()[0],0)
            self.assertFalse(any((store.root/'raw').rglob('*.*')))
            self.assertNotIn(CONTACT,'\n'.join(store.db.iterdump()))
        finally:store.close()

    def test_sec_rss_parser_failure_never_echoes_a_private_field(self):
        store=Store(self.root/'data')
        try:
            with patch('ashare.macro_sources.FEEDS',[('SEC监管公告','https://www.sec.gov/news/pressreleases.rss')]),patch('ashare.sec_source.Client') as client,patch('ashare.macro_sources.parse_feed',side_effect=ValueError(CONTACT)):
                client.return_value.fetch.return_value=b'<rss/>'
                result=macro_sources.collect(store,now(),now(),now(),config=CONFIG)
            self.assertEqual(result['failures'],['SEC监管公告：SEC_PARSE_OR_STORAGE_FAILURE'])
            self.assertNotIn(CONTACT,'\n'.join(store.db.iterdump()))
        finally:store.close()

    def test_success_collect_uses_all_four_sec_resource_types_and_two_filings(self):
        stamp=now();day=stamp[:10]
        facts={'cik':1,'facts':{'us-gaap':{tags[0]:{'units':{'USD':[
            {'start':day[:4]+'-01-01','end':day,'filed':day,'form':'10-Q','val':100}]}}
            for tags in us_company.TAGS.values()}}}
        submissions={'name':'Synthetic issuer','tickers':['TEST'],'filings':{'recent':{
            'form':['10-Q','8-K'],'filingDate':[day,day],
            'accessionNumber':['0000000001-26-000001','0000000001-26-000002'],
            'primaryDocument':['quarter.htm','update.htm']}}}
        requested=[]
        def fetch(url,**kwargs):
            requested.append(url)
            if url.endswith('company_tickers.json'):return json.dumps({'0':{'ticker':'TEST','cik_str':1}}).encode()
            if '/companyfacts/' in url:return json.dumps(facts).encode()
            if '/submissions/' in url:return json.dumps(submissions).encode()
            return ('<html><body><p>'+'Synthetic revenue and cash. '*30+'</p></body></html>').encode()
        store=Store(self.root/'data')
        try:
            with patch('ashare.sec_source.Client') as client:
                client.return_value.fetch=fetch
                packet=us_company.collect(store,'US:TEST',stamp,config=CONFIG)
            client.assert_called_once_with(CONFIG,store.root)
            self.assertEqual(packet['status'],'READY');self.assertEqual(len(packet['documents']),2)
            self.assertEqual(len(requested),5)
            self.assertEqual(sum('/Archives/' in url for url in requested),2)
            self.assertEqual(packet['metrics']['operating_cashflow'][0]['val'],100)
        finally:store.close()

    def test_multiple_processes_share_no_burst_budget(self):
        ctx=multiprocessing.get_context('fork');queue=ctx.Queue()
        processes=[ctx.Process(target=reserve_in_process,args=(str(self.root),queue)) for _ in range(4)]
        for process in processes:process.start()
        try:
            times=sorted(queue.get(timeout=8) for _ in processes)
            for process in processes:
                process.join(timeout=8);self.assertEqual(process.exitcode,0)
            self.assertTrue(all(b-a>=0.48 for a,b in zip(times,times[1:])),times)
        finally:
            for process in processes:
                if process.is_alive():process.terminate();process.join()
            queue.close();queue.join_thread()

    def test_identity_excludes_contact_value_but_includes_effective_source(self):
        config=json.loads((ROOT/'config.example.json').read_text());validate_settings(config)
        first={**config,**CONFIG};second={**first,'sec_contact_email':'other@example.invalid'}
        disabled={**first,'sec_enabled':False};missing={**first,'sec_contact_email':None}
        self.assertEqual(build.config_fingerprint(first),build.config_fingerprint(second))
        self.assertNotEqual(build.config_fingerprint(first),build.config_fingerprint(disabled))
        self.assertNotEqual(build.config_fingerprint(first),build.config_fingerprint(missing))
        store=Store(self.root/'data')
        try:
            with patch('ashare.build.behavior_runtime_fingerprint',return_value='synthetic-runtime'):
                identity=build.behavior_info(first,store)
                self.assertEqual(identity,build.behavior_info(second,store))
                self.assertNotEqual(identity,build.behavior_info(disabled,store))
                self.assertNotEqual(identity,build.behavior_info(missing,store))
        finally:store.close()

    def test_config_show_journal_receipt_and_new_snapshots_do_not_contain_contact(self):
        from ashare.cli import extended
        from approval_fixture import grant
        raw=json.loads((ROOT/'config.example.json').read_text())
        raw.update(data_dir=str(self.root/'data'),sec_enabled=False,sec_contact_email=CONTACT)
        path=self.root/'config.json';path.write_text(json.dumps(raw))
        shown=extended(SimpleNamespace(command='config',action='show'),{'config_path':str(path)})
        self.assertEqual(shown['sec_contact_configured']['value'],True)
        self.assertNotIn(CONTACT,json.dumps(shown))
        with self.assertRaisesRegex(ValueError,'不允许通过命令修改'):
            config_ops.request_change(path,{'sec_contact_email':'other@example.invalid'},reason='synthetic')
        request=config_ops.request_change(path,{'sec_enabled':True},reason='synthetic SEC source approval')
        store=Store(self.root/'data')
        try:grant(store,request)
        finally:store.close()
        result=config_ops.apply(path,{'sec_enabled':True},reason='synthetic SEC source approval',approval_id=request['id'])
        store=Store(self.root/'data')
        try:
            rendered=json.dumps(request)+json.dumps(result)+'\n'.join(store.db.iterdump())
            self.assertNotIn(CONTACT,rendered)
        finally:store.close()
        for item in (self.root/'data/workflow/changes').glob('*'):
            self.assertNotIn(CONTACT,item.read_text())
        config=pipeline.load_config(path,configure_model=False)
        with patch('ashare.pipeline.build_packet',return_value={'source_checks':[]}),patch('ashare.pipeline.fallback',return_value={}),patch('ashare.pipeline.report',return_value='synthetic-report'):
            result=pipeline.run(config,use_model=False,collect_data=False)
        snapshot=self.root/'data/reports'/result['run_id']/'config-snapshot.json'
        self.assertNotIn(CONTACT,snapshot.read_text())
        self.assertTrue(json.loads(snapshot.read_text())['sec_contact_configured'])
        self.assertEqual(json.loads(path.read_text())['sec_contact_email'],CONTACT)

    def test_disabled_cache_is_explicitly_partial_without_refresh(self):
        store=Store(self.root/'data')
        try:
            store.add_document(symbol='US:TEST',kind='us_financial_data',title='synthetic',source='SEC',
                url='https://data.sec.gov/x',published_at=now(),pages=[(None,json.dumps({'status':'READY','metrics':{'assets':[]},'gaps':[]}))],
                raw_path='synthetic-cache.json',quality='structured_source',cloud_allowed=True)
            with patch('ashare.us_company.collect',side_effect=AssertionError('FETCH_FORBIDDEN')):
                packet=us_company.dossier(store,'US:TEST',now(),refresh=True,config={'sec_enabled':False})
            self.assertEqual(packet['status'],'PARTIAL');self.assertEqual(packet['source_status'],'SEC_DISABLED')
            self.assertIn('ready_at',packet)
        finally:store.close()

    def test_parser_or_storage_error_detail_cannot_echo_contact(self):
        store=Store(self.root/'data')
        try:
            with patch('ashare.us_company.collect',side_effect=ValueError(CONTACT)):
                packet=us_company.dossier(store,'US:TEST',now(),refresh=True,config=CONFIG)
            self.assertEqual(packet['status'],'PARTIAL')
            detail=store.db.execute('SELECT detail FROM data_attempts').fetchone()[0]
            self.assertEqual(detail,'SEC_PARSE_OR_STORAGE_FAILURE')
            self.assertNotIn(CONTACT,json.dumps(packet))
        finally:store.close()


if __name__=='__main__':unittest.main()
