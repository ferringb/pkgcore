# Copyright: 2004-2011 Brian Harring <ferringb@gmail.com> (BSD/GPL2)
# Copyright: 2005-2006 Jason Stubbs <jstubbs@gmail.com> (BSD/GPL2)
# Copyright: 2004-2005 Gentoo Foundation
# License: GPL2


"""
subprocess related functionality
"""

__all__ = [
    "cleanup_pids", "spawn", "spawn_sandbox", "spawn_bash", "spawn_fakeroot",
    "spawn_get_output",
]

import atexit
import itertools
import os
import signal
import sys

from snakeoil.demandload import demandload
from snakeoil.mappings import ProtectedDict
from snakeoil.osutils import access
from snakeoil.process import find_binary, CommandNotFound, closerange

from pkgcore.const import (
    BASH_BINARY, SANDBOX_BINARY, FAKED_PATH, LIBFAKEROOT_PATH)

demandload(
    'pkgcore.log:logger',
)

try:
    import resource
    max_fd_limit = resource.getrlimit(resource.RLIMIT_NOFILE)[0]
except ImportError:
    max_fd_limit = 256


def spawn_bash(mycommand, debug=False, name=None, **keywords):
    """spawn the command via bash -c"""

    args = [BASH_BINARY, '--norc', '--noprofile']
    if debug:
        # Print commands and their arguments as they are executed.
        args.append("-x")
    args.append("-c")
    if isinstance(mycommand, str):
        args.append(mycommand)
    else:
        args.extend(mycommand)
    if name is None:
        name = os.path.basename(args[3])
    return spawn(args, name=name, **keywords)

def spawn_sandbox(mycommand, name=None, **keywords):
    """spawn the command under sandboxed"""

    if not is_sandbox_capable():
        return spawn_bash(mycommand, name=name, **keywords)
    args = [SANDBOX_BINARY]
    if isinstance(mycommand, str):
        args.extend(mycommand.split())
    else:
        args.extend(mycommand)
    if name is None:
        name = os.path.basename(args[1])
    return spawn(args, name=name, **keywords)

_exithandlers = []
def atexit_register(func, *args, **kargs):
    """Wrapper around atexit.register that is needed in order to track
    what is registered.  For example, when portage restarts itself via
    os.execv, the atexit module does not work so we have to do it
    manually by calling the run_exitfuncs() function in this module."""
    _exithandlers.append((func, args, kargs))

def run_exitfuncs():
    """This should behave identically to the routine performed by
    the atexit module at exit time.  It's only necessary to call this
    function when atexit will not work (because of os.execv, for
    example)."""

    exc_info = None
    while _exithandlers:
        func, targs, kargs = _exithandlers.pop()
        try:
            func(*targs, **kargs)
        except SystemExit:
            exc_info = sys.exc_info()
        except:
            exc_info = sys.exc_info()

    if exc_info is not None:
        raise exc_info[0], exc_info[1], exc_info[2]

atexit.register(run_exitfuncs)

# We need to make sure that any processes spawned are killed off when
# we exit. spawn() takes care of adding and removing pids to this list
# as it creates and cleans up processes.
spawned_pids = []
def cleanup_pids(pids=None):
    """reap list of pids if specified, else all children"""

    global spawned_pids
    if pids is None:
        pids = spawned_pids
    elif pids is not spawned_pids:
        pids = list(pids)

    while pids:
        pid = pids.pop()
        try:
            if os.waitpid(pid, os.WNOHANG) == (0, 0):
                os.kill(pid, signal.SIGTERM)
                os.waitpid(pid, 0)
        except OSError:
            # This pid has been cleaned up outside
            # of spawn().
            pass

        if spawned_pids is not pids:
            try:
                spawned_pids.remove(pid)
            except ValueError:
                pass

