"""Privileged integration tests. Run only on a disposable Linux host.

GVDEMO_INTEGRATION=1 GVDEMO_RUNSC=/path/to/runsc python3 -m unittest discover -s tests
"""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.environ.get('GVDEMO_INTEGRATION') == '1', 'requires disposable root environment')
class IntegrationTest(unittest.TestCase):
    def setUp(self):
        self.child_env = {k: v for k, v in os.environ.items() if k not in ('SUDO_UID', 'SUDO_GID')}
        self.directory = tempfile.TemporaryDirectory(prefix='gvisor-demo-integration-')
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name)
        self.runsc = os.environ.get('GVDEMO_RUNSC', 'runsc')

    def command(self, name, *options):
        return [sys.executable, str(ROOT / 'demo.py'), '--runsc', self.runsc,
                '--workdir', str(self.base / name), '--output', str(self.base / f'{name}.json'),
                '--auto', '0.01', '--title-size', 'normal', '--activity', 'off', *options]

    def verify_clean(self, name):
        self.assertFalse((self.base / name).exists())
        data = json.loads((self.base / f'{name}.json').read_text())
        cgroup = data['execution'].get('cgroup')
        if cgroup:
            self.assertFalse((Path('/sys/fs/cgroup') / cgroup).exists())
        return data

    def test_quick_profile(self):
        subprocess.run(self.command('quick'), check=True, stdout=subprocess.DEVNULL, env=self.child_env, timeout=180)
        data = self.verify_clean('quick')
        self.assertEqual(data['execution']['status'], 'completed')
        self.assertEqual(len(data['startup']['seq_ms']), 10)
        self.assertEqual(len(data['startup']['burst_ms']), 10)
        self.assertEqual(len(data['cr']['clone_ms']), 2)

    def test_signals_save_results_and_cleanup(self):
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            name = f'signal-{sig}'
            with self.subTest(signal=sig):
                process = subprocess.Popen(self.command(name, 'startup', '--runs', '200'),
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=self.child_env)
                try:
                    deadline = time.monotonic() + 20
                    while not (self.base / name / 'state').exists():
                        if process.poll() is not None or time.monotonic() > deadline:
                            self.fail('demo failed to start')
                        time.sleep(0.02)
                    time.sleep(0.3)
                    process.send_signal(sig)
                    process.wait(timeout=70)
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait()
                self.assertEqual(self.verify_clean(name)['execution']['status'], 'interrupted')

    def test_independent_runs(self):
        commands = [self.command(name, 'startup', '--runs', '2', '--burst', '2')
                    for name in ('first', 'second')]
        processes = [subprocess.Popen(command, stdout=subprocess.DEVNULL, env=self.child_env) for command in commands]
        try:
            for process in processes:
                self.assertEqual(process.wait(timeout=90), 0)
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.wait()
        for name in ('first', 'second'):
            self.assertEqual(self.verify_clean(name)['execution']['status'], 'completed')

    def test_sudo_result_ownership_and_groups(self):
        import pwd
        caller = pwd.getpwnam('nobody')
        self.base.chmod(0o755)
        writable = self.base / 'caller-output'
        writable.mkdir()
        writable.chmod(0o777)
        output = writable / 'owned.json'
        protected = self.base / 'group-root'
        protected.mkdir()
        protected.chmod(0o770)
        script = (
            'import os, sys; sys.path.insert(0, ' + repr(str(ROOT)) + '); '
            'from gvdemo.session import ResultFile; '
            'groups=os.getgroups(); out=ResultFile(sys.argv[1]); '
            'out.save({"ok":True}); out.close(); '
            'assert os.geteuid()==0 and os.getgroups()==groups'
        )
        environment = dict(self.child_env, SUDO_UID=str(caller.pw_uid), SUDO_GID=str(caller.pw_gid))
        subprocess.run([sys.executable, '-c', script, str(output)], env=environment, check=True)
        self.assertEqual(output.stat().st_uid, caller.pw_uid)
        failed = subprocess.run([sys.executable, '-c', script, str(protected / 'denied.json')],
                                env=environment, stderr=subprocess.DEVNULL)
        self.assertNotEqual(failed.returncode, 0)
        self.assertFalse((protected / 'denied.json').exists())

    def test_runtime_failure_and_existing_directory(self):
        fake = self.base / 'broken-runsc'
        fake.write_text('#!/bin/sh\ncase "$*" in *--version*) echo fake;; *list*) exit 0;; *) exit 1;; esac\n')
        fake.chmod(0o700)
        command = self.command('failure', 'anywhere')
        command[command.index('--runsc') + 1] = str(fake)
        completed = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=self.child_env, timeout=90)
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(self.verify_clean('failure')['execution']['status'], 'failed')
        path = self.base / 'existing'
        path.mkdir()
        sentinel = path / 'sentinel'
        sentinel.write_text('preserve')
        completed = subprocess.run(self.command('existing', 'startup'), stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, env=self.child_env, timeout=30)
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(sentinel.read_text(), 'preserve')


if __name__ == '__main__':
    unittest.main()
