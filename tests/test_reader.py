import argparse
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

    def _add_image_session(self, blocks, sid='image1234-session'):
        line = json.dumps({'type': 'message', 'role': 'user', 'timestamp': 1,
                           'content': blocks}, ensure_ascii=False)
        self.db = sqlite3.connect(self.home / 'workbuddy.db')
        self._add(sid, '图片测试', [line])
        self.db.commit()
        self.db.close()
        self.jsonls.append(self.projects / f'{sid}.jsonl')
        self.before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.jsonls}
        self.dbhash = hashlib.sha256((self.home / 'workbuddy.db').read_bytes()).hexdigest()
        return self.projects / f'{sid}.jsonl'

    def _image_block(self, path, mime='image/png'):
        return {'type': 'image_blob_ref', 'blob_id': 'synthetic', 'blob_path': str(path),
                'mime': mime, 'original_filename': pathlib.Path(path).name, 'size': 4}

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

    def test_images_export_multiple_same_name_images_and_link_encoding(self):
        blobs = self.home / 'blobs'
        blobs.mkdir()
        first = blobs / 'same.png'
        second = blobs / 'other.png'
        first.write_bytes(b'png-one')
        second.write_bytes(b'png-two')
        self._add_image_session([{'type': 'input_text', 'text': '图片'},
                                 self._image_block(first), self._image_block(second)])
        out = pathlib.Path(self.tmp.name) / '导出 # 一.md'
        p = self.run_cli('show', 'image1234', '--images', '--out', str(out), home=self.home)
        self.assertEqual(p.returncode, 0, p.stderr)
        attachments = out.with_name(out.stem + '-attachments')
        self.assertEqual(len(list(attachments.iterdir())), 2)
        body = out.read_text(encoding='utf-8')
        self.assertEqual(body.count('![图片]('), 2)
        self.assertIn('%23', body)
        self.assertNotIn(str(first), body)
        for target in attachments.iterdir():
            self.assertIn(target.read_bytes(), (b'png-one', b'png-two'))
        self.assertEqual(first.read_bytes(), b'png-one')
        self.assertEqual(second.read_bytes(), b'png-two')

    def test_images_only_messages_deduplicate_and_repeat_export(self):
        blobs = self.home / 'blobs'
        blobs.mkdir()
        image = blobs / 'same.png'
        image.write_bytes(b'png-data')
        self._add_image_session([self._image_block(image), self._image_block(image)])
        out = pathlib.Path(self.tmp.name) / 'only.md'
        for _ in range(2):
            p = self.run_cli('show', 'image1234', '--images', '--out', str(out), home=self.home)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertEqual(len(list(out.with_name('only-attachments').iterdir())), 1)
            self.assertEqual(out.read_text(encoding='utf-8').count('![图片]('), 2)
        self.assertIn('消息数: 1', out.read_text(encoding='utf-8'))

    def test_image_reference_errors_are_visible_without_source_path(self):
        blobs = self.home / 'blobs'
        blobs.mkdir()
        missing = blobs / 'missing.png'
        self._add_image_session([self._image_block(missing),
                                 {'type': 'image_blob_ref', 'blob_path': {'bad': 'value'}},
                                 {'type': 'input_image', 'original_filename': 'private.png'}])
        out = pathlib.Path(self.tmp.name) / 'errors.md'
        p = self.run_cli('show', 'image1234', '--images', '--out', str(out), home=self.home)
        self.assertEqual(p.returncode, 0, p.stderr)
        body = out.read_text(encoding='utf-8')
        self.assertEqual(body.count('图片未导出'), 3)
        self.assertNotIn(str(missing), body)
        self.assertNotIn('private.png', body)
        self.assertIn('未导出不支持的图片结构', p.stderr)

    def test_images_invalid_mime_falls_back_to_safe_source_suffix(self):
        blobs = self.home / 'blobs'
        blobs.mkdir()
        image = blobs / 'mime.png'
        image.write_bytes(b'png-data')
        dict_mime = self._image_block(image)
        dict_mime['mime'] = {'unexpected': 'object'}
        list_mime = self._image_block(image)
        list_mime['mime'] = ['unexpected', 'list']
        self._add_image_session([dict_mime, list_mime])
        out = pathlib.Path(self.tmp.name) / 'mime.md'
        p = self.run_cli('show', 'image1234', '--images', '--out', str(out), home=self.home)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(out.read_text(encoding='utf-8').count('![图片]('), 2)
        attachments = list(out.with_name('mime-attachments').iterdir())
        self.assertEqual(len(attachments), 1)
        self.assertEqual(attachments[0].suffix, '.png')
        self.assertEqual(attachments[0].read_bytes(), b'png-data')

    def test_out_directory_is_rejected_before_attachment_write(self):
        blobs = self.home / 'blobs'
        blobs.mkdir()
        image = blobs / 'same.png'
        image.write_bytes(b'png-data')
        self._add_image_session([self._image_block(image)])
        out_dir = pathlib.Path(self.tmp.name) / 'already-a-directory'
        out_dir.mkdir()
        p = self.run_cli('show', 'image1234', '--images', '--out', str(out_dir), home=self.home)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('--out 必须指向普通文件', p.stderr)
        self.assertFalse(out_dir.with_name(out_dir.name + '-attachments').exists())
        self.assertEqual(image.read_bytes(), b'png-data')

    def test_partial_image_temp_is_not_published_and_retry_succeeds(self):
        blobs = self.home / 'blobs'
        blobs.mkdir()
        image = blobs / 'same.png'
        image.write_bytes(b'png-complete')
        spec = importlib.util.spec_from_file_location('wb_conv_partial_test_module', CLI)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.set_home(str(self.home))
        destination = pathlib.Path(self.tmp.name) / 'atomic-attachments'
        destination.mkdir()
        real_fdopen = os.fdopen

        class PartialWriter:
            def __init__(self, stream):
                self.stream = stream

            def __enter__(self):
                return self

            def __exit__(self, *_):
                self.stream.close()

            def write(self, data):
                self.stream.write(data[:4])
                self.stream.flush()
                raise OSError('synthetic interrupted write')

            def flush(self):
                self.stream.flush()

        with mock.patch.object(module.os, 'fdopen', side_effect=lambda fd, mode: PartialWriter(real_fdopen(fd, mode))):
            self.assertIsNone(module._export_image(str(image), 'image/png', destination, {}))
        partials = list(destination.glob('.wb-image-*.tmp'))
        self.assertEqual(len(partials), 1)
        self.assertEqual(partials[0].read_bytes(), b'png-')
        target_name = hashlib.sha256(image.read_bytes()).hexdigest() + '.png'
        self.assertFalse((destination / target_name).exists())
        name = module._export_image(str(image), 'image/png', destination, {})
        self.assertEqual(name, target_name)
        self.assertEqual((destination / name).read_bytes(), image.read_bytes())

    def test_concurrent_different_target_is_never_overwritten(self):
        blobs = self.home / 'blobs'
        blobs.mkdir()
        image = blobs / 'same.png'
        image.write_bytes(b'png-source')
        spec = importlib.util.spec_from_file_location('wb_conv_race_test_module', CLI)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.set_home(str(self.home))
        destination = pathlib.Path(self.tmp.name) / 'race-attachments'
        destination.mkdir()
        target = destination / (hashlib.sha256(image.read_bytes()).hexdigest() + '.png')

        def concurrent_publish(_temporary, target_path):
            pathlib.Path(target_path).write_bytes(b'user-existing-data')
            raise FileExistsError('synthetic concurrent publication')

        with mock.patch.object(module.os, 'link', side_effect=concurrent_publish):
            self.assertIsNone(module._export_image(str(image), 'image/png', destination, {}))
        self.assertEqual(target.read_bytes(), b'user-existing-data')

    def test_markdown_replace_failure_preserves_existing_output(self):
        self._add_image_session([{'type': 'input_text', 'text': 'atomic markdown'}])
        spec = importlib.util.spec_from_file_location('wb_conv_markdown_test_module', CLI)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.set_home(str(self.home))
        out = pathlib.Path(self.tmp.name) / 'keep.md'
        out.write_text('previous complete export', encoding='utf-8')
        args = argparse.Namespace(id='image1234', images=False, out=str(out), raw=False,
                                  full=False, maxlen=1500, trace=False)
        with mock.patch.object(module.os, 'replace', side_effect=OSError('synthetic publish failure')):
            with self.assertRaises(SystemExit):
                module.cmd_show(args)
        self.assertEqual(out.read_text(encoding='utf-8'), 'previous complete export')
        self.assertTrue(list(pathlib.Path(self.tmp.name).glob('.wb-markdown-*.tmp')))

    def test_images_requires_out_before_any_write(self):
        self._add_image_session([{'type': 'input_text', 'text': 'x'}])
        p = self.run_cli('show', 'image1234', '--images', home=self.home)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('--images 必须与 --out', p.stderr)
        self.assertFalse(list(pathlib.Path(self.tmp.name).glob('*.md')))

    def test_images_rejects_attachment_directory_symlink_into_workbuddy(self):
        blobs = self.home / 'blobs'
        blobs.mkdir()
        image = blobs / 'same.png'
        image.write_bytes(b'png-data')
        self._add_image_session([self._image_block(image)])
        out = pathlib.Path(self.tmp.name) / 'link.md'
        attachment = out.with_name('link-attachments')
        try:
            attachment.symlink_to(blobs, target_is_directory=True)
        except (OSError, NotImplementedError):
            if os.name != 'nt':
                self.skipTest('directory symlinks unavailable')
            junction = subprocess.run(['cmd', '/c', 'mklink', '/J', str(attachment), str(blobs)],
                                      text=True, capture_output=True)
            if junction.returncode != 0:
                self.skipTest('directory symlink and junction creation unavailable')
        p = self.run_cli('show', 'image1234', '--images', '--out', str(out), home=self.home)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('拒绝将图片附件写入 WorkBuddy 数据目录', p.stderr)
        self.assertEqual(image.read_bytes(), b'png-data')
        self.assertFalse(out.exists())

    def test_images_rejects_hardlink_out_to_source_session(self):
        blobs = self.home / 'blobs'
        blobs.mkdir()
        image = blobs / 'same.png'
        image.write_bytes(b'png-data')
        source = self._add_image_session([self._image_block(image)])
        alias = pathlib.Path(self.tmp.name) / 'source-alias.md'
        try:
            os.link(source, alias)
        except (OSError, NotImplementedError) as error:
            self.skipTest(f'hard links unavailable: {error}')
        p = self.run_cli('show', 'image1234', '--images', '--out', str(alias), home=self.home)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn('拒绝通过硬链接覆盖源会话 JSONL', p.stderr)

    def test_images_default_export_does_not_create_attachments(self):
        blobs = self.home / 'blobs'
        blobs.mkdir()
        image = blobs / 'same.png'
        image.write_bytes(b'png-data')
        self._add_image_session([self._image_block(image)])
        out = pathlib.Path(self.tmp.name) / 'plain.md'
        p = self.run_cli('show', 'image1234', '--out', str(out), home=self.home)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertFalse(out.with_name('plain-attachments').exists())
        self.assertNotIn('![图片]', out.read_text(encoding='utf-8'))

    def test_images_preserve_raw_full_text_mode(self):
        blobs = self.home / 'blobs'
        blobs.mkdir()
        image = blobs / 'same.png'
        image.write_bytes(b'png-data')
        wrapped = '<system-reminder>保留包装</system-reminder><user_query>原始正文</user_query>'
        self._add_image_session([{'type': 'input_text', 'text': wrapped}, self._image_block(image)])
        out = pathlib.Path(self.tmp.name) / 'raw.md'
        p = self.run_cli('show', 'image1234', '--raw', '--full', '--images', '--out', str(out), home=self.home)
        self.assertEqual(p.returncode, 0, p.stderr)
        body = out.read_text(encoding='utf-8')
        self.assertIn(wrapped, body)
        self.assertIn('![图片](', body)


if __name__ == '__main__':
    unittest.main(verbosity=2)
