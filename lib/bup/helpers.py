"""Helper functions and classes for bup."""
import sys, os, pwd, subprocess, errno, socket, select, mmap, stat, re
from bup import _version
import bup._helpers as _helpers

# This function should really be in helpers, not in bup.options.  But we
# want options.py to be standalone so people can include it in other projects.
from bup.options import _tty_width
tty_width = _tty_width


def atoi(s):
    """Convert the string 's' to an integer. Return 0 if s is not a number."""
    try:
        return int(s or '0')
    except ValueError:
        return 0


def atof(s):
    """Convert the string 's' to a float. Return 0 if s is not a number."""
    try:
        return float(s or '0')
    except ValueError:
        return 0


buglvl = atoi(os.environ.get('BUP_DEBUG', 0))


# Write (blockingly) to sockets that may or may not be in blocking mode.
# We need this because our stderr is sometimes eaten by subprocesses
# (probably ssh) that sometimes make it nonblocking, if only temporarily,
# leading to race conditions.  Ick.  We'll do it the hard way.
def _hard_write(fd, buf):
    while buf:
        (r,w,x) = select.select([], [fd], [], None)
        if not w:
            raise IOError('select(fd) returned without being writable')
        try:
            sz = os.write(fd, buf)
        except OSError, e:
            if e.errno != errno.EAGAIN:
                raise
        assert(sz >= 0)
        buf = buf[sz:]

def log(s):
    """Print a log message to stderr."""
    sys.stdout.flush()
    _hard_write(sys.stderr.fileno(), s)


def debug1(s):
    if buglvl >= 1:
        log(s)


def debug2(s):
    if buglvl >= 2:
        log(s)


def mkdirp(d, mode=None):
    """Recursively create directories on path 'd'.

    Unlike os.makedirs(), it doesn't raise an exception if the last element of
    the path already exists.
    """
    try:
        if mode:
            os.makedirs(d, mode)
        else:
            os.makedirs(d)
    except OSError, e:
        if e.errno == errno.EEXIST:
            pass
        else:
            raise


def next(it):
    """Get the next item from an iterator, None if we reached the end."""
    try:
        return it.next()
    except StopIteration:
        return None


def unlink(f):
    """Delete a file at path 'f' if it currently exists.

    Unlike os.unlink(), does not throw an exception if the file didn't already
    exist.
    """
    try:
        os.unlink(f)
    except OSError, e:
        if e.errno == errno.ENOENT:
            pass  # it doesn't exist, that's what you asked for


def readpipe(argv):
    """Run a subprocess and return its output."""
    p = subprocess.Popen(argv, stdout=subprocess.PIPE)
    r = p.stdout.read()
    p.wait()
    return r


def realpath(p):
    """Get the absolute path of a file.

    Behaves like os.path.realpath, but doesn't follow a symlink for the last
    element. (ie. if 'p' itself is a symlink, this one won't follow it, but it
    will follow symlinks in p's directory)
    """
    try:
        st = os.lstat(p)
    except OSError:
        st = None
    if st and stat.S_ISLNK(st.st_mode):
        (dir, name) = os.path.split(p)
        dir = os.path.realpath(dir)
        out = os.path.join(dir, name)
    else:
        out = os.path.realpath(p)
    #log('realpathing:%r,%r\n' % (p, out))
    return out


def detect_fakeroot():
    "Return True if we appear to be running under fakeroot."
    return os.getenv("FAKEROOTKEY") != None


_username = None
def username():
    """Get the user's login name."""
    global _username
    if not _username:
        uid = os.getuid()
        try:
            _username = pwd.getpwuid(uid)[0]
        except KeyError:
            _username = 'user%d' % uid
    return _username


_userfullname = None
def userfullname():
    """Get the user's full name."""
    global _userfullname
    if not _userfullname:
        uid = os.getuid()
        try:
            _userfullname = pwd.getpwuid(uid)[4].split(',')[0]
        except KeyError:
            _userfullname = 'user%d' % uid
    return _userfullname


_hostname = None
def hostname():
    """Get the FQDN of this machine."""
    global _hostname
    if not _hostname:
        _hostname = socket.getfqdn()
    return _hostname


