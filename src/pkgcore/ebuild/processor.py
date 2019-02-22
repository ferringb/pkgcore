# Copyright: 2004-2011 Brian Harring <ferringb@gmail.com>
# License: GPL2/BSD

"""
low level ebuild processor.

This basically is a coprocessor that controls a bash daemon for actual
ebuild execution. Via this, the bash side can reach into the python
side (and vice versa), enabling remote trees (piping data from python
side into bash side for example).

A couple of processors are left lingering while pkgcore is running for
the purpose of avoiding spawning overhead, this (and the general
design) reduces regen time by over 40% compared to portage-2.1
"""

# this needs work. it's been pruned heavily from what ebd used
# originally, but it still isn't what I would define as 'right'

__all__ = (
    "request_ebuild_processor", "release_ebuild_processor", "EbuildProcessor",
    "UnhandledCommand", "expected_ebuild_env")

try:
    import threading
    _global_ebp_lock = threading.Lock()
    _acquire_global_ebp_lock = _global_ebp_lock.acquire
    _release_global_ebp_lock = _global_ebp_lock.release
except ImportError:
    def _acquire_global_ebp_lock():
        pass

    def _release_global_ebp_lock():
        pass


inactive_ebp_list = []
active_ebp_list = []

import contextlib
import errno
from functools import partial
import os
import signal
import sys

from snakeoil import klass
from snakeoil.currying import pretty_docs
from snakeoil.demandload import demandload
from snakeoil.osutils import abspath, normpath, pjoin

from pkgcore import __file__, const, os_data
from pkgcore.ebuild import const as e_const
from pkgcore.exceptions import PkgcoreException

demandload(
    'logging',
    'itertools:chain',
    'traceback',
    'snakeoil:fileutils',
    'snakeoil:process',
    'snakeoil.process:spawn',
    'pkgcore.log:logger',
)


def _single_thread_allowed(functor):
    """Decorator that forces method to run under single thread."""
    def _inner(*args, **kwargs):
        _acquire_global_ebp_lock()
        try:
            return functor(*args, **kwargs)
        finally:
            _release_global_ebp_lock()
    _inner.func = functor
    pretty_docs(_inner, name=functor.__name__)
    return _inner


@_single_thread_allowed
def forget_all_processors():
    active_ebp_list[:] = []
    inactive_ebp_list[:] = []


@_single_thread_allowed
def shutdown_all_processors():
    """Kill off all known processors."""
    try:
        while active_ebp_list:
            try:
                active_ebp_list.pop().shutdown_processor(
                    ignore_keyboard_interrupt=True)
            except EnvironmentError:
                pass

        while inactive_ebp_list:
            try:
                inactive_ebp_list.pop().shutdown_processor(
                    ignore_keyboard_interrupt=True)
            except EnvironmentError:
                pass
    except Exception as e:
        traceback.print_exc()
        logger.error(e)
        raise

spawn.atexit_register(shutdown_all_processors)


@_single_thread_allowed
def request_ebuild_processor(userpriv=False, sandbox=None, fd_pipes=None):
    """Request an ebuild_processor instance, creating a new one if needed.

    :return: :obj:`EbuildProcessor`
    :param userpriv: should the processor be deprived to
        :obj:`pkgcore.os_data.portage_gid` and :obj:`pkgcore.os_data.portage_uid`?
    :param sandbox: should the processor be sandboxed?
    """

    if sandbox is None:
        sandbox = spawn.is_sandbox_capable()

    for ebp in inactive_ebp_list:
        if ebp.userprived() == userpriv and (ebp.sandboxed() or not sandbox):
            if not ebp.is_alive:
                inactive_ebp_list.remove(ebp)
                continue
            inactive_ebp_list.remove(ebp)
            active_ebp_list.append(ebp)
            break
    else:
        ebp = EbuildProcessor(userpriv, sandbox, fd_pipes=fd_pipes)
        active_ebp_list.append(ebp)

    return ebp


