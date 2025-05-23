# This file is part of Cockpit.
#
# Copyright (C) 2022 Red Hat, Inc.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""Bi-directional asyncio.Transport implementations based on file descriptors."""

import asyncio
import collections
import ctypes
import errno
import fcntl
import logging
import os
import select
import signal
import struct
import subprocess
import termios
from typing import Any, ClassVar, Sequence

from .jsonutil import JsonObject, get_int

sys_prctl = ctypes.CDLL(None).prctl


def prctl(*args: int) -> None:
    if sys_prctl(*args) != 0:
        raise OSError('prctl() failed')


SET_PDEATHSIG = 1


logger = logging.getLogger(__name__)
IOV_MAX = 1024  # man 2 writev


class _Transport(asyncio.Transport):
    BLOCK_SIZE: ClassVar[int] = 1024 * 1024

    # A transport always has a loop and a protocol
    _loop: asyncio.AbstractEventLoop
    _protocol: asyncio.Protocol

    _queue: 'collections.deque[bytes] | None'
    _in_fd: int
    _out_fd: int
    _closing: bool
    _is_reading: bool
    _eof: bool
    _eio_is_eof: bool = False

    def __init__(self,
                 loop: asyncio.AbstractEventLoop,
                 protocol: asyncio.Protocol,
                 in_fd: int = -1, out_fd: int = -1,
                 extra: 'dict[str, object] | None' = None):
        super().__init__(extra)

        self._loop = loop
        self._protocol = protocol

        logger.debug('Created transport %s for protocol %s, fds %d %d', self, protocol, in_fd, out_fd)

        self._queue = None
        self._is_reading = False
        self._eof = False
        self._closing = False

        self._in_fd = in_fd
        self._out_fd = out_fd

        os.set_blocking(in_fd, False)
        if out_fd != in_fd:
            os.set_blocking(out_fd, False)

        self._protocol.connection_made(self)
        self.resume_reading()

    def _read_ready(self) -> None:
        logger.debug('Read ready on %s %s %d', self, self._protocol, self._in_fd)
        try:
            data = os.read(self._in_fd, _Transport.BLOCK_SIZE)
        except BlockingIOError:  # pragma: no cover
            return
        except OSError as exc:
            if self._eio_is_eof and exc.errno == errno.EIO:
                # PTY devices return EIO to mean "EOF"
                data = b''
            else:
                # Other errors: terminate the connection
                self.abort(exc)
                return

        if data != b'':
            logger.debug('  read %d bytes', len(data))
            self._protocol.data_received(data)
        else:
            logger.debug('  got EOF')
            self._close_reader()
            keep_open = self._protocol.eof_received()
            if not keep_open:
                self.close()

    def is_reading(self) -> bool:
        return self._is_reading

    def _close_reader(self) -> None:
        self.pause_reading()
        self._in_fd = -1

    def pause_reading(self) -> None:
        if self._is_reading:
            self._loop.remove_reader(self._in_fd)
            self._is_reading = False

    def resume_reading(self) -> None:
        # It's possible that the Protocol could decide to attempt to unpause
        # reading after _close_reader() got called.  Check that the fd is != -1
        # before actually resuming.
        if not self._is_reading and self._in_fd != -1:
            self._loop.add_reader(self._in_fd, self._read_ready)
            self._is_reading = True

    def _close(self) -> None:
        pass

    def abort(self, exc: 'Exception | None' = None) -> None:
        self._closing = True
        self._close_reader()
        self._remove_write_queue()
        self._loop.call_soon(self._protocol.connection_lost, exc)
        self._close()

    def can_write_eof(self) -> bool:
        raise NotImplementedError

    def write_eof(self) -> None:
        assert not self._eof
        self._eof = True
        if self._queue is None:
            logger.debug('%s got EOF.  closing backend.', self)
            self._write_eof_now()
        else:
            logger.debug('%s got EOF.  bytes in queue, deferring close', self)

    def get_write_buffer_size(self) -> int:
        if self._queue is None:
            return 0
        return sum(len(block) for block in self._queue)

    def get_write_buffer_limits(self) -> 'tuple[int, int]':
        return (0, 0)

    def set_write_buffer_limits(self, high: 'int | None' = None, low: 'int | None' = None) -> None:
        assert high is None or high == 0
        assert low is None or low == 0

    def _write_eof_now(self) -> None:
        raise NotImplementedError

    def _write_ready(self) -> None:
        logger.debug('%s _write_ready', self)
        assert self._queue is not None

        try:
            n_bytes = os.writev(self._out_fd, self._queue)
        except BlockingIOError:  # pragma: no cover
            n_bytes = 0
        except OSError as exc:
            self.abort(exc)
            return

        logger.debug('  successfully wrote %d bytes from the queue', n_bytes)

        while n_bytes:
            block = self._queue.popleft()
            if len(block) > n_bytes:
                # This block wasn't completely written.
                logger.debug('  incomplete block.  Stop.')
                self._queue.appendleft(block[n_bytes:])
                break
            n_bytes -= len(block)
            logger.debug('  removed complete block.  %d remains.', n_bytes)

        if not self._queue:
            logger.debug('%s queue drained.')
            self._remove_write_queue()
            if self._eof:
                logger.debug('%s queue drained.  closing backend now.')
                self._write_eof_now()
            if self._closing:
                self.abort()

    def _remove_write_queue(self) -> None:
        if self._queue is not None:
            self._protocol.resume_writing()
            self._loop.remove_writer(self._out_fd)
            self._queue = None

    def _create_write_queue(self, data: bytes) -> None:
        logger.debug('%s creating write queue for fd %s', self, self._out_fd)
        assert self._queue is None
        self._loop.add_writer(self._out_fd, self._write_ready)
        self._queue = collections.deque((data,))
        self._protocol.pause_writing()

    def write(self, data: bytes) -> None:
        # this is a race condition with subprocesses: if we get and process the the "exited"
        # event before seeing BrokenPipeError, we'll try to write to a closed pipe.
        # Do what the standard library does and ignore, instead of assert
        if self._closing:
            logger.debug('ignoring write() to closing transport fd %i', self._out_fd)
            return

        assert not self._eof

        if self._queue is not None:
            self._queue.append(data)

            # writev() will complain if the queue is too long.  Consolidate it.
            if len(self._queue) > IOV_MAX:
                all_data = b''.join(self._queue)
                self._queue.clear()
                self._queue.append(all_data)

            return

        try:
            n_bytes = os.write(self._out_fd, data)
        except BlockingIOError:
            n_bytes = 0
        except OSError as exc:
            self.abort(exc)
            return

        if n_bytes != len(data):
            self._create_write_queue(data[n_bytes:])

    def close(self) -> None:
        if self._closing:
            return

        self._closing = True
        self._close_reader()

        if self._queue is not None:
            # abort() will be called from _write_ready() when it's done
            return

        self.abort()

    def get_protocol(self) -> asyncio.BaseProtocol:
        return self._protocol

    def is_closing(self) -> bool:
        return self._closing

    def set_protocol(self, protocol: asyncio.BaseProtocol) -> None:
        raise NotImplementedError

    def __del__(self) -> None:
        self._close()