_resource_path = None
def resource_path(subdir=''):
    global _resource_path
    if not _resource_path:
        _resource_path = os.environ.get('BUP_RESOURCE_PATH') or '.'
    return os.path.join(_resource_path, subdir)

class NotOk(Exception):
    pass

class Conn:
    """A helper class for bup's client-server protocol."""
    def __init__(self, inp, outp):
        self.inp = inp
        self.outp = outp

    def read(self, size):
        """Read 'size' bytes from input stream."""
        self.outp.flush()
        return self.inp.read(size)

    def readline(self):
        """Read from input stream until a newline is found."""
        self.outp.flush()
        return self.inp.readline()

    def write(self, data):
        """Write 'data' to output stream."""
        #log('%d writing: %d bytes\n' % (os.getpid(), len(data)))
        self.outp.write(data)

    def has_input(self):
        """Return true if input stream is readable."""
        [rl, wl, xl] = select.select([self.inp.fileno()], [], [], 0)
        if rl:
            assert(rl[0] == self.inp.fileno())
            return True
        else:
            return None

    def ok(self):
        """Indicate end of output from last sent command."""
        self.write('\nok\n')

    def error(self, s):
        """Indicate server error to the client."""
        s = re.sub(r'\s+', ' ', str(s))
        self.write('\nerror %s\n' % s)

    def _check_ok(self, onempty):
        self.outp.flush()
        rl = ''
        for rl in linereader(self.inp):
            #log('%d got line: %r\n' % (os.getpid(), rl))
            if not rl:  # empty line
                continue
            elif rl == 'ok':
                return None
            elif rl.startswith('error '):
                #log('client: error: %s\n' % rl[6:])
                return NotOk(rl[6:])
            else:
                onempty(rl)
        raise Exception('server exited unexpectedly; see errors above')

    def drain_and_check_ok(self):
        """Remove all data for the current command from input stream."""
        def onempty(rl):
            pass
        return self._check_ok(onempty)

    def check_ok(self):
        """Verify that server action completed successfully."""
        def onempty(rl):
            raise Exception('expected "ok", got %r' % rl)
        return self._check_ok(onempty)


def linereader(f):
    """Generate a list of input lines from 'f' without terminating newlines."""
    while 1:
        line = f.readline()
        if not line:
            break
        yield line[:-1]


def chunkyreader(f, count = None):
    """Generate a list of chunks of data read from 'f'.

    If count is None, read until EOF is reached.

    If count is a positive integer, read 'count' bytes from 'f'. If EOF is
    reached while reading, raise IOError.
    """
    if count != None:
        while count > 0:
            b = f.read(min(count, 65536))
            if not b:
                raise IOError('EOF with %d bytes remaining' % count)
            yield b
            count -= len(b)
    else:
        while 1:
            b = f.read(65536)
            if not b: break
            yield b


def slashappend(s):
    """Append "/" to 's' if it doesn't aleady end in "/"."""
    if s and not s.endswith('/'):
        return s + '/'
    else:
        return s


def _mmap_do(f, sz, flags, prot):
    if not sz:
        st = os.fstat(f.fileno())
        sz = st.st_size
    map = mmap.mmap(f.fileno(), sz, flags, prot)
    f.close()  # map will persist beyond file close
    return map


def mmap_read(f, sz = 0):
    """Create a read-only memory mapped region on file 'f'.

    If sz is 0, the region will cover the entire file.
    """
    return _mmap_do(f, sz, mmap.MAP_PRIVATE, mmap.PROT_READ)


def mmap_readwrite(f, sz = 0):
    """Create a read-write memory mapped region on file 'f'.

    If sz is 0, the region will cover the entire file.
    """
    return _mmap_do(f, sz, mmap.MAP_SHARED, mmap.PROT_READ|mmap.PROT_WRITE)