@_single_thread_allowed
def release_ebuild_processor(ebp):
    """The inverse of request_ebuild_processor.

    Any processor requested via request_ebuild_processor B{must} be released
    via this function once it's no longer in use.

    :param ebp: :obj:`EbuildProcessor` instance
    :return: boolean indicating release results- if the processor isn't known
        as active, False is returned.
        If a processor isn't known as active, this means either calling
        error or an internal error.
    """

    try:
        active_ebp_list.remove(ebp)
    except ValueError:
        return False

    assert ebp not in inactive_ebp_list
    # We can't reuse processors that use custom fd mappings or are locked on
    # release for one reason or another.
    if ebp.locked or ebp._fd_pipes:
        ebp.shutdown_processor()
    else:
        inactive_ebp_list.append(ebp)
    return True


@_single_thread_allowed
def drop_ebuild_processor(ebp):
    """Force an ebuild processor to be dropped from active/inactive lists.

    :param ebp: :obj:`EbuildProcessor` instance
    """
    try:
        active_ebp_list.remove(ebp)
    except ValueError:
        pass

    try:
        inactive_ebp_list.remove(ebp)
    except ValueError:
        pass

@contextlib.contextmanager
def reuse_or_request(ebp=None, **kwargs):
    """Do a processor operation, locking as necessary.

    If the processor is given, it's assumed to be locked already.
    If no processor is given, one is allocated, then released upon
    finishing."""
    release_required = ebp is None
    try:
        if ebp is None:
            ebp = request_ebuild_processor(**kwargs)
        yield ebp
    finally:
        if release_required and ebp is not None:
            release_ebuild_processor(ebp)


class ProcessingInterruption(PkgcoreException):
    """Generic ebuild processor exception."""


class FinishedProcessing(ProcessingInterruption):
    """Signal ebuild processor that current command has finished running."""

    def __init__(self, val, msg=None):
        super().__init__(f"Finished processing with val, {val}")
        self.val, self.msg = val, msg


class UnhandledCommand(ProcessingInterruption):
    """Bash processor sent an unknown command."""

    def __init__(self, line=None):
        super().__init__(f"unhandled command, {line}")
        self.line = line
        self.args = (line,)


class InternalError(ProcessingInterruption):
    """Ebuild processor encountered an internal error or invalid command."""

    def __init__(self, line=None, msg=None):
        super().__init__(f"Internal error occurred: line={line!r}, msg={msg!r}")
        self.line, self.msg = line, msg
        self.args = (line, msg)


class TimeoutError(PkgcoreException):
    """Bash processor timed out."""


class ProcessorError(PkgcoreException):
    """Bash processor returned a failure."""


def chuck_TermInterrupt(ebp, *args):
    """Event handler for bash side 'term' command."""
    # Retrieve any message that the bash side was not able to output due to
    # redirection, e.g. running a command while redirecting stderr to /dev/null.
    msg = ebp.read().strip('\0').strip('\n')
    if msg:
        msg = '\n'.join(msg.split('\0'))
        print(msg, file=sys.stderr)
    drop_ebuild_processor(ebp)
    ebp.shutdown_processor(force=True)
    raise FinishedProcessing(False)


def chuck_KeyboardInterrupt(*args):
    """Event handler for SIGINT."""
    for ebp in chain(active_ebp_list, inactive_ebp_list):
        drop_ebuild_processor(ebp)
        ebp.shutdown_processor(force=True)
    raise KeyboardInterrupt("ctrl+c encountered")

signal.signal(signal.SIGINT, chuck_KeyboardInterrupt)


def chuck_UnhandledCommand(processor, line):
    """Event handler for unhandled commands."""
    raise UnhandledCommand(line)


def chuck_StoppingCommand(val, processor, *args):
    """Event handler for successful phase/command completion."""
    if callable(val):
        raise FinishedProcessing(val(args[0]))
    raise FinishedProcessing(val)


