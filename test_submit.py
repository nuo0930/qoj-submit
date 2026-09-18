import contextlib
import io
import os
import runpy
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ['QOJ_SUBMIT_NOVENV'] = '1'
ns = runpy.run_path(str(Path(__file__).with_name('submit')))
g = ns['cmd_status'].__globals__


def qrow(sid, user='alice', verdict='Accepted', pid='100'):
    return f'<tr><td><a href="/submission/{sid}">{sid}</a></td><td><a href="/user/profile/{user}">{user}</a></td><td><a href="/contest/12/problem/{pid}">Secret title</a></td><td class="uoj-status">{verdict}</td><td>42ms</td><td>128MB</td></tr>'


def cfrow(sid, verdict='Wrong answer on test 7'):
    return f'<tr data-submission-id="{sid}"><td><a href="/contest/12/problem/A">A - Secret title</a></td><td><span class="verdict-rejected">{verdict}</span></td></tr>'


def contest(cf=False):
    site = 'https://codeforces.com' if cf else 'https://qoj.ac'
    return dict(site=site, contest_url=site+'/contest/12', contest_id='12', username='alice', problems={'A': {'id': 'A' if cf else '100', 'title': 'Secret title'}})


class RegressionTests(unittest.TestCase):
    def test_verdicts(self):
        for raw, expected in [('Accepted', 'AC'), ('WRONG_ANSWER', 'WA'), ('Time limit exceeded on test 8', 'TLE'), ('Memory Limit Exceeded', 'MLE'), ('Runtime error on test 2', 'RE'), ('Compilation error', 'CE'), ('Output limit exceeded', 'OLE'), ('Reject on test 4', 'REJECT'), ('Running on test 5', 'JUDGING'), ('Waiting', 'PENDING'), ('WA on test 6', 'WA')]:
            with self.subTest(raw=raw):
                self.assertEqual(ns['short_verdict'](raw), expected)

    def test_qoj_pagination_filters_and_deduplicates(self):
        c = contest()
        client = Mock()
        base = c['contest_url']+'/submissions'
        pages = ['<table>'+qrow(4)+qrow(5, 'bob')+'</table><a href="?page=2">2</a>', '<table>'+qrow(4)+qrow(2, verdict='Time limit exceeded')+'</table><a href="?page=2">2</a>']
        client.get.side_effect = [(200, base+'?submitter=alice', pages[0]), (200, base+'?page=2&submitter=alice', pages[1])]
        rows = ns['fetch_all_contest_submissions'](client, c)
        self.assertEqual([r['id'] for r in rows], ['4','2'])
        self.assertEqual(client.get.call_count, 2)
        for call in client.get.call_args_list:
            self.assertIn('submitter=alice', call.args[0])

    def test_cf_my_pagination(self):
        c = contest(True)
        client = Mock()
        base = c['contest_url']+'/my'
        client.get.side_effect = [(200, base, '<table>'+cfrow(4)+'</table><a href="/contest/12/my/page/2">2</a>'), (200, base+'/page/2', '<table>'+cfrow(2)+'</table>')]
        self.assertEqual([r['id'] for r in ns['fetch_all_contest_submissions'](client,c)], ['4','2'])
        self.assertEqual(client.get.call_args_list[0].args[0], base)

    def test_limit(self):
        c = contest()
        client = Mock()
        client.get.return_value = (200,c['contest_url']+'/submissions','<table>'+qrow(4)+qrow(3)+'</table><a href="?page=2">2</a>')
        self.assertEqual(len(ns['fetch_all_contest_submissions'](client,c,1)),1)
        self.assertEqual(client.get.call_count,1)

    def test_no_username_never_requests_public_list(self):
        c = contest(); c['username']=''
        client = Mock()
        with self.assertRaises(OSError): ns['fetch_all_contest_submissions'](client,c)
        client.get.assert_not_called()

    def test_bad_list_not_empty_success(self):
        for status, url, body in [(403,'https://qoj.ac','<table/>'), (200,'https://qoj.ac/login','<table/>'), (200,'https://qoj.ac','unexpected page')]:
            with self.assertRaises(OSError): ns['validate_submission_page'](status,url,body)

    def test_status_only_two_columns_and_current_account(self):
        buf=io.StringIO()
        rows=ns['parse_submission_rows']('<table>'+qrow(42,verdict='Time limit exceeded on test 9')+'</table>')
        cfg={'accounts': {'qoj.ac': {'cookie':'x=1','username':'new_user'}}}
        fetch=Mock(return_value=rows)
        with patch.dict(g,load_global=lambda:cfg,require_contest=lambda:(Path('unused'),contest()),make_client=Mock(),fetch_all_contest_submissions=fetch),contextlib.redirect_stdout(buf):
            ns['cmd_status'](SimpleNamespace(limit=0,debug=False))
        self.assertEqual(buf.getvalue().split(), ['ID', 'Problem', 'Status', 'Submitted', '42', 'A', 'TLE', '-'])
        self.assertEqual(fetch.call_args.args[1]['username'],'new_user')

    def test_qoj_skips_token_only_statement(self):
        client=Mock(); client.site='https://qoj.ac'
        statement='<input name="_token" value="old"><a href="/contest/12/problem/100/submit">Submit</a>'
        form='<form action="/contest/12/problem/100/submit"><input name="_token" value="new"><textarea name="answer_answer_editor"></textarea></form>'
        client.get.side_effect=[(200,'https://qoj.ac/contest/12/problem/100/statement/en',statement),(200,'https://qoj.ac/contest/12/problem/100/submit',form)]
        result=ns['_fetch_problem_submit_page'](client,contest(),{'id':'100','url':'https://qoj.ac/contest/12/problem/100'})
        self.assertTrue(result[0].endswith('/submit'))
        self.assertEqual(ns['extract_token'](result[3]),'new')

    def test_post_disconnect_never_replayed(self):
        client=ns['Client']('https://qoj.ac')
        conn=Mock(); conn.getresponse.side_effect=ns['RemoteDisconnected']('lost response')
        with patch.object(client,'_connection',return_value=conn),patch.object(client,'_cffi_session',return_value=None):
            with self.assertRaises(TimeoutError): client.post_multipart('/submit',{'code':'x'},fatal=False)
        self.assertEqual(conn.request.call_count,1)

    def test_post_timeout_can_be_reconciled(self):
        client=ns['Client']('https://codeforces.com')
        with patch.object(client,'_http_once',side_effect=TimeoutError('lost')) as send:
            with self.assertRaises(TimeoutError): client.post_form('/submit',{},fatal=False)
            self.assertEqual(send.call_count,1)

    def test_get_retries_disconnect(self):
        client=ns['Client']('https://qoj.ac')
        with patch.object(client,'_http_once',side_effect=[OSError('lost'),(200,'',b'ok','utf-8')]) as send,patch.dict(g,time=Mock(wraps=ns['time'])):
            self.assertEqual(client.get('/test',retries=1,fatal=False)[2],'ok')
            self.assertEqual(send.call_count,2)

    def test_old_submission_is_never_new(self):
        for sid in ('99','100','1'):
            self.assertFalse(ns['is_new_submission_id'](sid,['100']))
        self.assertTrue(ns['is_new_submission_id']('101',['100']))

    def test_cf_timeout_reconciles_new_submission(self):
        c=contest(True); client=Mock(); client.post_form.side_effect=TimeoutError('lost response')
        new=[{'id':'101','problem_id':'A'}]
        remember=Mock()
        with patch.dict(g,fetch_cf_submission_rows=Mock(return_value=new),remember_submit=remember),contextlib.redirect_stdout(io.StringIO()):
            ns['_submit_codeforces'](client,Path('unused'),c,'A',c['problems']['A'],'int main(){}','.cpp',None,c['contest_url']+'/submit',c['contest_url']+'/submit','<input name="csrf_token" value="token">',['100'])
        self.assertEqual(remember.call_args.args[-1],'101')
        self.assertFalse(client.post_form.call_args.kwargs['fatal'])

    def test_cf_failure_does_not_claim_old_submission(self):
        c=contest(True); client=Mock(); client.post_form.return_value=(200,c['contest_url']+'/submit','')
        with patch.dict(g,wait_for_new_sid=Mock(return_value=None)),contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                ns['_submit_codeforces'](client,Path('unused'),c,'A',c['problems']['A'],'int main(){}','.cpp',None,c['contest_url']+'/submit',c['contest_url']+'/submit','<input name="csrf_token" value="token">',['100'])

    def test_contest_alias_not_filename(self):
        self.assertFalse(ns['looks_like_submit_target']('contest'))
        self.assertEqual(ns['build_parser']().parse_args(['status']).limit,0)

