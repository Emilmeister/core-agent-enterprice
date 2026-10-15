"""Trusted, stdlib-only entrypoint; launched with python -I -S inside Bubblewrap."""
import errno
import json
import os
import resource
import select
import sys
import time


def _authorize(gate_fd, token, deadline):
    timeout = deadline - time.monotonic()
    if len(token) != 32 or timeout <= 0 or not select.select([gate_fd], [], [], timeout)[0]:
        raise ValueError('release timeout')
    # The supervisor writes the token atomically. EOF and short reads deny exec.
    if os.read(gate_fd, 32) != token:
        raise ValueError('release authorization')


def _exec_target(argv, environment):
    try:
        os.execvpe(argv[0], argv, environment)
    except OSError as error:
        kind = 'EXEC'
        if error.errno == errno.ENOENT:
            paths = ([argv[0]] if os.path.dirname(argv[0]) else
                     [os.path.join(directory, argv[0]) for directory in os.get_exec_path(environment)])
            # Resolve only in the isolated filesystem. An existing script/binary
            # can also yield ENOENT when its interpreter/loader is missing.
            if not any(os.path.lexists(path) for path in paths):
                kind = 'MISSING'
        return f'{kind}:{error.errno or 0}'.encode('ascii')


def main():
    config_fd, gate_fd, status_fd = map(int, sys.argv[1:])
    os.set_inheritable(status_fd, False)
    try:
        raw = os.read(config_fd, 65537)
        os.close(config_fd)
        if len(raw) > 65536:
            raise ValueError('config length')
        config = json.loads(raw)
        limits = config['limits']
        for kind, value in ((resource.RLIMIT_CPU, limits['cpu_seconds']),
                            (resource.RLIMIT_AS, limits['address_space_bytes']),
                            (resource.RLIMIT_NOFILE, limits['max_open_files']),
                            (resource.RLIMIT_FSIZE, limits['max_file_bytes']),
                            (resource.RLIMIT_CORE, 0)):
            resource.setrlimit(kind, (value, value))
        with open('/proc/self/status') as handle:
            status = dict(line.rstrip().split(':', 1) for line in handle if ':' in line)
        if any(int(status[name], 16) for name in ('CapInh', 'CapPrm', 'CapEff', 'CapBnd', 'CapAmb')):
            raise ValueError('capabilities')
        if int(status['NoNewPrivs']) != 1 or int(status['Seccomp']) != 2:
            raise ValueError('security state')
        # Close inherited mount/namespace/config descriptors before application exec.
        for entry in os.listdir('/proc/self/fd'):
            descriptor = int(entry)
            if descriptor > 2 and descriptor not in (gate_fd, status_fd):
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        token = bytes.fromhex(config['token'])
        if len(token) != 32 or os.write(status_fd, b'READY') != 5:
            raise ValueError('release setup')
        _authorize(gate_fd, token, config['deadline'])
        os.close(gate_fd)
        os.write(status_fd, _exec_target(config['argv'], config['environment']))
    except BaseException as error:
        try:
            os.write(status_fd, f'ERR:{getattr(error, "errno", 0) or 0}'.encode('ascii'))
        except OSError:
            pass
    os._exit(125)


if __name__ == '__main__':
    main()
