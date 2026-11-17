"""Profile, prerequisite and privileged file lifecycle regression tests."""

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import demo
from gvdemo.options import prerequisites, static_elf
from gvdemo.runsc import Runsc, owned_mounts, time_to_ready
from gvdemo.session import ResultFile, cleanup_session, create_workdir, private_cache


class OptionsTest(unittest.TestCase):
    def test_quick_default(self):
        args = demo.parse_args([])
        self.assertEqual(args.scenes, ['intro', 'anywhere', 'startup', 'cr', 'summary'])
        self.assertEqual((args.runs, args.burst, args.cr_mb, args.clones, args.cpus, args.memory),
                         (10, 10, 16, 2, 2, '2G'))

    def test_full_and_explicit_overrides(self):
        args = demo.parse_args(['startup', '--profile', 'full', '--runs', '3'])
        self.assertEqual(args.scenes, ['startup'])
        self.assertEqual((args.runs, args.burst), (3, 200))
        self.assertEqual(demo.parse_args(['--profile', 'full']).scenes, list(demo.SCENE_NAMES))
        self.assertEqual(demo.parse_args(['all']).scenes, list(demo.SCENE_NAMES))

    def test_invalid_arguments(self):
        for argv in (['missing'], ['all', 'startup'], ['--runs', '0'], ['--cpus', '-1'],
                     ['--memory', 'NaN'], ['--memory', '0.1'], ['--auto', 'inf'],
                     ['--churn-levels', '1,0'], ['--density-mem', '100'], ['--burst', '-2']):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    demo.parse_args(argv)

    def test_check_creates_no_resources(self):
        with mock.patch.object(demo, 'prerequisites', return_value=[]), \
             mock.patch.object(demo, 'create_workdir') as create, \
             mock.patch.object(demo, 'ResultFile') as output, \
             contextlib.redirect_stdout(io.StringIO()):
            demo.main(['--check'])
        create.assert_not_called()
        output.assert_not_called()

    def test_systemd_requirements_only_when_selected(self):
        with mock.patch('gvdemo.options.shutil.which', return_value=None), \
             mock.patch('gvdemo.options.subprocess.run'), \
             mock.patch('gvdemo.options.os.geteuid', return_value=0):
            self.assertFalse(any('Docker' in e for e in prerequisites(demo.parse_args(['--no-limit']))))
            self.assertTrue(any('Docker' in e for e in prerequisites(demo.parse_args(['systemd', '--no-limit']))))

    def test_dynamic_executable_is_rejected(self):
        self.assertFalse(static_elf(sys.executable))


    def test_disabled_parent_controllers_are_reported(self):
        with mock.patch('gvdemo.options.shutil.which', return_value='/busybox'), \
             mock.patch('gvdemo.options.static_elf', return_value=True), \
             mock.patch('gvdemo.options.subprocess.run'), \
             mock.patch('gvdemo.options.os.geteuid', return_value=0), \
             mock.patch('gvdemo.options.Path.exists', return_value=True), \
             mock.patch('gvdemo.options.Path.read_text', side_effect=[
                 'cpu cpuset memory pids', 'cpu memory pids']):
            errors = prerequisites(demo.parse_args([]))
        self.assertTrue(any('cgroup.subtree_control' in error for error in errors))


class ChurnTest(unittest.TestCase):
    def test_failed_readiness_is_an_error(self):
        from gvdemo.scenes import churn
        args = demo.parse_args(['churn', '--churn-levels', '1', '--churn-seconds', '0.1'])
        results = {}
        with mock.patch.object(churn, 'console'), \
             mock.patch.object(churn, 'title'), \
             mock.patch.object(churn, 'out'), \
             mock.patch.object(churn, 'make_bundle'), \
             mock.patch.object(churn, 'wait_to_start'), \
             mock.patch.object(churn, 'scene_live'), \
             mock.patch.object(churn, 'cpu_busy_s', return_value=0), \
             mock.patch.object(churn, 'time_to_ready', return_value=None):
            with self.assertRaisesRegex(RuntimeError, 'failed to become ready'):
                churn.run(mock.Mock(), {}, args, results)
        self.assertNotIn('churn', results)


