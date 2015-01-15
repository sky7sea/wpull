'''RPC processes.'''

import abc
import gettext
import json
import logging
import subprocess
import atexit
import errno
import time

from trollius import From
import trollius

from wpull.backport.logging import BraceMessage as __


_ = gettext.gettext
_logger = logging.getLogger(__name__)


class Process(object):
    '''Subprocess wrapper.'''
    def __init__(self, proc_args, stdout_callback=None, stderr_callback=None):
        self._proc_args = proc_args
        self._stdout_callback = stdout_callback
        self._stderr_callback = stderr_callback
        self._process = None
        self._stderr_reader = None
        self._stdout_reader = None

    @property
    def process(self):
        '''Return the underlying process.'''
        return self._process

    @trollius.coroutine
    def start(self, use_atexit=True):
        '''Start the executable.

        Args:
            use_atexit (bool): If True, the process will automatically be
                terminated at exit.
        '''
        assert not self._process

        _logger.debug('Starting process %s', self._proc_args)

        process_future = trollius.create_subprocess_exec(
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            *self._proc_args
        )
        self._process = yield From(process_future)

        self._stderr_reader = trollius.async(self._read_stderr())
        self._stdout_reader = trollius.async(self._read_stdout())

        if use_atexit:
            atexit.register(self.close)

    def close(self):
        '''Terminate or kill the subprocess.

        This function is blocking.
        '''
        if not self._process:
            return

        if self._process.returncode is not None:
            return

        _logger.debug('Terminate process.')

        try:
            self._process.terminate()
        except OSError as error:
            if error.errno != errno.ESRCH:
                raise

        for dummy in range(10):
            if self._process.returncode is not None:
                return

            time.sleep(0.05)

        _logger.debug('Failed to terminate. Killing.')

        try:
            self._process.kill()
        except OSError as error:
            if error.errno != errno.ESRCH:
                raise

    @trollius.coroutine
    def _read_stdout(self):
        '''Continously read the stdout for RPC messages.'''
        try:
            while self._process.returncode is None:
                line = yield From(self._process.stdout.readline())

                _logger.debug('Read stdout line %s', repr(line))

                if not line:
                    break

                if self._stdout_callback:
                    yield From(self._stdout_callback(line))

        except Exception:
            _logger.exception('Unhandled read stdout exception.')
            raise

    @trollius.coroutine
    def _read_stderr(self):
        '''Continously read stderr for error messages.'''
        try:
            while self._process.returncode is None:
                line = yield From(self._process.stderr.readline())

                if not line:
                    break

                if self._stderr_callback:
                    yield From(self._stderr_callback(line))

        except Exception:
            _logger.exception('Unhandled read stderr exception.')
            raise


class RPCProcess(Process):
    '''RPC subprocess wrapper.'''
    def __init__(self, proc_args, message_callback):
        super().__init__(proc_args, stdout_callback=self._stdout_callback,
                         stderr_callback=self._stderr_callback)
        self._message_callback = message_callback

    @trollius.coroutine
    def _stdout_callback(self, line):
        '''Handle stdout message.'''
        if line.startswith(b'!RPC '):
            message = json.loads(line[5:].decode('utf-8'))
            return_value = self._message_callback(message)

            if return_value is not None:
                yield From(self.send_message(return_value))
        else:
            _logger.warning(__(
                _('Subprocess: {message}'),
                message=line.decode('utf-8', 'replace').rstrip()
            ))

    @trollius.coroutine
    def _stderr_callback(self, line):
        '''Handle stderr output'''
        _logger.warning(__(
            _('Subprocess: {message}'),
            message=line.decode('utf-8', 'replace').rstrip()
        ))

    @trollius.coroutine
    def send_message(self, message):
        '''Send a RPC message.'''
        self._process.stdin.write(b'!RPC ')
        self._process.stdin.write(json.dumps(message).encode('utf-8'))
        self._process.stdin.write(b'\n')
        yield From(self._process.stdin.drain())