class LatencyTests(unittest.TestCase):
    def cfg(self):
        return {'site':'https://codeforces.com','accounts':{'codeforces.com':{'cookie':'JSESSIONID=x; 39ce7=y','username':'alice'}}}

    def test_whoami_default_never_opens_network_or_browser(self):
        client, browser = Mock(), Mock()
        out=io.StringIO()
        with patch.dict(g,load_global=self.cfg,Client=client,_cf_handle_from_firefox_profile=browser),contextlib.redirect_stdout(out):
            ns['cmd_whoami'](SimpleNamespace(debug=False,check=False))
        client.assert_not_called(); browser.assert_not_called()
        self.assertIn('alice',out.getvalue())
        self.assertIn('未联网验证',out.getvalue())

    def test_whoami_check_uses_short_single_request(self):
        client=Mock()
        client.get.return_value=(200,'https://codeforces.com/','<div class="lang-chooser"><a href="/profile/alice">alice</a></div>')
        with patch.dict(g,load_global=self.cfg,Client=Mock(return_value=client)),contextlib.redirect_stdout(io.StringIO()):
            ns['cmd_whoami'](SimpleNamespace(debug=False,check=True))
        client.get.assert_called_once_with('/',timeout=15,retries=1,fatal=False)

    def test_whoami_does_not_trust_cookies_when_check_fails(self):
        client=Mock()
        client.get.return_value=(403,'https://codeforces.com/','blocked')
        with patch.dict(g,load_global=self.cfg,Client=Mock(return_value=client)),contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()) as err:
            with self.assertRaises(SystemExit): ns['cmd_whoami'](SimpleNamespace(debug=False,check=True))
        self.assertIn('无法判断',err.getvalue())

    def test_whoami_timeout_has_no_traceback(self):
        client=Mock();client.get.side_effect=TimeoutError('slow')
        with patch.dict(g,load_global=self.cfg,Client=Mock(return_value=client)),contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()) as err:
            with self.assertRaises(SystemExit): ns['cmd_whoami'](SimpleNamespace(debug=False,check=True))
        self.assertIn('网络异常',err.getvalue())

    def test_redirects_share_budget(self):
        client=ns['Client']('https://qoj.ac')
        now=[0.0]; budgets=[]
        def request(url,data,headers,method,connect,read,**kwargs):
            budgets.append(read); now[0]+=2
            return 302,'/next',b'','utf-8'
        clock=SimpleNamespace(monotonic=lambda:now[0],sleep=lambda seconds:None)
        with patch.dict(g,time=clock),patch.object(client,'_http_once',side_effect=request):
            with self.assertRaises(TimeoutError): client.get('/',timeout=3,retries=0,fatal=False)
        self.assertEqual(budgets,[3.0,1.0])

    def test_exhausted_budget_does_not_retry(self):
        client=ns['Client']('https://qoj.ac');now=[0.0]
        def request(*args,**kwargs):
            now[0]=6.0
            raise TimeoutError('slow')
        clock=SimpleNamespace(monotonic=lambda:now[0],sleep=Mock())
        with patch.dict(g,time=clock),patch.object(client,'_http_once',side_effect=request) as request_mock:
            with self.assertRaises(TimeoutError): client.get('/',timeout=6,retries=3,fatal=False)
        self.assertEqual(request_mock.call_count,1)
        clock.sleep.assert_not_called()

    def test_trickling_body_stops_at_deadline(self):
        now=[0.0]; fp=Mock(); sock=Mock()
        def read(size):
            now[0]+=1
            return b'x'
        fp.read1.side_effect=read
        with patch.dict(g,time=SimpleNamespace(monotonic=lambda:now[0])):
            with self.assertRaises(TimeoutError): ns['Client']._read_body(fp,deadline=2,sock=sock)
        fp.read.assert_not_called()
        self.assertEqual(fp.read1.call_count,2)
        self.assertEqual([c.args[0] for c in sock.settimeout.call_args_list],[2,1])

    def test_valid_empty_list_needs_no_fallback(self):
        for func,cf in [('fetch_submission_rows',False),('fetch_cf_submission_rows',True)]:
            with self.subTest(func=func):
                c=contest(cf);client=Mock()
                client.get.return_value=(200,c['contest_url']+'/submissions','<table></table>')
                self.assertEqual(ns[func](client,c,'A' if cf else '100'),[])
                self.assertEqual(client.get.call_count,1)