class SessionTest(unittest.TestCase):
    def setUp(self):
        # Ordinary file lifecycle tests run as the current test process.
        environment = {k: v for k, v in os.environ.items() if k not in ('SUDO_UID', 'SUDO_GID')}
        patch = mock.patch.dict(os.environ, environment, clear=True)
        patch.start()
        self.addCleanup(patch.stop)

    def test_private_default_and_existing_path(self):
        path = create_workdir()
        try:
            self.assertEqual(path.stat().st_mode & 0o777, 0o700)
            with self.assertRaises(FileExistsError):
                create_workdir(path)
        finally:
            path.rmdir()

    def test_symlink_ancestor_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'link').symlink_to(root, target_is_directory=True)
            with self.assertRaises(OSError):
                create_workdir(root / 'link' / 'work')
            self.assertFalse((root / 'work').exists())

    def test_untrusted_cache_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'cache'
            path.mkdir(mode=0o777)
            path.chmod(0o777)
            with self.assertRaises(ValueError):
                private_cache(path)

    def test_output_is_exclusive_and_saves_partial_results(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'results.json'
            output = ResultFile(path)
            try:
                output.save({'execution': {'status': 'running'}})
                output.save({'execution': {'status': 'failed'}, 'startup': [1]})
                self.assertEqual(json.loads(path.read_text())['startup'], [1])
                with self.assertRaises(FileExistsError):
                    ResultFile(path)
            finally:
                output.close()
            link = Path(directory) / 'link'
            link.symlink_to(path)
            with self.assertRaises(FileExistsError):
                ResultFile(link)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_mounts_match_components_and_decode_escapes(self):
        with tempfile.NamedTemporaryFile(mode='w') as mounts:
            mounts.write('1 2 0:1 / /tmp/demo-2/net rw - nsfs nsfs rw\n'
                         '1 2 0:1 / /tmp/demo/net rw - nsfs nsfs rw\n'
                         '1 2 0:1 / /tmp/demo/a\\040b/net rw - nsfs nsfs rw\n')
            mounts.flush()
            self.assertEqual(owned_mounts('/tmp/demo', mounts.name),
                             ['/tmp/demo/a b/net', '/tmp/demo/net'])


class CleanupTest(unittest.TestCase):
    def test_cgroup_failure_does_not_skip_runtime_cleanup(self):
        runtime = mock.Mock()
        remove = mock.Mock(side_effect=[RuntimeError('busy'), None])
        directory = mock.Mock()
        cleanup_session(runtime, 'demo', directory, remove)
        self.assertEqual(remove.call_count, 2)
        self.assertEqual(runtime.cleanup.call_count, 2)
        directory.rmdir.assert_called_once()

    def test_persistent_cleanup_failure_preserves_directory(self):
        runtime = mock.Mock()
        remove = mock.Mock(side_effect=RuntimeError('busy'))
        directory = mock.Mock()
        with self.assertRaisesRegex(RuntimeError, 'cgroup cleanup'):
            cleanup_session(runtime, 'demo', directory, remove)
        runtime.cleanup.assert_called_once_with(preserve=True)
        directory.rmdir.assert_not_called()

    def test_failed_listing_preserves_state(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Runsc(sys.executable, directory, [])
            sentinel = runtime.root / 'sentinel'
            sentinel.write_text('preserve')
            failed = subprocess.CompletedProcess([], 1, '', 'list failed')
            with mock.patch.object(runtime, '_run', return_value=failed):
                with self.assertRaisesRegex(RuntimeError, 'cannot list'):
                    runtime.cleanup()
            self.assertTrue(sentinel.exists())

    def test_failed_delete_preserves_state(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Runsc(sys.executable, directory, [])
            sentinel = runtime.root / 'sentinel'
            sentinel.write_text('preserve')
            with mock.patch.object(runtime, 'ids', return_value=['sandbox']), \
                 mock.patch.object(runtime, '_run', return_value=subprocess.CompletedProcess([], 1)):
                with self.assertRaisesRegex(RuntimeError, 'delete'):
                    runtime.cleanup()
            self.assertTrue(sentinel.exists())

    def test_cleanup_failure_is_saved_in_results(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'result.json'
            environment = {k: v for k, v in os.environ.items() if k not in ('SUDO_UID', 'SUDO_GID')}
            with mock.patch.dict(os.environ, environment, clear=True), \
                 mock.patch.object(demo, 'prerequisites', return_value=[]), \
                 mock.patch.object(demo, 'cleanup_session', side_effect=RuntimeError('cleanup failed')), \
                 mock.patch.object(demo, 'create_workdir', return_value=Path(directory)), \
                 mock.patch('gvdemo.runsc.Runsc') as runtime, \
                 mock.patch('gvdemo.ui.ask_next', return_value='quit'), \
                 mock.patch.dict('gvdemo.scenes.SCENES', {'summary': mock.Mock()}), \
                 contextlib.redirect_stdout(io.StringIO()):
                runtime.return_value.version.return_value = 'fake'
                with self.assertRaisesRegex(RuntimeError, 'cleanup failed'):
                    demo.main(['summary', '--no-limit', '--auto', '0.1', '--output', str(path)])
            metadata = json.loads(path.read_text())['execution']
            self.assertEqual(metadata['status'], 'failed')
            self.assertIn('cleanup failed', metadata['cleanup_error'])

    def test_kiosk_failure_is_saved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'result.json'
            environment = {k: v for k, v in os.environ.items() if k not in ('SUDO_UID', 'SUDO_GID')}
            with mock.patch.dict(os.environ, environment, clear=True), \
                 mock.patch.object(demo, 'prerequisites', return_value=[]), \
                 mock.patch.object(demo, 'cleanup_session'), \
                 mock.patch.object(demo, 'create_workdir', return_value=Path(directory)), \
                 mock.patch('gvdemo.runsc.Runsc') as runtime, \
                 mock.patch('gvdemo.ui.ask_next', return_value='quit'), \
                 mock.patch.dict('gvdemo.scenes.SCENES', {'summary': mock.Mock(side_effect=RuntimeError('scene failed'))}), \
                 contextlib.redirect_stdout(io.StringIO()):
                runtime.return_value.version.return_value = 'fake'
                demo.main(['summary', '--no-limit', '--kiosk', '--output', str(path)])
            metadata = json.loads(path.read_text())['execution']
            self.assertEqual(metadata['status'], 'failed')
            self.assertEqual(metadata['scene_errors'], [{'scene': 'summary', 'error': 'scene failed'}])


class ProcessTest(unittest.TestCase):
    def test_timeout_kills_and_reaps_command(self):
        with tempfile.TemporaryDirectory() as directory:
            rs = Runsc(sys.executable, directory, [])
            process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                                       start_new_session=True)
            with mock.patch.object(rs, 'start', return_value=process), \
                 mock.patch.object(process, 'communicate', side_effect=subprocess.TimeoutExpired('fake', 60)):
                with self.assertRaises(subprocess.TimeoutExpired):
                    rs.call('run')
            self.assertIsNotNone(process.poll())

    def test_cancellation_blocks_new_sandboxes(self):
        with tempfile.TemporaryDirectory() as directory:
            rs = Runsc(sys.executable, directory, [])
            rs.cancel_pending()
            with self.assertRaisesRegex(RuntimeError, 'cancelled'):
                rs.start(['run'])

    def test_ready_requires_successful_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            rs = Runsc(sys.executable, directory, [])
            process = subprocess.Popen([sys.executable, '-c', 'print("ready"); exit(1)'],
                                       stdout=subprocess.PIPE, start_new_session=True)
            with mock.patch.object(rs, 'start', return_value=process):
                self.assertIsNone(time_to_ready(rs, directory, 'failed'))


if __name__ == '__main__':
    unittest.main()