class SubprocessProtocol(asyncio.Protocol):
    """An extension to asyncio.Protocol for use with SubprocessTransport."""
    def process_exited(self) -> None:
        """Called when subprocess has exited."""
        raise NotImplementedError


class WindowSize:
    def __init__(self, value: JsonObject):
        self.rows = get_int(value, 'rows')
        self.cols = get_int(value, 'cols')


class SubprocessTransport(_Transport, asyncio.SubprocessTransport):
    """A bi-directional transport speaking with stdin/out of a subprocess.

    Note: this is not really a normal SubprocessTransport.  Although it
    implements the entire API of asyncio.SubprocessTransport, it is not
    designed to be used with asyncio.SubprocessProtocol objects.  Instead, it
    pair with normal Protocol objects which also implement the
    SubprocessProtocol defined in this module (which only has a
    process_exited() method).  Whatever the protocol writes is sent to stdin,
    and whatever comes from stdout is given to the Protocol via the
    .data_received() function.

    If stderr is configured as a pipe, the transport will separately collect
    data from it, making it available via the .get_stderr() method.
    """

    _pty_fd: 'int | None' = None
    _process: 'subprocess.Popen[bytes] | None' = None
    _returncode: 'int | None' = None
    _stderr: 'Spooler | None'

    def get_stderr(self, *, reset: bool = False) -> str:
        if self._stderr is not None:
            return self._stderr.get(reset=reset).decode(errors='replace')
        else:
            return ''

    def watch_exit(self, process: 'subprocess.Popen[bytes]') -> None:
        def flag_exit() -> None:
            assert isinstance(self._protocol, SubprocessProtocol)
            logger.debug('Process exited with status %d', self._returncode)
            if not self._closing:
                self._protocol.process_exited()

        def pidfd_ready() -> None:
            pid, status = os.waitpid(process.pid, 0)
            assert pid == process.pid
            try:
                self._returncode = os.waitstatus_to_exitcode(status)
            except ValueError:
                self._returncode = status
            self._loop.remove_reader(pidfd)
            os.close(pidfd)
            flag_exit()

        def child_watch_fired(pid: int, code: int) -> None:
            assert process.pid == pid
            self._returncode = code
            flag_exit()

        # We first try to create a pidfd to track the process manually.  If
        # that does work, we need to create a SafeChildWatcher, which has been
        # deprecated and removed in Python 3.14.  This effectively means that
        # using Python 3.14 requires that we're running on a kernel with pidfd
        # support, which is fine: the only place we still care about such old
        # kernels is on RHEL8 and we have Python 3.6 there.
        try:
            pidfd = os.pidfd_open(process.pid)
            self._loop.add_reader(pidfd, pidfd_ready)
        except (AttributeError, OSError):
            quark = '_cockpit_transports_child_watcher'
            watcher = getattr(self._loop, quark, None)

            if watcher is None:
                watcher = asyncio.SafeChildWatcher()
                watcher.attach_loop(self._loop)
                setattr(self._loop, quark, watcher)

            watcher.add_child_handler(process.pid, child_watch_fired)

    def __init__(self,
                 loop: asyncio.AbstractEventLoop,
                 protocol: SubprocessProtocol,
                 args: Sequence[str],
                 *,
                 pty: bool = False,
                 window: 'WindowSize | None' = None,
                 **kwargs: Any):

        # go down as a team -- we don't want any leaked processes when the bridge terminates
        def preexec_fn() -> None:
            prctl(SET_PDEATHSIG, signal.SIGTERM)
            if pty:
                fcntl.ioctl(0, termios.TIOCSCTTY, 0)

        if pty:
            self._pty_fd, session_fd = os.openpty()

            if window is not None:
                self.set_window_size(window)

            kwargs['stderr'] = session_fd
            self._process = subprocess.Popen(args,
                                             stdin=session_fd, stdout=session_fd,
                                             preexec_fn=preexec_fn, start_new_session=True, **kwargs)
            os.close(session_fd)

            in_fd, out_fd = self._pty_fd, self._pty_fd
            self._eio_is_eof = True

        else:
            self._process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                             preexec_fn=preexec_fn, **kwargs)
            assert self._process.stdin
            assert self._process.stdout
            in_fd = self._process.stdout.fileno()
            out_fd = self._process.stdin.fileno()

        if self._process.stderr is not None:
            self._stderr = Spooler(loop, self._process.stderr.fileno())
        else:
            self._stderr = None

        super().__init__(loop, protocol, in_fd, out_fd)
        self.watch_exit(self._process)

    def set_window_size(self, size: WindowSize) -> None:
        assert self._pty_fd is not None
        fcntl.ioctl(self._pty_fd, termios.TIOCSWINSZ, struct.pack('2H4x', size.rows, size.cols))

    def can_write_eof(self) -> bool:
        assert self._process is not None
        return self._process.stdin is not None

    def _write_eof_now(self) -> None:
        assert self._process is not None
        assert self._process.stdin is not None
        self._process.stdin.close()
        self._out_fd = -1

    def get_pid(self) -> int:
        assert self._process is not None
        return self._process.pid

    def get_returncode(self) -> 'int | None':
        return self._returncode

    def get_pipe_transport(self, fd: int) -> asyncio.Transport:
        raise NotImplementedError

    def send_signal(self, sig: signal.Signals) -> None:  # type: ignore[override] # mypy/issues/13885
        assert self._process is not None
        # We try to avoid using subprocess.send_signal().  It contains a call
        # to waitpid() internally to avoid signalling the wrong process (if a
        # PID gets reused), but:
        #
        #  - we already detect the process exiting via our pidfd
        #
        #  - the check is actually harmful since collecting the process via
        #    waitpid() prevents our pidfd-based watcher from doing the same,
        #    resulting in an error.
        #
        # It's on us now to check it, but that's easy:
        if self._returncode is not None:
            logger.debug("won't attempt %s to process %i.  It exited already.", sig, self._process.pid)
            return

        try:
            os.kill(self._process.pid, sig)
            logger.debug('sent %s to process %i', sig, self._process.pid)
        except ProcessLookupError:
            # already gone? fine
            logger.debug("can't send %s to process %i.  It's exited just now.", sig, self._process.pid)

    def terminate(self) -> None:
        self.send_signal(signal.SIGTERM)

    def kill(self) -> None:
        self.send_signal(signal.SIGKILL)

    def _close(self) -> None:
        if self._pty_fd is not None:
            os.close(self._pty_fd)
            self._pty_fd = None

        if self._process is not None:
            if self._process.stdin is not None:
                self._process.stdin.close()
                self._process.stdin = None
            try:
                self.terminate()  # best effort...
            except PermissionError:
                logger.debug("can't kill %i due to EPERM", self._process.pid)