class TransportTests(unittest.TestCase):
    def test_qoj_uses_browser_transport(self):
        client=ns['Client']('https://qoj.ac')
        with patch.object(client,'_cffi_http_once',return_value=(200,'',b'ok','utf-8')) as curl:
            self.assertEqual(client.get('/',fatal=False)[2],'ok')
        curl.assert_called_once()

    def test_proxy_connect_and_credentials(self):
        client=ns['Client']('https://qoj.ac'); conn=Mock()
        with patch.dict(g,getproxies=lambda:{'https':'http://user:p%40ss@127.0.0.1:7890'},proxy_bypass=lambda host:False),patch.object(ns['http'].client,'HTTPSConnection',return_value=conn) as factory:
            client._connection(ns['urlparse']('https://qoj.ac'),5)
        self.assertEqual(factory.call_args.args,('127.0.0.1',7890))
        conn.set_tunnel.assert_called_once_with('qoj.ac',443,headers={'Proxy-Authorization':'Basic dXNlcjpwQHNz'})

    def test_no_proxy_uses_direct_connection(self):
        client=ns['Client']('https://qoj.ac'); conn=Mock()
        with patch.dict(g,getproxies=lambda:{'https':'http://127.0.0.1:7890'},proxy_bypass=lambda host:True),patch.object(ns['http'].client,'HTTPSConnection',return_value=conn) as factory:
            client._connection(ns['urlparse']('https://qoj.ac'),5)
        self.assertEqual(factory.call_args.args,('qoj.ac',443))
        conn.set_tunnel.assert_not_called()

    def test_socks_not_silently_bypassed(self):
        client=ns['Client']('https://qoj.ac')
        with patch.dict(g,getproxies=lambda:{'all':'socks5://localhost:7890'},proxy_bypass=lambda host:False):
            with self.assertRaises(OSError): client._connection(ns['urlparse']('https://qoj.ac'),5)

    def test_curl_type_error_never_replays_post(self):
        client=ns['Client']('https://qoj.ac'); session=Mock()
        session.request.side_effect=TypeError('response failure')
        with patch.object(client,'_cffi_session',return_value=session):
            with self.assertRaises(TimeoutError): client.post_form('/submit',{},fatal=False)
        self.assertEqual(session.request.call_count,1)

