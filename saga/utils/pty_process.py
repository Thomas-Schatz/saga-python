
__author__    = "Andre Merzky"
__copyright__ = "Copyright 2012-2013, The SAGA Project"
__license__   = "MIT"


import re
import os
import sys
import pty
import tty
import time
import errno
import shlex
import select
import signal
import termios
import threading

import saga.utils.logger as sul
import saga.exceptions   as se

# --------------------------------------------------------------------
#
_CHUNKSIZE = 1024  # default size of each read
_POLLDELAY = 0.01  # seconds in between read attempts
_DEBUG_MAX = 600


# --------------------------------------------------------------------
#
class PTYProcess (object) :
    """
    This class spawns a process, providing that child with pty I/O channels --
    it will maintain stdin, stdout and stderr channels to the child.  All
    write-like operations operate on the stdin, all read-like operations operate
    on the stdout stream.  Data from the stderr stream are at this point
    redirected to the stdout channel.

    Example::

        # run an interactive client process
        pty = PTYProcess ("/usr/bin/ssh -t localhost")

        # check client's I/O for one of the following patterns (prompts).  
        # Then search again.
        n, match = pty.find (['password\s*:\s*$', 
                              'want to continue connecting.*\(yes/no\)\s*$', 
                              '[\$#>]\s*$'])

        while True :

            if n == 0 :
                # found password prompt - tell the secret
                pty.write ("secret\\n")
                n, _ = pty.find (['password\s*:\s*$', 
                                  'want to continue connecting.*\(yes/no\)\s*$', 
                                  '[\$#>]\s*$'])
            elif n == 1 :
                # found request to accept host key - sure we do... (who checks
                # those keys anyways...?).  Then search again.
                pty.write ("yes\\n")
                n, _ = pty.find (['password\s*:\s*$', 
                                  'want to continue connecting.*\(yes/no\)\s*$', 
                                  '[\$#>]\s*$'])
            elif n == 2 :
                # found shell prompt!  Wohoo!
                break
        

        while True :
            # go full Dornroeschen (Sleeping Beauty)...
            pty.alive (recover=True) or break      # check / restart process
            pty.find  (['[\$#>]\s*$'])             # find shell prompt
            pty.write ("/bin/sleep "100 years"\\n") # sleep!  SLEEEP!

        # something bad happened
        print pty.autopsy ()

    """

    # ----------------------------------------------------------------
    #
    def __init__ (self, command, logger=None) :
        """
        The class constructor, which runs (execvpe) command in a separately
        forked process.  The bew process will inherit the environment of the
        application process.

        :type  command: string or list of strings
        :param command: The given command is what is run as a child, and
        fed/drained via pty pipes.  If given as string, command is split into an
        array of strings, using :func:`shlex.split`.

        :type  logger:  :class:`saga.utils.logger.Logger` instance
        :param logger:  logger stream to send status messages to.
        """

        self.logger = logger
        if  not  self.logger : self.logger = sul.getLogger ('PTYProcess') 
        self.logger.debug ("PTYProcess init %s" % self)


        if isinstance (command, basestring) :
            command = shlex.split (command)

        if not isinstance (command, list) :
            raise se.BadParameter ("PTYProcess expects string or list command")

        if len(command) < 1 :
            raise se.BadParameter ("PTYProcess expects non-empty command")

        self.rlock   = threading.RLock ()

        self.command = command # list of strings too run()


        self.cache   = ""      # data cache
        self.child   = None    # the process as created by subprocess.Popen
        self.ptyio   = None    # the process' io channel, from pty.fork()

        self.exit_code        = None  # child died with code (may be revived)
        self.exit_signal      = None  # child kill by signal (may be revived)

        self.recover_max      = 3  # TODO: make configure option.  This does not
        self.recover_attempts = 0  # apply for recovers triggered by gc_timeout!


        try :
            self.initialize ()

        except Exception as e :
            raise se.NoSuccess ("pty or process creation failed (%s)" % e)

    # --------------------------------------------------------------------
    #
    def __del__ (self) :
        """ 
        Need to free pty's on destruction, otherwise we might ran out of
        them (see cat /proc/sys/kernel/pty/max)
        """

        self.logger.debug ("PTYProcess del  %s" % self)
        with self.rlock :
    
            try :
                self.finalize ()
            except :
                pass
    

    # ----------------------------------------------------------------------
    #
    def initialize (self) :

        with self.rlock :

            # already initialized?
            if  self.child :
                self.logger.warn ("initialization race: %s" % ' '.join (self.command))
                return

    
            self.logger.info ("running: %s" % ' '.join (self.command))

            # create the child
            try :
                self.child, self.child_fd = pty.fork ()
            except Exception as e:
                raise se.NoSuccess ("Could not run (%s): %s" \
                                 % (' '.join (self.command), e))
            
            if  not self.child :
                # this is the child

                try :
                    # all I/O set up, have a pty (*fingers crossed*), lift-off!
                    os.execvpe (self.command[0], self.command, os.environ)

                except OSError as e:
                    self.logger.error ("Could not execute (%s): %s" \
                                    % (' '.join (self.command), e))
                    sys.exit (-1)

            else :
                # this is the parent
                new = termios.tcgetattr (self.child_fd)
                new[3] = new[3] & ~termios.ECHO

                termios.tcsetattr (self.child_fd, termios.TCSANOW, new)


                self.parent_in  = self.child_fd
                self.parent_out = self.child_fd


    # --------------------------------------------------------------------
    #
    def finalize (self, wstat=None) :
        """ kill the child, close all I/O channels """

        with self.rlock :

            # now we can safely kill the process -- unless some wait did that before
            if  wstat == None :

                if  self.child :
                    # yes, we have something to kill!
                    try :
                        os.kill (self.child, signal.SIGKILL)
                    except OSError :
                        pass

                    # hey, kiddo, how did that go?
                    while True :
                        try :
                            wpid, wstat = os.waitpid (self.child, 0)

                        except OSError as e :
                            # this should not have failed -- child disappeared?
                            self.exit_code   = None 
                            self.exit_signal = None
                            wstat            = None
                            break

                        if  wpid :
                            break

            # at this point, we declare the process to be gone for good
            self.child = None

            # lets see if we can perform some post-mortem analysis
            if  wstat != None :

                if  os.WIFEXITED (wstat) :
                    # child died of natural causes - perform autopsy...
                    self.exit_code   = os.WEXITSTATUS (wstat)
                    self.exit_signal = None

                elif os.WIFSIGNALED (wstat) :
                    # murder!! Child got killed by someone!  recover evidence...
                    self.exit_code   = None
                    self.exit_signal = os.WTERMSIG (wstat)


            try : 
                if  self.parent_out :
                    os.close (self.parent_out)
                    self.parent_out = None
            except OSError :
                pass

          # try : 
          #     if  self.parent_in :
          #         os.close (self.parent_in)
          #         self.parent_in = None
          # except OSError :
          #     pass

          # try : 
          #     os.close (self.parent_err) 
          # except OSError :
          #     pass


    # --------------------------------------------------------------------
    #
    def wait (self) :
        """ 
        blocks forever until the child finishes on its own, or is getting
        killed
        """

        # yes, for ever and ever...
        while True :

            if not self.child:
                # this was quick ;-)
                return

            # we need to lock, as the SIGCHLD will only arrive once
            with self.rlock :
                # hey, kiddo, whats up?
                try :
                    wpid, wstat = os.waitpid (self.child, 0)
                except OSError as e :

                    if e.errno == errno.ECHILD :

                        # child disappeared
                        self.exit_code   = None
                        self.exit_signal = None
                        self.finalize ()
                        return

                    # no idea what happened -- it is likely bad
                    raise se.NoSuccess ("waitpid failed: %s" % e)


                # did we get a note about child termination?
                if 0 == wpid :

                    # nope, all is well - carry on
                    continue


                # Yes, we got a note.  
                # Well, maybe the child fooled us and is just playing dead?
                if os.WIFSTOPPED   (wstat) or \
                   os.WIFCONTINUED (wstat)    :
                    # we don't care if someone stopped/resumed the child -- that is up
                    # to higher powers.  For our purposes, the child is alive.  Ha!
                    continue


                # not stopped, poor thing... - soooo, what happened??  But hey,
                # either way, its dead -- make sure it stays dead, to avoid
                # zombie apocalypse...
                self.child = None
                self.finalize (wstat=wstat)

                return


    # --------------------------------------------------------------------
    #
    def alive (self, recover=False) :
        """
        try to determine if the child process is still active.  If not, mark 
        the child as dead and close all IO descriptors etc ("func:`finalize`).

        If `recover` is `True` and the child is indeed dead, we attempt to
        re-initialize it (:func:`initialize`).  We only do that for so many
        times (`self.recover_max`) before giving up -- at that point it seems
        likely that the child exits due to a re-occurring operations condition.

        Note that upstream consumers of the :class:`PTYProcess` should be
        careful to only use `recover=True` when they can indeed handle
        a disconnected/reconnected client at that point, i.e. if there are no
        assumptions on persistent state beyond those in control of the upstream
        consumers themselves.
        """

        with self.rlock :

            # do we have a child which we can check?
            if  self.child :

                while True :
                    # print 'waitpid %s' % self.child
                    # hey, kiddo, whats up?
                    wpid, wstat = os.waitpid (self.child, os.WNOHANG)
                    # print 'waitpid %s : %s - %s' % (self.child, wpid, wstat)

                    # did we get a note about child termination?
                    if 0 == wpid :
                        # print 'waitpid %s : %s - %s -- none' % (self.child, wpid, wstat)
                        # nope, all is well - carry on
                        return True


                    # Yes, we got a note.  
                    # Well, maybe the child fooled us and is just playing dead?
                    if os.WIFSTOPPED   (wstat) or \
                       os.WIFCONTINUED (wstat)    :
                        # print 'waitpid %s : %s - %s -- stop/cont' % (self.child, wpid, wstat)
                        # we don't care if someone stopped/resumed the child -- that is up
                        # to higher powers.  For our purposes, the child is alive.  Ha!
                        continue

                    break

                # so its dead -- make sure it stays dead, to avoid zombie
                # apocalypse...
                self.child = None
                self.finalize (wstat=wstat)


            # check if we can attempt a post-mortem revival though
            if  not recover :
                # print 'not alive, not recover'
                # nope, we are on holy ground - revival not allowed.
                return False

            # we are allowed to revive!  So can we try one more time...  pleeeease??
            # (for cats, allow up to 9 attempts; for Buddhists, always allow to
            # reincarnate, etc.)
            if self.recover_attempts >= self.recover_max :
                # nope, its gone for good - just report the sad news
                # print 'not alive, no recover anymore'
                return False

            # MEDIIIIC!!!!
            self.recover_attempts += 1
            self.initialize ()

            # well, now we don't trust the child anymore, of course!  So we check
            # again.  Yes, this is recursive -- but note that recover_attempts get
            # incremented on every iteration, and this will eventually lead to
            # call termination (tm).
            # print 'alive, or not alive?  Check again!'
            return self.alive (recover=True)



    # --------------------------------------------------------------------
    #
    def autopsy (self) :
        """ 
        return diagnostics information string for dead child processes
        """

        with self.rlock :

            if  self.child :
                # Boooh!
                return "false alarm, process %s is alive!" % self.child

            ret  = ""
            ret += "  exit code  : %s\n" % self.exit_code
            ret += "  exit signal: %s\n" % self.exit_signal
            ret += "  last output: %s\n" % self.cache[-256:] # FIXME: smarter selection

            return ret


    # --------------------------------------------------------------------
    #
    def read (self, size=0, timeout=0, _force=False) :
        """ 
        read some data from the child.  By default, the method reads whatever is
        available on the next read, up to _CHUNKSIZE, but other read sizes can
        be specified.  
        
        The method will return whatever data it has at timeout::
        
          timeout == 0 : return the content of the first successful read, with
                         whatever data up to 'size' have been found.
          timeout <  0 : return after first read attempt, even if no data have 
                         been available.

        If no data are found, the method returns an empty string (not None).

        This method will not fill the cache, but will just read whatever data it
        needs (FIXME).

        Note: the returned lines do *not* get '\\\\r' stripped.
        """

        with self.rlock :

            found_eof = False

            if not self.alive (recover=False) :
                if self.cache :
                    raise se.NoSuccess ("process I/O failed: %s" % self.cache[-256:])
                else :
                    raise se.NoSuccess ("process I/O failed")

            try:
                # start the timeout timer right now.  Note that even if timeout is
                # short, and child.poll is slow, we will nevertheless attempt at least
                # one read...
                start = time.time ()
                ret   = ""

                # read until we have enough data, or hit timeout ceiling...
                while True :

                    # first, lets see if we still have data in the cache we can return
                    if len (self.cache) :

                        if not size :
                            ret = self.cache
                            self.cache = ""
                            return ret

                        # we don't even need all of the cache
                        elif size <= len (self.cache) :
                            ret = self.cache[:size]
                            self.cache = self.cache[size:]
                            return ret

                    # otherwise we need to read some more data, right?
                    # idle wait 'til the next data chunk arrives, or 'til _POLLDELAY
                    rlist, _, _ = select.select ([self.parent_out], [], [], _POLLDELAY)

                    # got some data? 
                    for f in rlist:
                        # read whatever we still need

                        readsize = _CHUNKSIZE
                        if size: 
                            readsize = size-len(ret)

                        buf  = os.read (f, _CHUNKSIZE)

                        if  len(buf) == 0 and sys.platform == 'darwin' :
                            self.logger.debug ("read : MacOS EOF")
                            self.finalize ()
                            found_eof = True
                            raise se.NoSuccess ("unexpected EOF (%s)" \
                                             % self.cache[-256:])


                        self.cache += buf.replace ('\r', '')
                        log         = buf.replace ('\r', '')
                        log         = log.replace ('\n', '\\n')
                      # print "buf: --%s--" % buf
                      # print "log: --%s--" % log
                        if  len(log) > _DEBUG_MAX :
                            self.logger.debug ("read : [%5d] [%5d] (%s ... %s)" \
                                            % (f, len(log), log[:30], log[-30:]))
                        else :
                            self.logger.debug ("read : [%5d] [%5d] (%s)" \
                                            % (f, len(log), log))


                    # lets see if we still got any data in the cache we can return
                    if len (self.cache) :

                        if not size :
                            ret = self.cache
                            self.cache = ""
                            return ret

                        # we don't even need all of the cache
                        elif size <= len (self.cache) :
                            ret = self.cache[:size]
                            self.cache = self.cache[size:]
                            return ret

                    # at this point, we do not have sufficient data -- only
                    # return on timeout

                    if  timeout == 0 : 
                        # only return if we have data
                        if len (self.cache) :
                            ret        = self.cache
                            self.cache = ""
                            return ret

                    elif timeout < 0 :
                        # return of we have data or not
                        ret        = self.cache
                        self.cache = ""
                        return ret

                    else : # timeout > 0
                        # return if timeout is reached
                        now = time.time ()
                        if (now-start) > timeout :
                            ret        = self.cache
                            self.cache = ""
                            return ret


            except Exception as e :

                if found_eof :
                    raise e

                raise se.NoSuccess ("read from process failed '%s' : (%s)" \
                                 % (e, self.cache[-256:]))


    # ----------------------------------------------------------------
    #
    def find (self, patterns, timeout=0) :
        """
        This methods reads bytes from the child process until a string matching
        any of the given patterns is found.  If that is found, all read data are
        returned as a string, up to (and including) the match.  Note that
        pattern can match an empty string, and the call then will return just
        that, an empty string.  If all patterns end with matching a newline,
        this method is effectively matching lines -- but note that '$' will also
        match the end of the (currently available) data stream.

        The call actually returns a tuple, containing the index of the matching
        pattern, and the string up to the match as described above.

        If no pattern is found before timeout, the call returns (None, None).
        Negative timeouts will block until a match is found

        Note that the pattern are interpreted with the re.M (multi-line) and
        re.S (dot matches all) regex flags.

        Performance: the call is doing repeated string regex searches over
        whatever data it finds.  On complex regexes, and large data, and small
        read buffers, this method can be expensive.  

        Note: the returned data get '\\\\r' stripped.
        """

        with self.rlock :

            try :
                start = time.time ()                       # startup timestamp
                ret   = []                                 # array of read lines
                patts = []                                 # compiled patterns
                data  = self.cache                         # initial data to check
                self.cache = ""

                if not data : # empty cache?
                    data = self.read (timeout=_POLLDELAY)

                # pre-compile the given pattern, to speed up matching
                for pattern in patterns :
                    patts.append (re.compile (pattern, re.MULTILINE | re.DOTALL))

                # we wait forever -- there are two ways out though: data matches
                # a pattern, or timeout passes
                while True :

                  # time.sleep (0.1)

                    # skip non-lines
                    if  None == data :
                        data += self.read (timeout=_POLLDELAY)

                    # check current data for any matching pattern
                  # print ">>%s<<" % data
                    for n in range (0, len(patts)) :

                        match = patts[n].search (data)
                      # print "==%s==" % patterns[n]

                        if match :
                            # a pattern matched the current data: return a tuple of
                            # pattern index and matching data.  The remainder of the
                            # data is cached.
                            ret  = data[0:match.end()]
                            self.cache = data[match.end():] 

                          # print "~~match!~~ %s" % data[match.start():match.end()]
                          # print "~~match!~~ %s" % (len(data))
                          # print "~~match!~~ %s" % (str(match.span()))
                          # print "~~match!~~ %s" % (ret)

                            return (n, ret.replace('\r', ''))

                    # if a timeout is given, and actually passed, return a non-match
                    if timeout == 0 :
                        return (None, None)

                    if timeout > 0 :
                        now = time.time ()
                        if (now-start) > timeout :
                            self.cache = data
                            return (None, None)

                    # no match yet, still time -- read more data
                    data += self.read (timeout=_POLLDELAY)


            except Exception as e :
                if  issubclass (e.__class__, se.SagaException) :
                    raise se.NoSuccess ("output parsing failed (%s): %s" % (e._plain_message, data))
                raise se.NoSuccess ("output parsing failed (%s): %s" % (e, data))


    # ----------------------------------------------------------------
    #
    def write (self, data) :
        """
        This method will repeatedly attempt to push the given data into the
        child's stdin pipe, until it succeeds to write all data.
        """

        with self.rlock :

            if not self.alive (recover=False) :
                raise se.NoSuccess ("cannot write to dead process (%s)" \
                                 % self.cache[-256:])

            try :

                log = data.replace ('\n', '\\n')
                log =  log.replace ('\r', '')
                if  len(log) > _DEBUG_MAX :
                    self.logger.debug ("write: [%5d] [%5d] (%s ... %s)" \
                                    % (self.parent_in, len(data), log[:30], log[-30:]))
                else :
                    self.logger.debug ("write: [%5d] [%5d] (%s)" \
                                    % (self.parent_in, len(data), log))

                # attempt to write forever -- until we succeeed
                while data :

                    # check if the pty pipe is ready for data
                    _, wlist, _ = select.select ([], [self.parent_in], [], _POLLDELAY)

                    for f in wlist :
                        
                        # write will report the number of written bytes
                        size = os.write (f, data)

                        # otherwise, truncate by written data, and try again
                        data = data[size:]

                        if data :
                            self.logger.info ("write: [%5d] [%5d]" % (f, size))


            except Exception as e :
                raise se.NoSuccess ("write to process failed (%s)" % e)


# vim: tabstop=8 expandtab shiftwidth=4 softtabstop=4