def spawn(mycommand, env=None, name=None, fd_pipes=None, returnpid=False,
          uid=None, gid=None, groups=None, umask=None, cwd=None, pgid=None):

    """wrapper around execve

    :type mycommand: list or string
    :type env: mapping with string keys and values
    :param name: controls what the process is named
        (what it would show up as under top for example)
    :type fd_pipes: mapping from existing fd to fd (inside the new process)
    :param fd_pipes: controls what fd's are left open in the spawned process-
    :param returnpid: controls whether spawn waits for the process to finish,
        or returns the pid.
    """
    # mycommand is either a str or a list
    if isinstance(mycommand, str):
        mycommand = mycommand.split()

    # If an absolute path to an name file isn't given
    # search for it unless we've been told not to.
    binary = find_binary(mycommand[0])

    # mypids will hold the pids of all processes created.
    mypids = []

    pid = os.fork()

    if not pid:
        # 'Catch "Exception"'
        # pylint: disable-msg=W0703
        try:
            _exec(binary, mycommand, name, fd_pipes, env, gid, groups,
                  uid, umask, cwd, pgid)
        except Exception as e:
            # We need to catch _any_ exception so that it doesn't
            # propogate out of this function and cause exiting
            # with anything other than os._exit()
            sys.stderr.write("%s:\n   %s\n" % (e, " ".join(mycommand)))
            os._exit(1)

    # Add the pid to our local and the global pid lists.
    mypids.append(pid)
    spawned_pids.append(pid)

    # If the caller wants to handle cleaning up the processes, we tell
    # it about all processes that were created.
    if returnpid:
        return mypids

    try:
        # Otherwise we clean them up.
        while mypids:

            # Pull the last reader in the pipe chain. If all processes
            # in the pipe are well behaved, it will die when the process
            # it is reading from dies.
            pid = mypids.pop(0)

            # and wait for it.
            retval = os.waitpid(pid, 0)[1]

            # When it's done, we can remove it from the
            # global pid list as well.
            spawned_pids.remove(pid)

            if retval:
                # If it failed, kill off anything else that
                # isn't dead yet.
                for pid in mypids:
                    if os.waitpid(pid, os.WNOHANG) == (0, 0):
                        os.kill(pid, signal.SIGTERM)
                        os.waitpid(pid, 0)
                    spawned_pids.remove(pid)

                return process_exit_code(retval)
    finally:
        cleanup_pids(mypids)

    # Everything succeeded
    return 0

def _exec(binary, mycommand, name=None, fd_pipes=None, env=None, gid=None,
          groups=None, uid=None, umask=None, cwd=None, pgid=None):
    """internal function to handle exec'ing the child process.

    If it succeeds this function does not return. It might raise an
    exception, and since this runs after fork calling code needs to
    make sure this is caught and os._exit is called if it does (or
    atexit handlers run twice).
    """
    if env is None:
        env = {}

    logger.debug(
        'executing %s%s: %s%s',
        binary,
        ' in %s' % cwd.rstrip('/') if cwd else '',
        ' '.join('%s="%s"' % (k, v) for k, v in env.iteritems()),
        ' ' + ' '.join(mycommand))

    # If the process we're creating hasn't been given a name
    # assign it the name of the binary.
    if name is None:
        name = os.path.basename(binary)

    # Set up the command's argument list.
    myargs = [name]
    myargs.extend(mycommand[1:])

    def _find_unused_pid(protected):
        for potential in itertools.count():
            if potential not in protected:
                protected.add(potential)
                yield potential

    # If we haven't been told what file descriptors to use
    # default to propogating our stdin, stdout and stderr.
    if fd_pipes is None:
        fd_pipes = {0: 0, 1: 1, 2: 2}

    # Set up the command's pipes.
    my_fds = {}

    # To protect from cases where direct assignment could
    # clobber needed fds ({1:2, 2:1}) we first dupe the fds
    # into unused fds.
    protected = set(fd_pipes)
    protected.update(fd_pipes.itervalues())
    fd_source = _find_unused_pid(protected)

    for trg_fd, src_fd in fd_pipes.iteritems():
        if trg_fd != src_fd:
            if trg_fd not in protected:
                # Nothing is in the way; move it immediately.
                os.dup2(src_fd, trg_fd)
            else:
                x = my_fds[trg_fd] = fd_source.next()
                os.dup2(src_fd, x)

    # reassign whats required now.
    for trg_fd, src_fd in my_fds.iteritems():
        os.dup2(src_fd, trg_fd)

    # Then close _all_ fds that haven't been explicitly
    # requested to be kept open.
    last = 0
    for fd in sorted(fd_pipes):
        if fd != last:
            closerange(last, fd)
        last = fd + 1

    closerange(last, max_fd_limit)

    if cwd is not None:
        os.chdir(cwd)

    # Set requested process permissions.
    if gid is not None:
        os.setgid(gid)
    if groups is not None:
        os.setgroups(groups)
    if uid is not None:
        os.setuid(uid)
    if umask is not None:
        os.umask(umask)
    if pgid is not None:
        os.setpgid(0, pgid)

    # finally, we reset the signal handlers that python screws with back to defaults.
    # gentoo bug #309001, #289486
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    signal.signal(signal.SIGQUIT, signal.SIG_DFL)
    # unneeded, but being paranoid should spawn grow a spawn_func target again.
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)

    # And switch to the new process.
    os.execve(binary, myargs, env)