class SubmitReliabilityTests(unittest.TestCase):
    def form(self):
        return '<form action="/contest/12/problem/100/submit"><input type="hidden" name="_token" value="fresh"><input type="hidden" name="extra" value="keep"><textarea name="answer_answer_editor"></textarea><button name="submit-answer" value="answer">Submit</button></form>'

    def send(self,client,before=None):
        c=contest()
        client.site=c['site']
        ns['_submit_qoj'](client,Path('unused'),c,c['problems']['A'],'int main(){}','.cpp',None,c['contest_url']+'/problem/100',c['contest_url']+'/problem/100',self.form(),before)

    def test_history_failure_still_allows_direct_receipt(self):
        client=Mock();client.post_multipart.return_value=(302,'https://qoj.ac/submission/101','')
        remember=Mock()
        with patch.dict(g,remember_submit=remember),contextlib.redirect_stdout(io.StringIO()): self.send(client)
        self.assertEqual(remember.call_args.args[-1],'101')
        self.assertEqual(client.post_multipart.call_count,1)
        args=client.post_multipart.call_args.args
        self.assertTrue(args[0].endswith('/submit'))
        self.assertEqual(args[1]['_token'],'fresh')
        self.assertEqual(args[1]['extra'],'keep')

    def test_list_redirect_does_not_require_another_network_request(self):
        client=Mock();client.post_multipart.return_value=(302,'https://qoj.ac/contest/12/submissions','')
        poll=Mock()
        with patch.dict(g,wait_for_new_sid=poll),contextlib.redirect_stdout(io.StringIO()) as out: self.send(client)
        poll.assert_not_called()
        self.assertIn('已接收',out.getvalue())

    def test_success_alert_is_not_rejection(self):
        client=Mock();client.post_multipart.return_value=(200,'https://qoj.ac/contest/12/submissions','<div class="alert-success">Success</div>')
        with patch.dict(g,wait_for_new_sid=Mock(return_value='101'),remember_submit=Mock()),contextlib.redirect_stdout(io.StringIO()): self.send(client,['100'])

    def test_post_timeout_queries_without_resending(self):
        client=Mock();client.post_multipart.side_effect=TimeoutError('request deadline')
        poll=Mock(return_value='101')
        with patch.dict(g,wait_for_new_sid=poll,remember_submit=Mock()),contextlib.redirect_stdout(io.StringIO()): self.send(client,['100'])
        self.assertEqual(client.post_multipart.call_count,1)
        self.assertEqual(poll.call_args.kwargs['seconds'],20)

    def test_statement_redirect_does_not_replay_post(self):
        client=Mock();client.post_multipart.return_value=(302,'https://qoj.ac/contest/12/problem/100/statement/en','')
        with patch.dict(g,wait_for_new_sid=Mock(return_value=None)),contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit): self.send(client,['100'])
        self.assertEqual(client.post_multipart.call_count,1)

    def test_unknown_baseline_does_not_identify_old_records(self):
        self.assertFalse(ns['is_new_submission_id']('101',None))
        client=Mock()
        self.assertIsNone(ns['wait_for_new_sid'](client,contest(),'100',None))
        client.get.assert_not_called()

    def test_first_get_timeout_leaves_budget_for_retry(self):
        now=[0.0]; budgets=[];client=ns['Client']('https://qoj.ac')
        def request(url,data,headers,method,connect,read,**kwargs):
            budgets.append(read)
            if len(budgets)==1:
                now[0]+=read
                raise TimeoutError('request deadline')
            return 200,'',b'ok','utf-8'
        clock=SimpleNamespace(monotonic=lambda:now[0],sleep=lambda seconds:now.__setitem__(0,now[0]+seconds))
        with patch.dict(g,time=clock),patch.object(client,'_http_once',side_effect=request):
            self.assertEqual(client.get('/',timeout=15,retries=1,fatal=False)[2],'ok')
        self.assertEqual(len(budgets),2)
        self.assertAlmostEqual(budgets[0],7.3)
        self.assertAlmostEqual(budgets[1],7.3)

    def test_submit_page_deadline_tries_next_candidate(self):
        client=Mock();client.site='https://qoj.ac'
        client.get.side_effect=[TimeoutError('request deadline'),(200,'https://qoj.ac/contest/12/problem/100/submit',self.form())]
        result=ns['_fetch_problem_submit_page'](client,contest(),contest()['problems']['A'])
        self.assertTrue(result[0].endswith('/submit'))
        self.assertEqual(client.get.call_count,2)

    def test_delayed_submission_is_found(self):
        now=[0.0]
        clock=SimpleNamespace(monotonic=lambda:now[0],sleep=lambda seconds:now.__setitem__(0,now[0]+seconds))
        fetch=Mock(side_effect=[[{'id':'100'}],TimeoutError('slow'),[{'id':'101'}]])
        with patch.dict(g,time=clock,fetch_submission_rows=fetch),contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ns['wait_for_new_sid'](Mock(),contest(),'100',['100'],20),'101')
        self.assertEqual(fetch.call_count,3)