def parse_num(s):
    """Parse data size information into a float number.

    Here are some examples of conversions:
        199.2k means 203981 bytes
        1GB means 1073741824 bytes
        2.1 tb means 2199023255552 bytes
    """
    g = re.match(r'([-+\d.e]+)\s*(\w*)', str(s))
    if not g:
        raise ValueError("can't parse %r as a number" % s)
    (val, unit) = g.groups()
    num = float(val)
    unit = unit.lower()
    if unit in ['t', 'tb']:
        mult = 1024*1024*1024*1024
    elif unit in ['g', 'gb']:
        mult = 1024*1024*1024
    elif unit in ['m', 'mb']:
        mult = 1024*1024
    elif unit in ['k', 'kb']:
        mult = 1024
    elif unit in ['', 'b']:
        mult = 1
    else:
        raise ValueError("invalid unit %r in number %r" % (unit, s))
    return int(num*mult)


def count(l):
    """Count the number of elements in an iterator. (consumes the iterator)"""
    return reduce(lambda x,y: x+1, l)


saved_errors = []
def add_error(e):
    """Append an error message to the list of saved errors.

    Once processing is able to stop and output the errors, the saved errors are
    accessible in the module variable helpers.saved_errors.
    """
    saved_errors.append(e)
    log('%-70s\n' % e)

istty = os.isatty(2) or atoi(os.environ.get('BUP_FORCE_TTY'))
def progress(s):
    """Calls log(s) if stderr is a TTY.  Does nothing otherwise."""
    if istty:
        log(s)


def handle_ctrl_c():
    """Replace the default exception handler for KeyboardInterrupt (Ctrl-C).

    The new exception handler will make sure that bup will exit without an ugly
    stacktrace when Ctrl-C is hit.
    """
    oldhook = sys.excepthook
    def newhook(exctype, value, traceback):
        if exctype == KeyboardInterrupt:
            log('Interrupted.\n')
        else:
            return oldhook(exctype, value, traceback)
    sys.excepthook = newhook


def columnate(l, prefix):
    """Format elements of 'l' in columns with 'prefix' leading each line.

    The number of columns is determined automatically based on the string
    lengths.
    """
    if not l:
        return ""
    l = l[:]
    clen = max(len(s) for s in l)
    ncols = (tty_width() - len(prefix)) / (clen + 2)
    if ncols <= 1:
        ncols = 1
        clen = 0
    cols = []
    while len(l) % ncols:
        l.append('')
    rows = len(l)/ncols
    for s in range(0, len(l), rows):
        cols.append(l[s:s+rows])
    out = ''
    for row in zip(*cols):
        out += prefix + ''.join(('%-*s' % (clen+2, s)) for s in row) + '\n'
    return out

def parse_date_or_fatal(str, fatal):
    """Parses the given date or calls Option.fatal().
    For now we expect a string that contains a float."""
    try:
        date = atof(str)
    except ValueError, e:
        raise fatal('invalid date format (should be a float): %r' % e)
    else:
        return date


class FSTime():
    # Class to represent filesystem timestamps.  Use integer
    # nanoseconds on platforms where we have the higher resolution
    # lstat.  Use the native python stat representation (floating
    # point seconds) otherwise.

    def __cmp__(self, x):
        return self._value.__cmp__(x._value)

    def to_timespec(self):
        """Return (s, ns) where ns is always non-negative
        and t = s + ns / 10e8""" # metadata record rep (and libc rep)
        s_ns = self.secs_nsecs()
        if s_ns[0] > 0 or s_ns[1] >= 0:
            return s_ns
        return (s_ns[0] - 1, 10**9 + s_ns[1]) # ns is negative

    if _helpers.lstat: # Use integer nanoseconds.

        @staticmethod
        def from_secs(secs):
            ts = FSTime()
            ts._value = int(secs * 10**9)
            return ts

        @staticmethod
        def from_timespec(timespec):
            ts = FSTime()
            ts._value = timespec[0] * 10**9 + timespec[1]
            return ts

        @staticmethod
        def from_stat_time(stat_time):
            return FSTime.from_timespec(stat_time)

        def approx_secs(self):
            return self._value / 10e8;

        def secs_nsecs(self):
            "Return a (s, ns) pair: -1.5s -> (-1, -10**9 / 2)."
            if self._value >= 0:
                return (self._value / 10**9, self._value % 10**9)
            abs_val = -self._value
            return (- (abs_val / 10**9), - (abs_val % 10**9))

    else: # Use python default floating-point seconds.

        @staticmethod
        def from_secs(secs):
            ts = FSTime()
            ts._value = secs
            return ts

        @staticmethod
        def from_timespec(timespec):
            ts = FSTime()
            ts._value = timespec[0] + (timespec[1] / 10e8)
            return ts

        @staticmethod
        def from_stat_time(stat_time):
            ts = FSTime()
            ts._value = stat_time
            return ts

        def approx_secs(self):
            return self._value

        def secs_nsecs(self):
            "Return a (s, ns) pair: -1.5s -> (-1, -5**9)."
            x = math.modf(self._value)
            return (x[1], x[0] * 10**9)