class EbuildProcessor(object):
    """Abstraction of a running ebd instance.

    Contains the env, functions, etc that ebuilds expect.
    """

    def __init__(self, userpriv, sandbox, fd_pipes=None):
        """
        :param sandbox: enables a sandboxed processor
        :param userpriv: enables a userpriv'd processor
        :param fd_pipes: mapping from existing fd to fd inside the ebd process
        """
        self.lock()
        self.ebd = e_const.EBUILD_DAEMON_PATH
        spawn_opts = {'umask': 0o002}

        self._preloaded_eclasses = {}
        self._eclass_caching = False
        self._outstanding_expects = []
        self._metadata_paths = None

        if userpriv:
            self.__userpriv = True
            spawn_opts.update({
                "uid": os_data.portage_uid,
                "gid": os_data.portage_gid,
                "groups": [os_data.portage_gid],
            })
        else:
            if spawn.is_userpriv_capable():
                spawn_opts.update({
                    "gid": os_data.portage_gid,
                    "groups": [0, os_data.portage_gid],
                })
            self.__userpriv = False

        # open the pipes to be used for chatting with the new daemon
        cread, cwrite = os.pipe()
        dread, dwrite = os.pipe()
        self.__sandbox = False

        self._fd_pipes = fd_pipes if fd_pipes is not None else {}

        # since it's questionable which spawn method we'll use (if
        # sandbox fex), we ensure the bashrc is invalid.
        env = {x: "/etc/portage/spork/not/valid/ha/ha"
               for x in ("BASHRC", "BASH_ENV")}

        if int(os.environ.get('PKGCORE_PERF_DEBUG', 0)):
            env["PKGCORE_PERF_DEBUG"] = os.environ['PKGCORE_PERF_DEBUG']
        if int(os.environ.get('PKGCORE_DEBUG', 0)):
            env["PKGCORE_DEBUG"] = os.environ['PKGCORE_DEBUG']
        if int(os.environ.get('PKGCORE_NOCOLOR', 0)):
            env["PKGCORE_NOCOLOR"] = os.environ['PKGCORE_NOCOLOR']
            if sandbox:
                env["NOCOLOR"] = os.environ['PKGCORE_NOCOLOR']

        # prepend script dir to PATH for git repo or unpacked tarball, for
        # installed versions it's empty
        env["PATH"] = os.pathsep.join(
            list(const.PATH_FORCED_PREPEND) + [os.environ["PATH"]])

        if sandbox:
            if not spawn.is_sandbox_capable():
                raise ValueError("spawn lacks sandbox capabilities")
            self.__sandbox = True
            spawn_func = spawn.spawn_sandbox