class StdioTransport(_Transport):
    """A bi-directional transport that corresponds to stdin/out.

    Can talk to just about anything:
        - files
        - pipes
        - character devices (including terminals)
        - sockets
    """

    def __init__(self, loop: asyncio.AbstractEventLoop, protocol: asyncio.Protocol, stdin: int = 0, stdout: int = 1):
        super().__init__(loop, protocol, stdin, stdout)

    def can_write_eof(self) -> bool:
        return False

    def _write_eof_now(self) -> None:
        raise RuntimeError("Can't write EOF to stdout")


class Spooler:
    """Consumes data from an fd, storing it in a buffer.

    This makes a copy of the fd, so you don't have to worry about holding it
    open.
    """

    _loop: asyncio.AbstractEventLoop
    _fd: int
    _contents: 'list[bytes]'

    def __init__(self, loop: asyncio.AbstractEventLoop, fd: int):
        self._loop = loop
        self._fd = -1  # in case dup() raises an exception
        self._contents = []

        self._fd = os.dup(fd)

        os.set_blocking(self._fd, False)
        loop.add_reader(self._fd, self._read_ready)

    def _read_ready(self) -> None:
        try:
            data = os.read(self._fd, 8192)
        except BlockingIOError:  # pragma: no cover
            return
        except OSError:
            # all other errors -> EOF
            data = b''

        if data != b'':
            self._contents.append(data)
        else:
            self.close()

    def _is_ready(self) -> bool:
        if self._fd == -1:
            return False
        return select.select([self._fd], [], [], 0) != ([], [], [])

    def get(self, *, reset: bool = False) -> bytes:
        while self._is_ready():
            self._read_ready()

        result = b''.join(self._contents)
        if reset:
            self._contents = []
        return result

    def close(self) -> None:
        if self._fd != -1:
            self._loop.remove_reader(self._fd)
            os.close(self._fd)
            self._fd = -1

    def __del__(self) -> None:
        self.close()
