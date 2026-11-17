"""Private run directories and caller-owned result files."""

import json
import pwd
import os
from pathlib import Path
import secrets
import stat
import tempfile


def create_workdir(requested=None):
    """Create a new private directory beneath trusted, symlink-free ancestors."""
    if requested is None:
        # Ignore TMPDIR when running a privileged demo.
        return Path(tempfile.mkdtemp(prefix='gvisor-demo-', dir='/tmp'))
    path = Path(os.path.abspath(requested))
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
            info = os.fstat(fd)
            shared_tmp = info.st_uid == 0 and bool(info.st_mode & stat.S_ISVTX)
            if (info.st_uid != os.geteuid() and not shared_tmp) or (info.st_mode & 0o022 and not shared_tmp):
                raise ValueError('workdir ancestors must be owned by root and not writable by other users')
        os.mkdir(path.name, mode=0o700, dir_fd=fd)
    finally:
        os.close(fd)
    return path


class ResultFile:
    """An exclusive result file opened with the invoking user's permissions."""

    def __init__(self, requested=None):
        self.path = Path(requested or f'gvisor-demo-results-{secrets.token_hex(6)}.json').absolute()
        uid, gid = os.geteuid(), os.getegid()
        groups = os.getgroups()
        try:
            if uid == 0 and 'SUDO_UID' in os.environ:
                caller_uid = int(os.environ['SUDO_UID'])
                caller_gid = int(os.environ['SUDO_GID'])
                try:
                    caller = pwd.getpwuid(caller_uid)
                    caller_groups = os.getgrouplist(caller.pw_name, caller_gid)
                except KeyError:
                    caller_groups = [caller_gid]
                os.setgroups(caller_groups)
                os.setegid(caller_gid)
                os.seteuid(caller_uid)
            self.fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        finally:
            os.seteuid(uid)
            os.setegid(gid)
            if uid == 0:
                os.setgroups(groups)

    def save(self, results):
        data = (json.dumps(results, indent=2, allow_nan=False) + '\n').encode()
        os.lseek(self.fd, 0, os.SEEK_SET)
        os.ftruncate(self.fd, 0)
        with os.fdopen(os.dup(self.fd), 'wb') as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())

    def close(self):
        os.close(self.fd)


def private_cache(path):
    """Create or validate a root-owned cache below the sticky /var/tmp directory."""
    path = Path(path)
    try:
        create_workdir(path)
    except FileExistsError:
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ValueError(f'cache must be a private owned directory: {path}')
    return path



def cleanup_session(rs, cgroup, workdir, remove_cgroup):
    """Attempt all teardown stages and preserve state after unresolved errors."""
    errors = []
    cgroup_error = None
    if rs is not None:
        rs.cancel_pending()
        try:
            rs.destroy_all()
        except Exception as error:
            # Cgroup teardown can stop sandboxes even if runsc failed.
            errors.append(f'sandbox teardown: {error}')
    if cgroup:
        try:
            remove_cgroup(cgroup)
        except Exception as error:
            cgroup_error = error
    if rs is not None:
        try:
            rs.cleanup(preserve=bool(errors) or cgroup_error is not None)
        except Exception as error:
            errors.append(f'runtime cleanup: {error}')
    if cgroup_error is not None:
        try:
            # Older kernels need runsc kill/delete before cgroups can go away.
            remove_cgroup(cgroup)
            if rs is not None:
                rs.cleanup(preserve=bool(errors))
        except Exception as error:
            errors.append(f'cgroup cleanup: {error}')
    if errors:
        raise RuntimeError('; '.join(errors))
    if workdir is not None:
        workdir.rmdir()