#            env.update({"SANDBOX_DEBUG":"1", "SANDBOX_DEBUG_LOG":"/var/tmp/test"})
        else:
            spawn_func = spawn.spawn

        # force to a neutral dir so that sandbox won't explode if
        # ran from a nonexistent dir
        spawn_opts["cwd"] = e_const.EBD_PATH

        # Force the pipes to be high up fd wise so nobody stupidly hits 'em, we
        # start from max-3 to avoid a bug in older bash where it doesn't check
        # if an fd is in use before claiming it.
        max_fd = min(spawn.max_fd_limit, 1024)
        env.update({
            "PKGCORE_EBD_READ_FD": str(max_fd-4),
            "PKGCORE_EBD_WRITE_FD": str(max_fd-3),
        })

        # allow any pipe overrides except the ones we use to communicate
        ebd_pipes = {0: 0, 1: 1, 2: 2}
        ebd_pipes.update(self._fd_pipes)
        ebd_pipes.update({max_fd-4: cread, max_fd-3: dwrite})

        # pgid=0: Each ebuild processor is the process group leader for all its
        # spawned children so everything can be terminated easily if necessary.
        self.pid = spawn_func(
            [spawn.BASH_BINARY, self.ebd, "daemonize"],
            fd_pipes=ebd_pipes, returnpid=True, env=env, pgid=0, **spawn_opts)[0]

        os.close(cread)
        os.close(dwrite)
        self.ebd_write = os.fdopen(cwrite, "w")
        self.ebd_read = os.fdopen(dread, "r")

        # basically a quick "yo" to the daemon
        self.write("dude?")
        if not self.expect("dude!"):
            logger.error("error in server coms, bailing.")
            raise InternalError(
                "expected 'dude!' response from ebd, which wasn't received. "
                "likely a bug")

        # send PKGCORE_PYTHON_BINARY...
        self.write(spawn.find_invoking_python())
        self.write(
            os.pathsep.join([
                normpath(abspath(pjoin(__file__, os.pardir, os.pardir))),
                os.environ.get('PYTHONPATH', '')])
            )
        if self.__sandbox:
            self.write("sandbox_log?")
            self.__sandbox_log = self.read().split()[0]
        else:
            self.write("no_sandbox")
        self._readonly_vars = frozenset(self.read().split())
        # locking isn't used much, but w/ threading this will matter
        self.unlock()

    def run_phase(self, phase, env, tmpdir=None, logging=None,
                  additional_commands=None, sandbox=True):
        """Utility function, to initialize the processor for a phase.

        Used to combine multiple calls into one, leaving the processor
        in a state where all that remains is a call start_processing
        call, then generic_handler event loop.

        :param phase: phase to prep for
        :type phase: str
        :param env: mapping of the environment to prep the processor with
        :param sandbox: should the sandbox be enabled?
        :param logging: None, or a filepath to log the output from the
            processor to
        :return: True for success, False for everything else
        """

        self.write(f"process_ebuild {phase}")
        if not self.send_env(env, tmpdir=tmpdir):
            return False
        self.write(f"set_sandbox_state {int(sandbox)}")
        if logging:
            if not self.set_logfile(logging):
                return False
        self.write("start_processing")
        return self.generic_handler(additional_commands=additional_commands)

    def sandboxed(self):
        """Is this instance sandboxed?"""
        return self.__sandbox

    def userprived(self):
        """Is this instance userprived?"""
        return self.__userpriv

    def write(self, string, flush=True, disable_runtime_exceptions=False,
              append_newline=True):
        """Send something to the bash side.

        :param string: string to write to the bash processor.
            All strings written are automatically \\n terminated.
        :param flush: boolean controlling whether the data is flushed
            immediately.  Disabling flush is useful when dumping large
            amounts of data.
        """
        string = str(string)
        try:
            if append_newline:
                if string != '\n':
                    string += "\n"
            #logger.debug("wrote %i: %s" % (len(string), string))
            self.ebd_write.write(string)
            if flush:
                self.ebd_write.flush()
        except IOError as ie:
            if ie.errno == errno.EPIPE and not disable_runtime_exceptions:
                raise RuntimeError(ie)
            raise

    def _consume_async_expects(self):
        if any(x[0] for x in self._outstanding_expects):
            self.ebd_write.flush()
        got = [x.rstrip('\n') for x in self.readlines(len(self._outstanding_expects))]
        ret = (got == [x[1] for x in self._outstanding_expects])
        self._outstanding_expects = []
        return ret

    def _timeout_ebp(self, signum, frame):
        raise TimeoutError("ebp for pid '%i' appears dead, timing out" % self.pid)

    def expect(self, want, async_req=False, flush=False, timeout=0):
        """Read from the daemon, check if the returned string is expected.

        :param want: string we're expecting
        :return: boolean, was what was read == want?
        """
        if timeout:
            signal.signal(signal.SIGALRM, self._timeout_ebp)
            signal.setitimer(signal.ITIMER_REAL, timeout)

        if async_req:
            self._outstanding_expects.append((flush, want))
            return True
        if flush:
            self.ebd_write.flush()
        if not self._outstanding_expects:
            try:
                return want == self.read().rstrip('\n')
            except TimeoutError:
                return False
            finally:
                if timeout:
                    signal.setitimer(signal.ITIMER_REAL, 0)
                    signal.signal(signal.SIGALRM, signal.SIG_DFL)

        self._outstanding_expects.append((flush, want))
        return self._consume_async_expects()

    def readlines(self, lines, ignore_killed=False):
        mydata = []
        while lines > 0:
            mydata.append(self.ebd_read.readline())
            cmd = mydata[-1]
            if cmd.startswith("killed"):
                chuck_KeyboardInterrupt(self)
            elif cmd.startswith('term'):
                chuck_TermInterrupt(self)
            lines -= 1
        return mydata

    def read(self, lines=1, ignore_killed=False):
        """Read data from the daemon.

        Shouldn't be called except internally.
        """
        return "\n".join(self.readlines(lines, ignore_killed=ignore_killed))

    def sandbox_summary(self, move_log=False):
        """If the instance is sandboxed, print the sandbox access summary.

        :param move_log: location to move the sandbox log to if a failure occurred
        """
        if not os.path.exists(self.__sandbox_log):
            self.write("end_sandbox_summary")
            return 0
        with open(self.__sandbox_log, "r") as f:
            violations = [x.strip() for x in f if x.strip()]
        if not violations:
            self.write("end_sandbox_summary")
            return 0
        if not move_log:
            move_log = self.__sandbox_log
        elif move_log != self.__sandbox_log:
            with open(move_log) as myf:
                for x in violations:
                    myf.write(x+"\n")

        # XXX this is fugly, use a colorizer or something
        # (but it is better than "from output import red" (portage's output))
        def red(text):
            return '\x1b[31;1m%s\x1b[39;49;00m' % (text,)

        self.write(red(
            "--------------------------- ACCESS VIOLATION SUMMARY "
            "---------------------------")+"\n")
        self.write(red(f"LOG FILE = \"{move_log}\"")+"\n\n")
        for x in violations:
            self.write(x+"\n")
        self.write(red(
            "-----------------------------------------------------"
            "---------------------------")+"\n")
        self.write("end_sandbox_summary")
        try:
            os.remove(self.__sandbox_log)
        except (IOError, OSError) as e:
            logger.error(f"exception caught when cleansing sandbox_log={e}")
        return 1

    def clear_preloaded_eclasses(self):
        if self.is_alive:
            self.write("clear_preloaded_eclasses")
            if not self.expect("clear_preload_eclasses succeeded", flush=True):
                self.shutdown_processor()
                return False
        self._preloaded_eclasses.clear()
        return True

    def preload_eclasses(self, cache, async_req=False, limited_to=None):
        """Preload an eclass stack's eclasses into bash functions.

        Avoids the cost of going to disk on inherit. Preloading eutils
        (which is heavily inherited) speeds up regen times for
        example.

        :param ec_file: filepath of eclass to preload
        :return: boolean, True for success
        """
        ec = cache.eclasses
        if limited_to:
            i = ((eclass, ec[eclass]) for eclass in limited_to)
        else:
            i = cache.eclasses.items()
        for eclass, data in i:
            if data.path != self._preloaded_eclasses.get(eclass):
                if self._preload_eclass(data.path, async_req=True):
                    self._preloaded_eclasses[eclass] = data.path
        if not async_req:
            return self._consume_async_expects()
        return True

    def allow_eclass_caching(self):
        self._eclass_caching = True

    def disable_eclass_caching(self):
        self.clear_preloaded_eclasses()
        self._eclass_caching = False

    def _preload_eclass(self, ec_file, async_req=False):
        """Preload an eclass into a bash function.

        Avoids the cost of going to disk on inherit. Preloading eutils
        (which is heavily inherited) speeds up regen times for
        example.

        :param ec_file: filepath of eclass to preload
        :return: boolean, True for success
        """
        if not os.path.exists(ec_file):
            logger.error(f"failed: {ec_file}")
            return False
        self.write(f"preload_eclass {ec_file}")
        if self.expect("preload_eclass succeeded", async_req=async_req, flush=True):
            return True
        return False

    def lock(self):
        """Lock the processor.

        Currently doesn't block any access, but will.
        """
        self.processing_lock = True

    def unlock(self):
        """Unlock the processor."""
        self.processing_lock = False

    locked = klass.alias_attr('processing_lock')

    @property
    def is_alive(self):
        """Returns if it's known if the processor has been shutdown."""
        try:
            if self.pid is None:
                return False
            try:
                if process.is_running(self.pid):
                    self.write("alive", disable_runtime_exceptions=True)
                    if self.expect("yep!", timeout=10):
                        return True
            except process.ProcessNotFound:
                # pid doesn't exist
                self.pid = None
            return False

        except (AttributeError, KeyboardInterrupt):
            # thrown only if failure occurred instantiation.
            return False

    def shutdown_processor(self, force=False, ignore_keyboard_interrupt=False):
        """Tell the daemon to shut itself down, and mark this instance as dead."""
        kill = False or force
        if not force:
            try:
                if self.pid is None:
                    return
                elif self.is_alive:
                    self.write("shutdown_daemon", disable_runtime_exceptions=True)
                    self.ebd_write.close()
                    self.ebd_read.close()
                else:
                    kill = True
            except FinishedProcessing:
                # TODO: remove this hack once bash side tracking is reworked
                # Handle case where we received a term interrupt while exiting,
                # e.g. IPC command dying.
                return
            except (EnvironmentError, ValueError):
                kill = True

        # TODO: add fallback to SIGKILL on failure
        if kill:
            os.killpg(self.pid, signal.SIGTERM)

        # now we wait for the process group
        try:
            os.waitpid(-self.pid, 0)
        except KeyboardInterrupt:
            if not ignore_keyboard_interrupt:
                raise

        # currently, this assumes all went well.
        # which isn't always true.
        self.pid = None

    def _generate_env_str(self, env_dict):
        data = []
        for key, val in sorted(env_dict.items()):
            if key in self._readonly_vars:
                continue
            if not key[0].isalpha():
                raise KeyError(f"{key}: bash doesn't allow digits as the first char")
            if not isinstance(val, (str, list, tuple)):
                raise ValueError(
                    f"_generate_env_str was fed a bad value; key={key}, val={val}")

            if isinstance(val, (list, tuple)):
                data.append("%s=(%s)" % (key, ' '.join(
                    f'[{i}]="{value}"' for i, value in enumerate(val))))
            elif val.isalnum():
                data.append(f"{key}={val}")
            elif "'" not in val:
                data.append(f"{key}='{val}'")
            else:
                data.append("%s=$'%s'" % (key, val.replace("'", "\\'")))

        # TODO: Move to using unprefixed lines to avoid leaking internal
        # variables to spawned commands once we use builtins for all commands
        # currently using pkgcore-ebuild-helper.
        return f"export {' '.join(data)}"

    def send_env(self, env_dict, async_req=False, tmpdir=None):
        """Transfer the ebuild's desired env (env_dict) to the running daemon.

        :type env_dict: mapping with string keys and values.
        :param env_dict: the bash env.
        """
        data = self._generate_env_str(env_dict)
        old_umask = os.umask(0o002)
        if tmpdir:
            path = pjoin(tmpdir, 'ebd-env-transfer')
            fileutils.write_file(path, 'wb', data.encode())
            self.write(f"start_receiving_env file {path}")
        else:
            self.write(
                f"start_receiving_env bytes {len(data)}\n{data}",
                append_newline=False)
        os.umask(old_umask)
        return self.expect("env_received", async_req=async_req, flush=True)

    def set_logfile(self, logfile=''):
        """
        Set the logfile (location to log to).

        Relevant only when the daemon is sandbox'd,

        :param logfile: filepath to log to
        """
        self.write(f"logging {logfile}")
        return self.expect("logging_ack")

    def __del__(self):
        """simply attempts to notify the daemon to die"""
        # for this to be reached means we ain't in a list no more.
        if self.is_alive:
            # I'd love to know why the exception wrapping is required...
            try:
                self.shutdown_processor()
            except TypeError:
                pass

    def _ensure_metadata_paths(self, paths):
        paths = tuple(paths)
        if self._metadata_paths == paths:
            return
        # filter here, so that a screwy default doesn't result in resetting it
        # every time.
        data = os.pathsep.join(filter(None, paths))
        self.write(f"set_metadata_path {len(data)}\n{data}", append_newline=False)
        if self.expect("metadata_path_received", flush=True):
            self._metadata_paths = paths

    def _run_depend_like_phase(self, command, package_inst, eclass_cache,
                               extra_commands={}):
        # ebuild is not allowed to run any external programs during
        # depend phases; use /dev/null since "" == "."
        self._ensure_metadata_paths(("/dev/null",))

        env = expected_ebuild_env(package_inst, depends=True)
        data = self._generate_env_str(env)
        self.write(f"{command} {len(data)}\n{data}", append_newline=False)

        updates = None
        if self._eclass_caching:
            updates = set()
        commands = extra_commands.copy()
        commands["request_inherit"] = partial(inherit_handler, eclass_cache, updates=updates)
        val = self.generic_handler(additional_commands=commands)

        if not val:
            logger.debug(f"returned val from {command} was '{val}'")
            raise ProcessorError(val)

        if updates:
            self.preload_eclasses(eclass_cache, limited_to=updates, async_req=True)

    def get_ebuild_environment(self, package_inst, eclass_cache):
        """Request a dump of the ebuild environ for a package.

        This dump is created from doing metadata sourcing.

        :param package_inst: :obj:`pkgcore.ebuild.ebuild_src.package` instance
            to regenerate
        :param eclass_cache: :obj:`pkgcore.ebuild.eclass_cache` instance to use
            for eclass access
        :return: string of the ebuild environment.
        """

        environ = []

        def receive_env(self, line):
            if environ:
                raise InternalError(line, "receive_env was invoked twice.")
            line = line.strip()
            if not line:
                raise InternalError(line, "During env receive, ebd didn't give us a size.")
            elif not line.isdigit():
                raise InternalError(line, "Returned size wasn't an integer")
            # This is a raw transfer, for obvious reasons.
            environ.append(self.ebd_read.read(int(line)))

        self._run_depend_like_phase('gen_ebuild_env', package_inst, eclass_cache,
                                    {'receive_env': receive_env})
        if not environ:
            raise InternalError(None, "receive_env was never invoked.")
        # Dump any leading/trailing spaces.
        return environ[0].strip()

    def get_keys(self, package_inst, eclass_cache):
        """
        request the metadata be regenerated from an ebuild

        :param package_inst: :obj:`pkgcore.ebuild.ebuild_src.package` instance
            to regenerate
        :param eclass_cache: :obj:`pkgcore.ebuild.eclass_cache` instance to use
            for eclass access
        :return: dict when successful, None when failed
        """
        metadata_keys = {}

        def receive_key(self, line):
            line = line.split("=", 1)
            if len(line) != 2:
                raise FinishedProcessing(True)
            metadata_keys[line[0]] = line[1]

        self._run_depend_like_phase('gen_metadata', package_inst, eclass_cache,
                                    {"key": receive_key})

        return metadata_keys

    # this basically handles all hijacks from the daemon, whether
    # confcache or portageq.
    def generic_handler(self, additional_commands=None):
        """Internal event handler responding to the running processor's requests.

        :type additional_commands: mapping from string to callable.
        :param additional_commands: Extra command handlers.
            Command names cannot have spaces.
            The callable is called with the processor as first arg, and
            remaining string (None if no remaining fragment) as second arg.
            If you need to split the args to command, whitespace splitting
            falls to your func.

        :raise UnhandledCommand: thrown when an unknown command is encountered.
        """

        # note that self is passed in. so... we just pass in the
        # unbound instance. Specifically, via digging through
        # __class__ if you don't do it, sandbox_summary (fex) cannot
        # be overridden, this func will just use this classes version.
        # so dig through self.__class__ for it. :P

        handlers = {"request_sandbox_summary": self.__class__.sandbox_summary}
        f = chuck_UnhandledCommand
        for x in ("prob", "env_receiving_failed", "failed"):
            handlers[x] = f
        del f

        handlers["phases"] = partial(
            chuck_StoppingCommand, lambda f: f.lower().strip() == "succeeded")

        handlers["killed"] = chuck_KeyboardInterrupt
        handlers["term"] = chuck_TermInterrupt

        if additional_commands is not None:
            for x in additional_commands:
                if not callable(additional_commands[x]):
                    raise TypeError(additional_commands[x])

            handlers.update(additional_commands)

        self.lock()

        try:
            if self._outstanding_expects:
                if not self._consume_async_expects():
                    logger.error("error in daemon")
                    raise UnhandledCommand("expects out of alignment")
            while True:
                line = self.read().strip()
                # split on first whitespace
                cmd, _, args_str = line.partition(' ')
                if not cmd:
                    raise InternalError(
                        f"Expected command; instead got nothing from {line!r}")
                if cmd in handlers:
                    args = []
                    if args_str:
                        args.append(args_str)
                    # TODO: handle exceptions raised from handlers better
                    handlers[cmd](self, *args)
                else:
                    logger.error(f"unhandled command {cmd!r}, line {line!r}")
                    raise UnhandledCommand(line)

        except FinishedProcessing as fp:
            v = fp.val
            self.unlock()
            return v