def lutime(path, times):
    if _helpers.utimensat:
        atime = times[0].to_timespec()
        mtime = times[1].to_timespec()
        return _helpers.utimensat(_helpers.AT_FDCWD, path, (atime, mtime),
                                  _helpers.AT_SYMLINK_NOFOLLOW)
    else:
        return None


def utime(path, times):
    if _helpers.utimensat:
        atime = times[0].to_timespec()
        mtime = times[1].to_timespec()
        return _helpers.utimensat(_helpers.AT_FDCWD, path, (atime, mtime), 0)
    else:
        atime = times[0].approx_secs()
        mtime = times[1].approx_secs()
        os.utime(path, (atime, mtime))


class stat_result():

    @staticmethod
    def from_stat_rep(st):
        result = stat_result()
        if _helpers._have_ns_fs_timestamps:
            (result.st_mode,
             result.st_ino,
             result.st_dev,
             result.st_nlink,
             result.st_uid,
             result.st_gid,
             result.st_rdev,
             result.st_size,
             atime,
             mtime,
             ctime) = st
        else:
            result.st_mode = st.st_mode
            result.st_ino = st.st_ino
            result.st_dev = st.st_dev
            result.st_nlink = st.st_nlink
            result.st_uid = st.st_uid
            result.st_gid = st.st_gid
            result.st_rdev = st.st_rdev
            result.st_size = st.st_size
            atime = FSTime.from_stat_time(st.st_atime)
            mtime = FSTime.from_stat_time(st.st_mtime)
            ctime = FSTime.from_stat_time(st.st_ctime)
        result.st_atime = FSTime.from_stat_time(atime)
        result.st_mtime = FSTime.from_stat_time(mtime)
        result.st_ctime = FSTime.from_stat_time(ctime)
        return result


def fstat(path):
    if _helpers.fstat:
        st = _helpers.fstat(path)
    else:
        st = os.fstat(path)
    return stat_result.from_stat_rep(st)


def lstat(path):
    if _helpers.lstat:
        st = _helpers.lstat(path)
    else:
        st = os.lstat(path)
    return stat_result.from_stat_rep(st)


# hashlib is only available in python 2.5 or higher, but the 'sha' module
# produces a DeprecationWarning in python 2.6 or higher.  We want to support
# python 2.4 and above without any stupid warnings, so let's try using hashlib
# first, and downgrade if it fails.
try:
    import hashlib
except ImportError:
    import sha
    Sha1 = sha.sha
else:
    Sha1 = hashlib.sha1


def version_date():
    """Format bup's version date string for output."""
    return _version.DATE.split(' ')[0]

def version_commit():
    """Get the commit hash of bup's current version."""
    return _version.COMMIT

def version_tag():
    """Format bup's version tag (the official version number).

    When generated from a commit other than one pointed to with a tag, the
    returned string will be "unknown-" followed by the first seven positions of
    the commit hash.
    """
    names = _version.NAMES.strip()
    assert(names[0] == '(')
    assert(names[-1] == ')')
    names = names[1:-1]
    l = [n.strip() for n in names.split(',')]
    for n in l:
        if n.startswith('tag: bup-'):
            return n[9:]
    return 'unknown-%s' % _version.COMMIT[:7]
