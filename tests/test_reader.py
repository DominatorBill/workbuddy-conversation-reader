import hashlib
import importlib.util
import json
import os
import pathlib
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
CLI = ROOT / 'scripts' / 'wb-conv.py'


def event(role, text, ts=1, ensure_ascii=False):
    return json.dumps({'type': 'message', 'role': role, 'timestamp': ts,
                       'content': [{'type': 'input_text' if role == 'user' else 'output_text', 'text': text}]}, ensure_ascii=ensure_ascii)


class ReaderRegression(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = pathlib.Path(self.tmp.name) / 'wb'
        self.projects = self.home / 'projects' / 'sample-project'
        self.projects.mkdir(parents=True)
        self.db = sqlite3.connect(self.home / 'workbuddy.db')
        self.db.execute('''create table sessions (
            id text primary key, cwd text, title text, custom_title text, status text,
            updated_at integer, last_activity_at integer, mode text, model text,
            is_background_automation integer, deleted_at integer, created_at integer,
            permission_mode text)''')
        self._add('abcd1234-full', '普通标题', [event('user', '普通问题'), event('assistant', '答复')])
        self.automation_body = '. You MUST follow these steps in order\n1. 包装指令一\n2. 包装指令二\n1. 实际任务紧邻包装\n2. 正文编号不能丢失\n' + ('长正文内容' * 700)
        long_task = '<system-reminder>私人包装</system-reminder><user_query>' + self.automation_body + '</user_query>'
        self._add('efgh5678-auto', '定时标题', [event('user', long_task)])
        self._add('zzzz9999-body', '其他标题', [event('user', '正文唯一搜索词以及中文内容', ensure_ascii=True)])
        self._add('yyyy8888-body', '第二正文标题', [event('user', '正文唯一搜索词第二命中')])
        self._add('tttt7777-collision-title', 'collision 标题命中', [event('user', 'title only')])
        self._add('uuuu6666-collision-body', '无关标题', [event('user', 'collision 正文命中')])
        self.db.commit()
        self.db.close()
        self.jsonls = list(self.projects.glob('*.jsonl'))
        self.before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.jsonls}
        self.dbhash = hashlib.sha256((self.home / 'workbuddy.db').read_bytes()).hexdigest()

    def tearDown(self):
        self.assertEqual(self.before, {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.jsonls})
        self.assertEqual(self.dbhash, hashlib.sha256((self.home / 'workbuddy.db').read_bytes()).hexdigest())
        self.tmp.cleanup()

    def _add(self, sid, title, lines):
        self.db.execute('insert into sessions values (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                        (sid, r'C:\demo', title, None, 'done', 100, 100, 'chat', 'test', 0, None, 1, ''))
        (self.projects / f'{sid}.jsonl').write_text('\n'.join(lines) + '\n', encoding='utf-8')

    def run_cli(self, *args, env=None, home=None):
        e = os.environ.copy()
        e.pop('WORKBUDDY_DIR', None)
        if env:
            e.update(env)
        cmd = [sys.executable, str(CLI)]
        if home is not None:
            cmd += ['--home', str(home)]
        return subprocess.run([*cmd, *args],
                              text=True, capture_output=True, env=e)

    def test_long_automation_full_and_numbered_body_preserved(self):
        p = self.run_cli('show', 'efgh5678', '--full', home=self.home)
        self.assertEqual(p.returncode, 0, p.stderr)
        expected = '[定时任务自动 prompt，内容完整保留，可能包含执行包装]\n\n' + self.automation_body
        self.assertTrue(p.stdout.rstrip('\r\n').endswith(expected), 'cleaned automation body changed or was truncated')

    def test_raw_keeps_wrappers_and_full_content(self):
        p = self.run_cli('show', 'efgh5678', '--raw', home=self.home)
        self.assertIn('<system-reminder>私人包装</system-reminder>', p.stdout)
        self.assertIn('You MUST follow these steps', p.stdout)
        self.assertIn('长正文内容' * 700, p.stdout)

    def test_unicode_escape_search(self):
        p = self.run_cli('search', '中文内容', '--json', home=self.home)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('zzzz9999-body', p.stdout)

    def test_prefix_ambiguity_and_complete_id(self):
        p = self.run_cli('title', 'abcd', home=self.home)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('abcd1234-full', p.stdout)
        self.db = sqlite3.connect(self.home / 'workbuddy.db')
        self.db.execute('insert into sessions values (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                        ('abcd1234-other', r'C:\demo', '副本标题', None, 'done', 100, 100, 'chat', 'test', 0, None, 1, ''))
        self.db.commit(); self.db.close()
        (self.projects / 'abcd1234-other.jsonl').write_text(event('user', '另一个会话') + '\n', encoding='utf-8')
        self.dbhash = hashlib.sha256((self.home / 'workbuddy.db').read_bytes()).hexdigest()
        p = self.run_cli('title', 'abcd', home=self.home)
        self.assertEqual(p.returncode, 2)
        p = self.run_cli('title', 'abcd1234-full', home=self.home)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('abcd1234-full', p.stdout)

    def test_duplicate_session_copy_is_ambiguous(self):
        duplicate_dir = self.home / 'projects' / 'other-project'
        duplicate_dir.mkdir()
        (duplicate_dir / 'abcd1234-full.jsonl').write_text(event('user', '副本') + '\n', encoding='utf-8')
        p = self.run_cli('show', 'abcd1234-full', home=self.home)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('多个项目副本', p.stderr)

    def test_find_deep_json_and_limit(self):
        p = self.run_cli('find', '正文唯一搜索词', '--deep', '--json', '-n', '1', home=self.home)
        self.assertEqual(p.returncode, 0, p.stderr)
        data = json.loads(p.stdout)
        self.assertEqual(len(data), 1)
        self.assertIn(data[0]['id'], {'zzzz9999-body', 'yyyy8888-body'})
        p = self.run_cli('find', '正文唯一搜索词', '--deep', '--json', '-n', '10', home=self.home)
        results = json.loads(p.stdout)
        self.assertEqual(len(results), 2)
        self.assertEqual({x['id'] for x in results}, {'zzzz9999-body', 'yyyy8888-body'})

    def test_find_deep_only_runs_after_title_miss(self):
        p = self.run_cli('find', 'collision', '--deep', '--json', home=self.home)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual([x['id'] for x in json.loads(p.stdout)], ['tttt7777-collision-title'])

    def test_title_search(self):
        p = self.run_cli('find', '定时标题', '--json', home=self.home)
        self.assertIn('efgh5678-auto', p.stdout)

    def test_readonly_out_refuses_source_overwrite(self):
        source = self.projects / 'abcd1234-full.jsonl'
        p = self.run_cli('show', 'abcd1234-full', '--out', str(source), home=self.home)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('拒绝将导出文件写入 WorkBuddy 数据目录', p.stderr)

    def test_readonly_out_refuses_case_changed_source_path(self):
        if os.name != 'nt':
            self.skipTest('case-insensitive path behavior is Windows-specific')
        source = self.projects / 'abcd1234-full.jsonl'
        case_variant = str(source).swapcase()
        p = self.run_cli('show', 'abcd1234-full', '--out', case_variant, home=self.home)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('拒绝将导出文件写入 WorkBuddy 数据目录', p.stderr)

    def test_readonly_out_refuses_hardlinks_to_jsonl_and_database(self):
        protected_files = [self.projects / 'abcd1234-full.jsonl', self.home / 'workbuddy.db']
        for index, protected in enumerate(protected_files):
            alias = pathlib.Path(self.tmp.name) / f'protected-hardlink-{index}.bin'
            try:
                os.link(protected, alias)
            except (OSError, NotImplementedError) as error:
                self.skipTest(f'hard links unavailable: {error}')
            p = self.run_cli('show', 'abcd1234-full', '--out', str(alias), home=self.home)
            self.assertNotEqual(p.returncode, 0)
            self.assertIn('拒绝通过硬链接覆盖', p.stderr)

    def test_cli_home_overrides_environment(self):
        p = subprocess.run([sys.executable, str(CLI), '--home', str(self.home), 'find', '普通标题', '--json'],
                           text=True, capture_output=True, env={**os.environ, 'WORKBUDDY_DIR': 'Z:/missing'})
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('abcd1234-full', p.stdout)

    def test_environment_home(self):
        p = self.run_cli('find', '普通标题', '--json', env={'WORKBUDDY_DIR': str(self.home)})
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('abcd1234-full', p.stdout)

    def test_default_home(self):
        profile = pathlib.Path(self.tmp.name) / 'isolated-profile'
        default_home = profile / '.workbuddy'
        profile.mkdir()
        shutil.copytree(self.home, default_home)
        env = os.environ.copy()
        env.pop('WORKBUDDY_DIR', None)
        env['USERPROFILE'] = str(profile)
        env['HOME'] = str(profile)
        p = subprocess.run([sys.executable, str(CLI), 'find', '普通标题', '--json'],
                           text=True, capture_output=True, env=env)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('abcd1234-full', p.stdout)

    def test_open_db_uri_escapes_reserved_path_chars(self):
        spec = importlib.util.spec_from_file_location('wb_conv_test_module', CLI)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.set_home(str(pathlib.Path(self.tmp.name) / 'wb #? reserved'))
        with mock.patch.object(module.os.path, 'exists', return_value=True):
            with mock.patch.object(module.sqlite3, 'connect') as connect:
                module.open_db()
        uri = connect.call_args.args[0]
        self.assertIn('%23', uri)
        self.assertIn('%3F', uri)
        self.assertTrue(uri.endswith('?mode=ro'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