def inherit_handler(ecache, ebp, line, updates=None):
    """Callback for implementing inherit digging into eclass_cache.

    Not for normal consumption.
    """
    if line is None:
        ebp.write("failed")
        raise UnhandledCommand(
            "inherit requires an eclass specified, none specified")

    line = line.strip()
    eclass = ecache.get_eclass(line)
    if eclass is None:
        ebp.write("failed")
        raise UnhandledCommand(
            f"inherit requires an unknown eclass, {line} cannot be found")

    if eclass.path is not None:
        ebp.write("path")
        ebp.write(eclass.path)
    else:
        # XXX $10 this doesn't work.
        value = eclass.text_fileobj().read()
        ebp.write("transfer")
        ebp.write(value)

    if updates is not None:
        updates.add(line)


# TODO: move to base wrapped pkg class once they're reworked
def expected_ebuild_env(pkg, d=None, env_source_override=None, depends=False):
    """Setup expected ebuild vars.

    :param d: if None, generates a dict, else modifies a passed in mapping
    :return: mapping
    """
    if d is None:
        d = {}
    d["CATEGORY"] = pkg.category
    d["PF"] = pkg.PF
    d["P"] = pkg.P
    d["PN"] = pkg.PN
    d["PV"] = pkg.PV
    d["PR"] = pkg.PR
    d["PVR"] = pkg.PVR
    if env_source_override:
        path = env_source_override.path
        if path is not None:
            d["EBUILD"] = path
    else:
        if pkg.ebuild.path is not None:
            d["EBUILD"] = pkg.ebuild.path
        else:
            # binpkgs don't have ebuild paths
            d["EBUILD"] = ""

    # add EAPI specific settings
    d.update(pkg.eapi.ebd_env)

    if not depends:
        path = list(const.PATH_FORCED_PREPEND + pkg.eapi.helpers)
        path.extend(d.get("PATH", "").split(os.pathsep))
        path.extend(os.environ.get("PATH", "").split(os.pathsep))
        d["PATH"] = os.pathsep.join(filter(None, path))
        d["INHERITED"] = ' '.join(pkg.data.get("_eclasses_", ()))
        d["USE"] = ' '.join(sorted(str(x) for x in pkg.use))
        d["SLOT"] = pkg.fullslot

        # temp hack.
        for x in ('chost', 'cbuild', 'ctarget'):
            val = getattr(pkg, x)
            if val is not None:
                d[x.upper()] = val
        # special note... if CTARGET is the same as CHOST, suppress it.
        # certain ebuilds (nano for example) will misbehave w/ it.
        if pkg.ctarget is not None and pkg.ctarget == pkg.chost:
            d.pop("CTARGET")

    for key in e_const.PKGCORE_DEBUG_VARS:
        val = os.environ.get(key)
        if val is not None:
            d[key] = val
    return d
