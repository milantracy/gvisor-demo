"""Profiles and prerequisite checks that do not create sandbox resources."""

import math
import os
from pathlib import Path
import platform
import shutil
import struct
import subprocess
import sys

QUICK_SCENES = ['intro', 'anywhere', 'startup', 'cr', 'summary']
PROFILES = {
    'quick': dict(runs=10, burst=10, cr_mb=16, clones=2, cpus=2, memory='2G'),
    'full': dict(runs=200, burst=200, cr_mb=128, clones=10, cpus=16, memory='32G'),
}


def parse_size(text):
    units = {'K': 2**10, 'M': 2**20, 'G': 2**30, 'T': 2**40}
    value = str(text).strip().upper()
    for suffix in ('IB', 'B'):
        if value.endswith(suffix):
            value = value[:-len(suffix)]
            break
    number = float(value[:-1]) * units[value[-1]] if value[-1:] in units else float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError('memory must be positive and finite')
    if int(number) < 1:
        raise ValueError("memory must be at least one byte")
    return int(number)


def configure(parser, args, scene_names):
    for name, value in PROFILES[args.profile].items():
        if getattr(args, name) is None:
            setattr(args, name, value)
    if args.scenes and any(s not in [*scene_names, 'all'] for s in args.scenes):
        parser.error('unknown scene; choose from ' + ', '.join(scene_names))
    if 'all' in args.scenes and len(args.scenes) != 1:
        parser.error('all cannot be combined with individual scenes')
    if not args.scenes:
        args.scenes = list(scene_names) if args.profile == 'full' else list(QUICK_SCENES)
    elif args.scenes == ['all']:
        args.scenes = list(scene_names)
    for name in ('runs', 'burst', 'cr_mb', 'clones', 'density', 'step', 'parallel',
                 'probes', 'cpus', 'sample_interval', 'idle_window',
                 'churn_seconds', 'kiosk_delay'):
        value = getattr(args, name)
        if not math.isfinite(value) or value <= 0:
            parser.error('--' + name.replace('_', '-') + ' must be positive and finite')
    if not math.isfinite(args.auto) or args.auto < 0:
        parser.error('--auto must be nonnegative and finite')
    if args.activity_width is not None and args.activity_width < 1:
        parser.error('--activity-width must be positive')
    if args.density_mem is not None and not (0 < args.density_mem < 100):
        parser.error('--density-mem must be between 0 and 100')
    try:
        if any(int(n) <= 0 for n in args.churn_levels.split(',')):
            raise ValueError()
        parse_size(args.memory)
    except (ValueError, OverflowError):
        parser.error('invalid --memory or --churn-levels')
    return args


def static_elf(path):
    """Whether an ELF executable has no program interpreter."""
    with open(path, 'rb') as file:
        header = file.read(64)
        if len(header) < 64 or header[:4] != b'\x7fELF' or header[4] not in (1, 2):
            return False
        endian = '<' if header[5] == 1 else '>'
        if header[4] == 2:
            offset = struct.unpack_from(endian + 'Q', header, 32)[0]
            size, count = struct.unpack_from(endian + 'HH', header, 54)
        else:
            offset = struct.unpack_from(endian + 'I', header, 28)[0]
            size, count = struct.unpack_from(endian + 'HH', header, 42)
        if size < 4 or count == 0:
            return False
        for index in range(count):
            file.seek(offset + index * size)
            entry = file.read(4)
            if len(entry) != 4 or struct.unpack(endian + 'I', entry)[0] == 3:
                return False
    return True


def prerequisites(args):
    """Return actionable failures for the selected scenes without making resources."""
    errors = []
    if sys.platform != 'linux':
        errors.append('a Linux host is required')
    if os.geteuid() != 0:
        errors.append('run with sudo: runsc needs root')
    try:
        import rich  # noqa: F401
    except ImportError:
        errors.append('install python3-rich for the system Python')
    busybox = shutil.which('busybox')
    if not busybox:
        errors.append('install busybox-static')
    elif not static_elf(busybox):
        errors.append('busybox must be statically linked; install busybox-static')
    try:
        subprocess.run([args.runsc, '--version'], check=True, capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as error:
        errors.append(f'cannot execute runsc --version: {error}; follow the gVisor installation guide')
    if not args.no_limit:
        path = Path('/sys/fs/cgroup/cgroup.controllers')
        controllers = path.read_text().split() if path.exists() else []
        if not {'cpu', 'cpuset', 'memory', 'pids'}.issubset(controllers):
            errors.append('cgroup v2 with cpu, cpuset, memory and pids is required; use --no-limit explicitly to run without limits')
        elif not {'cpu', 'cpuset', 'memory', 'pids'}.issubset(
                (path.parent / 'cgroup.subtree_control').read_text().split()):
            errors.append('enable cpu, cpuset, memory and pids in the parent cgroup.subtree_control; or use --no-limit explicitly')
        elif not os.access(path.parent, os.W_OK):
            errors.append('the cgroup v2 filesystem must be writable')
    if 'systemd' in args.scenes:
        if platform.machine() != 'x86_64':
            errors.append('the Chromium image currently supports x86_64 only')
        if not shutil.which('docker'):
            errors.append('systemd requires Docker; install and start Docker')
        else:
            try:
                subprocess.run(['docker', 'info'], check=True, capture_output=True, timeout=10)
            except (OSError, subprocess.SubprocessError):
                errors.append('systemd requires access to a running Docker daemon')
    return errors