class SubmitCommandTests(unittest.TestCase):
    def test_history_deadline_does_not_abort_command(self):
        src=Mock();src.suffix='.cpp';src.name='a.cpp';src.read_text.return_value='int main(){}';src.stat.return_value.st_mtime=0
        c=contest();c['name']='Contest 12'
        client=Mock();client.site=c['site']
        form=SubmitReliabilityTests().form()
        context=(c['contest_url']+'/problem/100/submit',200,c['contest_url']+'/problem/100/submit',form,[],False,'',None)
        sender=Mock()
        args=SimpleNamespace(file='a.cpp',contest=None,debug=False,problem=None,lang=None,dry_run=False,yes=True)
        with patch.dict(g,load_global=lambda:{},require_contest=lambda:(Path('unused'),c),resolve_source=lambda name:src,letter_from_filename=lambda *args:'A',file_mtime=lambda path:'',make_client=Mock(return_value=client),_load_submit_context=Mock(return_value=context),fetch_submission_rows=Mock(side_effect=TimeoutError('request deadline')),_submit_qoj=sender,finish_submission=Mock()),contextlib.redirect_stdout(io.StringIO()):
            ns['cmd_submit'](args)
        sender.assert_called_once()
        self.assertIsNone(sender.call_args.args[10])

class StatusMarkupTests(unittest.TestCase):
    def parse(self, cell, headers=''):
        page='<table>'+headers+'<tr><td><a href="/submission/123">123</a></td><td><a href="/contest/12/problem/100">Pending Problem Title</a></td>'+cell+'</tr></table>'
        rows=ns['parse_submission_rows'](page)
        return ns['short_verdict'](rows[0]['status'])

    def test_plain_pending_and_accept(self):
        for raw,expected in [('pending','PENDING'),('accept','AC'),('Accepted','AC'),('Pending...','PENDING'),('Judging...','JUDGING'),('Waiting for judge','PENDING'),('等待评测','PENDING')]:
            with self.subTest(raw=raw): self.assertEqual(self.parse('<td>'+raw+'</td>'),expected)

    def test_nested_empty_icon_does_not_truncate_status(self):
        self.assertEqual(self.parse('<td class="uoj-status"><span class="icon"></span><span>Accepted</span></td>'),'AC')

    def test_status_from_tooltip(self):
        for attr in ['title','data-original-title','aria-label','data-verdict','data-status']:
            with self.subTest(attr=attr):
                self.assertEqual(self.parse('<td><span '+attr+'="Time Limit Exceeded on test 9"><i></i></span></td>'),'TLE')

    def test_semantic_class(self):
        self.assertEqual(self.parse('<td><span class="verdict-accepted"></span></td>'),'AC')

    def test_rejected_class_does_not_hide_specific_verdict(self):
        self.assertEqual(self.parse('<td class="verdict-rejected"><span>Memory Limit Exceeded</span></td>'),'MLE')

    def test_rejected_wrapper_does_not_hide_specific_verdict(self):
        self.assertEqual(self.parse('<td>Rejected <span>Runtime Error on test 2</span></td>'),'RE')

    def test_header_identifies_score_column_only(self):
        headers='<tr><th>ID</th><th>Problem</th><th>Score</th></tr>'
        self.assertEqual(self.parse('<td>100</td>',headers),'AC')
        self.assertEqual(self.parse('<td>100</td>'),'UNKNOWN')

    def test_does_not_parse_problem_title_as_pending(self):
        self.assertEqual(self.parse('<td>unrecognized</td>'),'UNKNOWN')

    def test_raw_waiting_for_test_keeps_status_only(self):
        self.assertEqual(self.parse('<td class="uoj-status"><b>Wrong Answer</b><small> on test 42</small></td>'),'WA')