def spawn_fakeroot(mycommand, save_file, env=None, name=None,
                   returnpid=False, **keywords):
    """spawn a process via fakeroot

    refer to the fakeroot manpage for specifics of using fakeroot
    """
    if env is None:
        env = {}
    else:
        env = ProtectedDict(env)

    if name is None:
        name = "fakeroot %s" % mycommand

    args = [
        FAKED_PATH,
        "--unknown-is-real", "--foreground", "--save-file", save_file]

    rd_fd, wr_fd = os.pipe()
    daemon_fd_pipes = {1: wr_fd, 2: wr_fd}
    if os.path.exists(save_file):
        args.append("--load")
        daemon_fd_pipes[0] = os.open(save_file, os.O_RDONLY)
    else:
        daemon_fd_pipes[0] = os.open("/dev/null", os.O_RDONLY)

    pids = None
    pids = spawn(args, fd_pipes=daemon_fd_pipes, returnpid=True)
    try:
        try:
            rd_f = os.fdopen(rd_fd)
            line = rd_f.readline()
            rd_f.close()
            rd_fd = None
        except:
            cleanup_pids(pids)
            raise
    finally:
        for x in (rd_fd, wr_fd, daemon_fd_pipes[0]):
            if x is not None:
                try:
                    os.close(x)
                except OSError:
                    pass

    line = line.strip()

    try:
        fakekey, fakepid = map(int, line.split(":"))
    except ValueError:
        raise ExecutionFailure("output from faked was unparsable- %s" % line)

    # by now we have our very own daemonized faked.  yay.
    env["FAKEROOTKEY"] = str(fakekey)
    paths = [LIBFAKEROOT_PATH] + env.get("LD_PRELOAD", "").split(":")
    env["LD_PRELOAD"] = ":".join(x for x in paths if x)

    try:
        ret = spawn(
            mycommand, name=name, env=env, returnpid=returnpid,
            **keywords)
        if returnpid:
            return ret + [fakepid] + pids
        return ret
    finally:
        if not returnpid:
            cleanup_pids([fakepid] + pids)

def spawn_get_output(mycommand, spawn_type=None, raw_exit_code=False, collect_fds=(1,),
                     fd_pipes=None, split_lines=True, **keywords):

    """Call spawn, collecting the output to fd's specified in collect_fds list.

    :param spawn_type: the passed in function to call- typically :func:`spawn_bash`,
       :func:`spawn`, :func:`spawn_sandbox`, or :func:`spawn_fakeroot`.
       Defaults to :func:`spawn`.
    """

    if spawn_type is None:
        spawn_type = spawn

    pr, pw = None, None
    if fd_pipes is None:
        fd_pipes = {0: 0}
    else:
        fd_pipes = ProtectedDict(fd_pipes)
    try:
        pr, pw = os.pipe()
        for x in collect_fds:
            fd_pipes[x] = pw
        keywords["returnpid"] = True
        mypid = spawn_type(mycommand, fd_pipes=fd_pipes, **keywords)
        os.close(pw)
        pw = None

        if not isinstance(mypid, (list, tuple)):
            raise ExecutionFailure()

        fd = os.fdopen(pr, "r")
        try:
            if not split_lines:
                mydata = fd.read()
            else:
                mydata = fd.readlines()
        finally:
            fd.close()
            pw = None

        retval = os.waitpid(mypid[0], 0)[1]
        cleanup_pids(mypid)
        if raw_exit_code:
            return [retval, mydata]
        return [process_exit_code(retval), mydata]

    finally:
        if pr is not None:
            try:
                os.close(pr)
            except OSError:
                pass
        if pw is not None:
            try:
                os.close(pw)
            except OSError:
                pass

def process_exit_code(retval):
    """Process a waitpid returned exit code.

    :return: The exit code if it exit'd, the signal if it died from signalling.
    """
    # If it got a signal, return the signal that was sent.
    if retval & 0xff:
        return (retval & 0xff) << 8

    # Otherwise, return its exit code.
    return retval >> 8


class ExecutionFailure(Exception):
    def __init__(self, msg):
        Exception.__init__(self, msg)
        self.msg = msg

    def __str__(self):
        return "Execution Failure: %s" % self.msg

# cached capabilities

def is_fakeroot_capable(force=False):
    if not force:
        try:
            return is_fakeroot_capable.cached_result
        except AttributeError:
            pass
    if not (os.path.exists(FAKED_PATH) and os.path.exists(LIBFAKEROOT_PATH)):
        res = False
    else:
        try:
            r, s = spawn_get_output(["fakeroot", "--version"], fd_pipes={2: 1, 1: 1})
            res = (r == 0) and (len(s) == 1) and ("version 1." in s[0])
        except ExecutionFailure:
            res = False
    is_fakeroot_capable.cached_result = res
    return res

def is_sandbox_capable(force=False):
    if not force:
        try:
            return is_sandbox_capable.cached_result
        except AttributeError:
            pass
    if not (os.path.isfile(SANDBOX_BINARY) and access(SANDBOX_BINARY, os.X_OK)):
        res = False
    else:
        try:
            r, s = spawn_get_output([SANDBOX_BINARY, "--version"])
            res = (r == 0) and ("gentoo" in s[0].lower())
        except ExecutionFailure:
            res = False
    is_sandbox_capable.cached_result = res
    return res

def is_userpriv_capable(force=False):
    if not force:
        try:
            return is_userpriv_capable.cached_result
        except AttributeError:
            pass
    res = is_userpriv_capable.cached_result = (os.getuid() == 0)
    return res

_invoking_python = None

def find_invoking_python():
    # roughly... use sys.executable if possible, then major ver variations-
    # look for python2.5, python2, then just python, for example
    # NOTE: sys.executable in unreliable if the interpreter is embedded
    global _invoking_python
    if _invoking_python is not None and os.path.exists(_invoking_python):
        return _invoking_python
    if os.path.exists(sys.executable):
        test_input = "oh hai"
        returncode, output = spawn_get_output(
            [sys.executable, '-c', 'print("%s")' % test_input], collect_fds=(1, 2))
        if output and output[0].strip() == test_input:
            _invoking_python = sys.executable
            return _invoking_python

    chunks = list(str(x) for x in sys.version_info[:2])
    for potential in (chunks, chunks[:-1], ''):
        try:
            command_name = 'python%s' % '.'.join(potential)
            _invoking_python = find_binary(command_name)
            return _invoking_python
        except CommandNotFound:
            continue
    raise CommandNotFound('python')