class WatchFeatureTests(unittest.TestCase):
    def clock(self):
        now=[0.0]
        return SimpleNamespace(monotonic=lambda:now[0],sleep=lambda seconds:now.__setitem__(0,now[0]+seconds))

    def test_qoj_submission_time(self):
        page='<table>'+qrow(123).replace('</tr>','<td>2026-09-18 20:10:09</td><td>2026-09-18 20:11:12</td></tr>')+'</table>'
        self.assertEqual(ns['parse_submission_rows'](page)[0]['submit_time'],'2026-09-18 20:10:09')

    def test_cf_submission_time(self):
        page='<table>'+cfrow(123).replace('</tr>','<td><span>Sep/18/2026 20:10</span></td></tr>')+'</table>'
        self.assertEqual(ns['parse_cf_submission_rows'](page)[0]['submit_time'],'Sep/18/2026 20:10')

    def test_datetime_attribute(self):
        self.assertEqual(ns['submission_time_text']('<time datetime="2026-09-18T20:10:09+08:00">just now</time>'),'2026-09-18 20:10:09+08:00')

    def test_pending_then_accepted_prints_changes_only(self):
        fetch=Mock(side_effect=['PENDING','PENDING','JUDGING','AC'])
        with patch.dict(g,time=self.clock(),fetch_submission_verdict=fetch),contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertTrue(ns['watch_submission'](Mock(),contest(),'123',20))
        self.assertEqual(out.getvalue().count('#123  PENDING'),1)
        self.assertIn('#123  JUDGING',out.getvalue())
        self.assertIn('#123  AC',out.getvalue())

    def test_error_then_final_failure(self):
        fetch=Mock(side_effect=[TimeoutError('request deadline'),'JUDGING','TLE'])
        with patch.dict(g,time=self.clock(),fetch_submission_verdict=fetch),contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertTrue(ns['watch_submission'](Mock(),contest(),'123',20))
        self.assertIn('#123  TLE',out.getvalue())

    def test_unknown_is_not_final(self):
        with patch.dict(g,time=self.clock(),fetch_submission_verdict=Mock(return_value='UNKNOWN')),contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertFalse(ns['watch_submission'](Mock(),contest(),'123',3))
        self.assertIn('等待超时',out.getvalue())

    def test_only_exact_submission_is_watched(self):
        rows=[{'id':'124','status':'Accepted'},{'id':'123','status':'Pending'}]
        with patch.dict(g,fetch_submission_rows=Mock(return_value=rows)):
            self.assertEqual(ns['fetch_submission_verdict'](Mock(),contest(),'123'),'PENDING')

    def test_old_submission_uses_detail(self):
        client=Mock();client.get.return_value=(200,'https://qoj.ac/submission/123','<table>'+qrow(123,verdict='Accepted')+'</table>')
        with patch.dict(g,fetch_submission_rows=Mock(return_value=[])):
            self.assertEqual(ns['fetch_submission_verdict'](client,contest(),'123'),'AC')
        self.assertTrue(client.get.call_args.args[0].endswith('/submission/123'))

    def test_no_wait_skips_all_polling(self):
        watch,poll=Mock(),Mock()
        with patch.dict(g,watch_submission=watch,wait_for_new_sid=poll):
            ns['finish_submission'](Mock(),Path('unused'),contest(),{'id':'100'},None,['122'],SimpleNamespace(no_wait=True,wait=300))
        watch.assert_not_called();poll.assert_not_called()

    def test_default_wait_resolves_receipt_and_watches(self):
        watch=Mock();remember=Mock()
        with patch.dict(g,time=self.clock(),watch_submission=watch,wait_for_new_sid=Mock(return_value='123'),remember_submit=remember):
            ns['finish_submission'](Mock(),Path('unused'),contest(),{'id':'100'},None,['122'],SimpleNamespace(no_wait=False,wait=300))
        self.assertEqual(watch.call_args.args[2],'123')
        remember.assert_called_once()

    def test_cannot_guess_id_without_baseline(self):
        watch=Mock()
        with patch.dict(g,watch_submission=watch),contextlib.redirect_stdout(io.StringIO()) as out:
            ns['finish_submission'](Mock(),Path('unused'),contest(),{'id':'100'},None,None,SimpleNamespace(no_wait=False,wait=300))
        watch.assert_not_called()
        self.assertIn('未能确定',out.getvalue())

    def test_interrupt_stops_wait_not_submission(self):
        with patch.dict(g,watch_submission=Mock(side_effect=KeyboardInterrupt)),contextlib.redirect_stdout(io.StringIO()) as out:
            ns['finish_submission'](Mock(),Path('unused'),contest(),{'id':'100'},'123',['122'],SimpleNamespace(no_wait=False,wait=300))
        self.assertIn('仍在服务器评测',out.getvalue())

if __name__ == '__main__': unittest.main()
