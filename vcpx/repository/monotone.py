# -*- mode: python; coding: utf-8 -*-
# :Progetto: vcpx -- Monotone details
# :Creato:   Tue Apr 12 01:28:10 CEST 2005
# :Autore:   Markus Schiltknecht <markus@bluegap.ch>
# :Autore:   Riccardo Ghetta <birrachiara@tin.it>
# :Licenza:  GNU General Public License
#

"""
This module contains supporting classes for Monotone.
"""

__docformat__ = 'reStructuredText'

from os.path import exists, join, isdir
from os import getenv
from string import whitespace

from vcpx.repository import Repository
from vcpx.shwrap import ExternalCommand, PIPE, ReopenableNamedTemporaryFile
from vcpx.source import UpdatableSourceWorkingDir, InvocationError, \
                        ChangesetApplicationFailure, GetUpstreamChangesetsFailure
from vcpx.target import SynchronizableTargetWorkingDir, TargetInitializationFailure
from vcpx.changes import Changeset
from vcpx.tzinfo import UTC


MONOTONERC = """\
function get_passphrase(KEYPAIR_ID)
  return "%s"
end
"""

class Unknown(object):
    '''
    Assign this class to variables whose value is unknown.
    Similar to the "None" object.
    '''

class MonotoneRepository(Repository):
    METADIR = '_MTN'

    def _load(self, project):
        Repository._load(self, project)
        cget = project.config.get
        self.EXECUTABLE = cget(self.name, 'monotone-command', 'mtn')
        self.keyid = cget(self.name, 'keyid') or \
                     cget(self.name, '%s-keyid' % self.which)
        self.passphrase = cget(self.name, 'passphrase') or \
                          cget(self.name, '%s-passphrase' % self.which)
        self.keygenid = cget(self.name, 'keygenid') or \
                        cget(self.name, '%s-keygenid' % self.which)
        self.custom_lua = cget(self.name, 'custom_lua') or \
                          cget(self.name, '%s-custom_lua' % self.which)
        self._supportsLogRevision = Unknown
        self._supportsLogNoGraph = Unknown

    def supportsLogRevision(self, working_dir, revision):
        """
        In Monotone 0.32, ``mtn log --revision`` was changed to ``mtn
        log --from``.  This function returns True if ``--revision``
        is supported or False otherwise.

        FIXME: To call this method, you have to actually pass working_dir
        and a revision to it.  Ideally, this is unnecessary and it is
        really just a hack.  Perhaps a better way to do this is to have a
        ``log`` method on the MonotoneRepository class.
        """

        # Lazily (upon first request) attempt to determine the value
        # of this variable:
        if self._supportsLogRevision is Unknown:
            cmd = self.command("log",
                               "--db", self.repository,
                               "--last", "1",
                               "--revision", revision)
            mtl = ExternalCommand(cwd=working_dir, command=cmd)
            outstr = mtl.execute(stdout=PIPE, stderr=PIPE)
            if mtl.exit_status:
                self._supportsLogRevision = False
            else:
                self._supportsLogRevision = True

        return self._supportsLogRevision

    def supportsLogNoGraph(self, working_dir, revision):
        """
        In Monotone 0.33, the log message format was changed and the
        ``mtn log --no-graph`` option was added to revert to the prior log
        format.  Since this new format is intended for pretty-printing,
        it is not very helpful when screen-scraping.

        FIXME: (see the FIXME in supportsLogRevision)
        """

        # Lazily (upon first request) attempt to determine the value
        # of this variable:
        if self._supportsLogNoGraph is Unknown:
            if not self.supportsLogRevision(working_dir, revision):
                cmd = self.command("log",
                                   "--db", self.repository,
                                   "--last", "1",
                                   "--from", revision,
                                   "--no-graph") # <- we're testing for this!
                mtl = ExternalCommand(cwd=working_dir, command=cmd)
                outstr = mtl.execute(stdout=PIPE, stderr=PIPE)
                if mtl.exit_status:
                    self._supportsLogNoGraph = False
                else:
                    self._supportsLogNoGraph = True
            else:
                self._supportsLogNoGraph = False

        return self._supportsLogNoGraph

    def create(self):
        """
        Create a new monotone DB, storing the commit keys, if available
        """

        if not self.repository or exists(self.repository):
            return

        cmd = self.command("db", "init", "--db", self.repository)
        init = ExternalCommand(command=cmd)
        init.execute(stdout=PIPE, stderr=PIPE)

        if init.exit_status:
            raise TargetInitializationFailure("Was not able to initialize "
                                              "the monotone db at %r" %
                                              self.repository)

        if self.keyid:
            self.log.info("Using key %s for commits" % (self.keyid,))
        else:
            # keystore key id unspecified, look at other options
            if self.keygenid:
                keyfile = join(getenv("HOME"), '.monotone', 'keys', self.keygenid)
                if exists(keyfile):
                    self.log.info("Key %s exist, don't genkey again" % self.keygenid)
                else:
                    # requested a new key
                    cmd = self.command("genkey", "--db", self.repository)
                    regkey = ExternalCommand(command=cmd)
                    if self.passphrase:
                        passp = "%s\n%s\n" % (self.passphrase, self.passphrase)
                    else:
                        passp = None
                    regkey.execute(self.keygenid, input=passp, stdout=PIPE, stderr=PIPE)
                    if regkey.exit_status:
                        raise TargetInitializationFailure("Was not able to setup "
                                                      "the monotone initial key at %r" %
                                                      self.repository)
            else:
                raise TargetInitializationFailure("Can't setup the monotone "
                                                  "repository %r. "
                                                  "A keyid or keygenid "
                                                  "must be provided." %
                                                  self.repository)



class ExternalCommandChain:
    """
    This class implements command piping, i.e. a chain of
    ExternalCommand, each feeding its stdout to the stdin of next
    command in the chain. If a command fails, the chain breaks and
    returns error.

    Note:
    This class implements only a subset of ExternalCommand functionality
    """
    def __init__(self, command, cwd=None):
        self.commandchain =command
        self.cwd = cwd
        self.exit_status = 0

    def execute(self):
        outstr = None
        for cmd in self.commandchain:
            input = outstr
            exc = ExternalCommand(cwd=self.cwd, command=cmd)
            out, err = exc.execute(input=input, stdout=PIPE, stderr=PIPE)
            self.exit_status = exc.exit_status
            if self.exit_status:
                break
            outstr = out.getvalue()
	    if len(outstr) <= 0:
		break
        return out, err


class MonotoneChangeset(Changeset):
    """
    Monotone changesets differ from standard Changeset because:

    1. only the "revision" field is used for eq/ne comparison
    2. have additional properties used to handle history linearization
    """

    def __init__(self, linearized_ancestor, revision):
        """
        Initializes a new MonotoneChangeset. The linearized_ancestor
        parameters is the fake ancestor used for linearization. The
        very first revision tailorized has lin_ancestor==None
        """

        Changeset.__init__(self, revision=revision, date=None, author=None, log="")
        self.lin_ancestor = linearized_ancestor
        self.real_ancestors = None

    def __eq__(self, other):
        return (self.revision == other.revision)

    def __ne__(self, other):
        return (self.revision <> other.revision)

    def __str__(self):
        s = [Changeset.__str__(self)]
        s.append('linearized ancestor: %s' % self.lin_ancestor)
        s.append('real ancestor(s): %s' %
                 (self.real_ancestors and ','.join(self.real_ancestors)
                  or 'None'))
        return '\n'.join(s)

    def update(self, real_dates, authors, log, real_ancestors, branches):
        """
        Updates the monotone changeset secondary data
        """
        self.author=".".join(authors)
        self.setLog(log)
        self.date = real_dates[0]
        self.real_dates = real_dates
        self.real_ancestors = real_ancestors
        self.branches = branches


class MonotoneLogParser:
    """
    Obtain and parse a *single* "mtn log" output, reconstructing
    the revision information
    """

    class PrefixRemover:
        """
        Helper class. Matches a prefix, allowing access to the text following
        """
        def __init__(self, str):
            self.str = str
            self.value=""

        def __call__(self, prefix):
            if self.str.startswith(prefix):
                self.value = self.str[len(prefix):].strip()
                return True
            else:
                return False

    # logfile states
    SINGLE = 0  # single line state
    ADD = 1 # in add file/dir listing
    MOD = 2 # in mod file/dir listing
    DEL = 3 # in delete file/dir listing
    REN = 4 # in renamed file/dir listing
    LOG = 5 # in changelog listing
    CMT = 6 # in comment listing

    def __init__(self, repository, working_dir):
        self.working_dir = working_dir
        self.repository = repository

    def parse(self, revision):
        from datetime import datetime

        self.revision=""
        self.ancestors=[]
        self.authors=[]
        self.dates=[]
        self.changelog=""
        self.branches=[]

        cmd = None

        logRevision = self.repository.supportsLogRevision(self.working_dir,
                                                          revision)
        logNoGraph = self.repository.supportsLogNoGraph(self.working_dir,
                                                        revision)
        if logRevision: # <= Montone-0.31
            cmd = self.repository.command("log",
                                          "--db", self.repository.repository,
                                          "--last", "1",
                                          "--revision", revision)
        elif not logRevision and not logNoGraph: # Monotone-0.32
            cmd = self.repository.command("log",
                                          "--db", self.repository.repository,
                                          "--last", "1",
                                          "--from", revision)
        elif not logRevision and logNoGraph: # Monotone-0.33
            cmd = self.repository.command("log",
                                          "--db", self.repository.repository,
                                          "--last", "1",
                                          "--from", revision,
                                          "--no-graph")
        mtl = ExternalCommand(cwd=self.working_dir, command=cmd)
        outstr = mtl.execute(stdout=PIPE, stderr=PIPE)
        if mtl.exit_status:
            raise GetUpstreamChangesetsFailure("mtn log returned status %d" % mtl.exit_status)

        logs = ""
        comments = ""
        state = self.SINGLE
        loglines = outstr[0].getvalue().splitlines()
        for curline in loglines:

            pr = self.PrefixRemover(curline)
            if pr("Revision:"):
                if pr.value != revision:
                    raise GetUpstreamChangesetsFailure(
                        "Revision doesn't match. Expected %s, found %s" % (revision, pr.value))
                state = self.SINGLE
            elif pr("Ancestor:"):
                if pr.value:
                    self.ancestors.append(pr.value) # cset could be a merge and have multiple ancestors
                state = self.SINGLE
            elif pr("Author:"):
                self.authors.append(pr.value)
                state = self.SINGLE
            elif pr("Date:"):
                    # monotone dates are expressed in ISO8601, always UTC
                    dateparts = pr.value.split('T')
                    assert len(dateparts) >= 2, `dateparts`
                    day = dateparts[0]
                    time = dateparts[1]
                    y,m,d = map(int, day.split(day[4]))
                    hh,mm,ss = map(int, time.split(':'))
                    date = datetime(y,m,d,hh,mm,ss,0,UTC)
                    self.dates.append(date)
                    state = self.SINGLE
            elif pr("Branch:"):
                # branch data
                self.branches.append(pr.value)
                state = self.SINGLE
            elif pr("Tag"):
                # unused data, just resetting state
                state = self.SINGLE
            elif pr("Deleted files:") or pr("Deleted directories:") or pr("Deleted entries"):
                state=self.DEL
            elif pr("Renamed files:") or pr("Renamed directories:") or pr("Renamed entries"):
                state=self.REN
            elif pr("Added files:") or pr("Added directories:"):
                state=self.ADD
            elif pr("Modified files:"):
                state=self.ADD
            elif pr("ChangeLog:"):
                state=self.LOG
            elif pr("Comments:"):
                comments=comments + "Note:\n"
                state=self.CMT
            else:
                # otherwise, it must be a log/comment/changeset entry, or an unknown cert line
                if state == self.SINGLE:
                    # line coming from an unknown cert
                    pass
                elif state == self.LOG:
                    # log line, accumulate string
                    logs = logs + curline + "\n"
                elif state == self.CMT:
                    # comment line, accumulate string
                    comments = comments + curline + "\n"
                else:
                    # parse_cset_entry(mode, chset, curline.strip()) # cset entry, handle
                    pass # we ignore cset info

        # parsing terminated, verify the data
        if len(self.authors)<1 or len(self.dates)<1 or revision=="":
            raise GetUpstreamChangesetsFailure("Error parsing log of revision %s. Missing data" % revision)
        self.changelog = logs + comments

    def convertLog(self, chset):
        self.parse(chset.revision)

        chset.update(real_dates=self.dates,
                     authors=self.authors,
                     log=self.changelog,
                     real_ancestors=self.ancestors,
                     branches=self.branches)

        return chset


class MonotoneDiffParser:
    """
    This class obtains a diff beetween two arbitrary revisions, parsing
    it to get changeset entries.

    Note: since monotone tracks directories implicitly, a fake "add dir"
    cset entry is generated when a file is added to a subdir
    """

    class BasicIOTokenizer:
        # To write its control files, monotone uses a format called
        # internally "basic IO", a stanza file format with items
        # separated by blank lines. Lines are terminated by newlines.
        # The format supports strings, sequence of chars contained by
        # ". String could contain newlines and to insert a " in the
        # middle you escape it with \ (and \\ is used to obtain the \
        # char itself) basic IO files are always UTF-8
        # This class implements a small tokenizer for basic IO

        def __init__(self, stream):
            self.stream = stream

        def _string_token(self):
            # called at start of string, returns the complete string
            # Note: Exceptions checked outside
            escape = False
            str=['"']
            while True:
                ch = self.it.next()
                if escape:
                    escape=False
                    str.append(ch)
                    continue
                elif ch=='\\':
                    escape=True
                    continue
                else:
                    str.append(ch)
                    if ch=='"':
                        break   # end of filename string
            return "".join(str)

        def _normal_token(self, startch):
            # called at start of a token, stops at first whitespace
            # Note: Exceptions checked outside
            tok=[startch]
            while True:
                ch = self.it.next()
                if ch in whitespace:
                    break
                tok.append(ch)

            return "".join(tok)

        def __iter__(self):
            # restart the iteration
            self.it = iter(self.stream)
            return self

        def next(self):
            token =""
            while True:
                ch = self.it.next() # here we just propagate the StopIteration ...
                if ch in whitespace or ch=='#':
                    continue  # skip spaces beetween tokens ...
                elif ch == '"':
                    try:
                        token = self._string_token()
                        break
                    except StopIteration:
                        # end of stream reached while in a string: Error!!
                        raise GetUpstreamChangesetsFailure("diff end while in string parsing.")
                else:
                    token = self._normal_token(ch)
                    break
            return token

    def __init__(self, repository, working_dir):
        self.working_dir = working_dir
        self.repository = repository

    def _addPathToSet(self, s, path):
        parts = path.split('/')
        while parts:
            s.add('/'.join(parts))
            parts.pop()

    def convertDiff(self, chset):
        """
        Fills a chset with the details data coming by a diff between
        chset lin_ancestor and revision (i.e. the linearized history)
        """
        if (not chset.lin_ancestor or
            not chset.revision or
            chset.lin_ancestor == chset.revision):
            raise GetUpstreamChangesetsFailure(
                "Internal error: MonotoneDiffParser.convertDiff called "
                "with invalid parameters: lin_ancestor %s, revision %s" %
                (chset.lin_ancestor, chset.revision))

        # the order of revisions is very important. Monotone gives a
        # diff from the first to the second
        cmd = self.repository.command("diff",
                                      "--db", self.repository.repository,
                                      "--revision", chset.lin_ancestor,
                                      "--revision", chset.revision)

        mtl = ExternalCommand(cwd=self.working_dir, command=cmd)
        outstr = mtl.execute(stdout=PIPE, stderr=PIPE)
        if mtl.exit_status:
            raise GetUpstreamChangesetsFailure(
                "mtn diff returned status %d" % mtl.exit_status)

        # monotone diffs are prefixed by a section containing
        # metainformations about files
        # The section terminates with the first file diff, and each
        # line is prepended by the patch comment char (#).
        tk = self.BasicIOTokenizer(outstr[0].getvalue())
        tkiter = iter(tk)
        in_item = False
        try:
            while True:
                token = tkiter.next()
                if token.startswith("========"):
                    # found first patch marker. Changeset info terminated
                    in_item = False
                    break
                else:
                    in_item = False
                    # now, next token should be a string or an hash,
                    # or the two tokens are "no changes"
                    fname = tkiter.next()
                    if token == "no" and fname == "changes":
                        break
                    elif fname[0] != '"' and fname[0] != '[':
                        raise GetUpstreamChangesetsFailure(
                            "Unexpected token sequence: '%s' "
                            "followed by '%s'" %(token, fname))

                    if token == "content":
                        pass  # ignore it
                    # ok, is a file/dir, control changesets data
                    elif token == "add_file" or token=="add_directory":
                        flag = True
                        for i,e in enumerate(chset.entries):
                            if e.action_kind == e.DELETED and e.name == fname[1:-1]:
                               e.action_kind = e.UPDATED
                               chentry = e
                               flag = False
                               break
                        if flag:
                            chentry = chset.addEntry(fname[1:-1], chset.revision)
                            chentry.action_kind = chentry.ADDED
                    elif token=="add_dir":
                        chentry = chset.addEntry(fname[1:-1], chset.revision)
                        chentry.action_kind = chentry.ADDED
                    elif token=="delete":
                        chentry = chset.addEntry(fname[1:-1], chset.revision)
                        chentry.action_kind = chentry.DELETED
                    elif token=="rename":
                        # renames are in the form:  oldname to newname
                        tow = tkiter.next()
                        newname = tkiter.next()
                        if tow != "to" or fname[0]!='"':
                            raise GetUpstreamChangesetsFailure(
                                "Unexpected rename token sequence: '%s' "
                                "followed by '%s'" %(tow, newname))
                        chentry = chset.addEntry(newname[1:-1], chset.revision)
                        chentry.action_kind = chentry.RENAMED
                        chentry.old_name= fname[1:-1]
                    elif token == "patch":
                        # patch entries are in the form: from oldrev to newrev
                        fromw = tkiter.next()
                        oldr = tkiter.next()
                        tow = tkiter.next()
                        newr = tkiter.next()
                        if fromw != "from" or tow != "to":
                            raise GetUpstreamChangesetsFailure(
                                "Unexpected patch token sequence: '%s' "
                                "followed by '%s','%s','%s'" % (fromw, oldr,
                                                                tow, newr))

                        # The 'chentry' is not nessesary if no other entries exist.
                        # But needs, if one entry with rename or delete exist before,
                        # because the list of modifired file will be upstream only
                        # files from this list (Monotone to Subversion).
                        # So, the best: Always list the changed files here.
                        #
                        # Add file to the list, if no rename or other entry exist.
                        flag = True
                        for i,e in enumerate(chset.entries):
                            if e.name == fname[1:-1]:
                                flag = False
                                break
                        if flag:
                            chentry = chset.addEntry(fname[1:-1], chset.revision)
                            chentry.action_kind = chentry.UPDATED

        except StopIteration:
            if in_item:
                raise GetUpstreamChangesetsFailure("Unexpected end of 'diff' parsing changeset info")


class MonotoneRevToCset:
    """
    This class is used to create changesets from revision ids.

    Since most backends (and tailor itself) don't support monotone
    multihead feature, sometimes we need to linearize the revision
    graph, creating syntethized (i.e. fake) edges between revisions.

    The revision itself is real, only its ancestors (and all changes
    between) are faked.

    To properly do this, changeset are created by a mixture of 'log'
    and 'diff' output. Log gives the revision data, diff the
    differences beetween revisions.

    Monotone also supports multiple authors/tags/comments for each
    revision, while tailor allows only single values.

    We collapse those multiple data (when present) to single entries
    in the following manner:

    author
      all entries separated by a comma

    date
      chooses only one, at random

    changelog
      all entries appended, without a specific order

    comment
      all comments are appended to the changelog string, prefixed by a
      "Note:" line

    tag
      not used by tailor. Ignored

    branch
      used to restrict source revs (tailor follows only a single branch)

    testresult
      ignored

    other certs
      ignored

    Changesets created by monotone will have additional fields with
    the original data:

    real_ancestors
      list of the real revision ancestor(s)

    real_dates
      list with all date certs

    lin_ancestor
      linearized ancestor (i.e. previous revision in the linearized history)
    """

    def __init__(self, repository, working_dir, branch):
        self.working_dir = working_dir
        self.repository = repository
        self.branch = branch
        self.logparser = MonotoneLogParser(repository=repository,
                                           working_dir=working_dir)
        self.diffparser = MonotoneDiffParser(repository=repository,
                                             working_dir=working_dir)

    def updateCset(self, chset):
        # Parsing the log fills the changeset from revision data
        self.logparser.convertLog(chset)

        # if an ancestor is available, fills the cset with file/dir entries
        if chset.lin_ancestor:
            self.diffparser.convertDiff(chset)

    def getCset(self, revlist, onlyFirst):
        """
        receives a revlist, already toposorted (i.e. ordered by
        ancestry) and outputs a list of changesets, filtering out revs
        outside the chosen branch. If onlyFirst is true, only the
        first valid element is considered
        """
        cslist=[]
        anc=revlist[0]
        if onlyFirst:
            start_index = 0
        else:
            start_index = 1
        for r in revlist[start_index:]:
            chtmp = MonotoneChangeset(anc, r)
            self.logparser.convertLog(chtmp)
            if self.branch in chtmp.branches:
                cslist.append(MonotoneChangeset(anc, r)) # using a new, unfilled changeset
                anc=r
                if onlyFirst:
                    break
        return cslist


class MonotoneWorkingDir(UpdatableSourceWorkingDir, SynchronizableTargetWorkingDir):

    def _convert_head_initial(self, dbrepo, module, revision, working_dir):
        """
        This method handles HEAD and INITIAL pseudo-revisions, converting
        them to monotone revids
        """
        effective_rev = revision
        if revision == 'HEAD' or revision=='INITIAL':
            # in both cases we need the head(s) of the requested branch
            cmd = self.repository.command("automate","heads",
                                          "--db", dbrepo, module)
            mtl = ExternalCommand(cwd=working_dir, command=cmd)
            outstr = mtl.execute(stdout=PIPE, stderr=PIPE)
            if mtl.exit_status:
                raise InvocationError("The branch '%s' is empty" % module)

            revision = outstr[0].getvalue().split()
            if revision == 'HEAD':
                if len(revision)>1:
                    raise InvocationError("Branch '%s' has multiple heads. "
                                          "Please choose only one." % module)
                effective_rev=revision[0]
            else:
                # INITIAL requested. We must get the ancestors of
                # current head(s), topologically sort them and pick
                # the first (i.e. the "older" revision). Unfortunately
                # if the branch has multiple heads then we could end
                # up with only part of the ancestry graph.
                if len(revision)>1:
                    self.log.info('Branch "%s" has multiple heads. There '
                                  'is no guarantee to reconstruct the '
                                  'full history.', module)
                cmd = [ self.repository.command("automate","ancestors",
                                                "--db",dbrepo),
                        self.repository.command("automate","toposort",
                                                "--db",dbrepo, "-@-")
                        ]
                cmd[0].extend(revision)
                cld = ExternalCommandChain(cwd=working_dir, command=cmd)
                outstr = cld.execute()
                if cld.exit_status:
                    raise InvocationError("Ancestor reading returned "
                                          "status %d" % cld.exit_status)
                revlist = outstr[0].getvalue().split()
                if len(revlist)>1:
                    mtr = MonotoneRevToCset(repository=self.repository,
                                            working_dir=working_dir,
                                            branch=module)
                    first_cset = mtr.getCset(revlist, True)
                    if len(first_cset)==0:
                        raise InvocationError("Can't find an INITIAL revision on branch '%s'."
                                              % module)
                    effective_rev=first_cset[0].revision
                elif len(revlist)==0:
                    # Special case: only one revision in branch - is the head self
                    effective_rev=revision[0]
                else:
                    effective_rev=revlist[0]
        return effective_rev

    ## UpdatableSourceWorkingDir

    def _getUpstreamChangesets(self, sincerev=None):
        # mtn descendents returns results sorted in alpha order
        # here we want ancestry order, so descendents output is feed back to
        # mtn for a toposort ...
        cmd = [ self.repository.command("automate","descendents",
                                        "--db", self.repository.repository,
                                        sincerev),
                self.repository.command("automate","toposort",
                                        "--db", self.repository.repository,
                                        "-@-")
                ]
        cld = ExternalCommandChain(cwd=self.repository.rootdir, command=cmd)
        outstr = cld.execute()
        if cld.exit_status:
            raise InvocationError("mtn descendents returned "
                                  "status %d" % cld.exit_status)

        # now childs is a list of revids, we must transform it in a
        # list of monotone changesets. We fill only the
        # linearized ancestor and revision ids, because at this time
        # we need only to know WICH changesets must be applied to the
        # target repo, not WHAT are the changesets (apart for filtering
        # the outside-branch revs)
        childs = [sincerev] +outstr[0].getvalue().split()
        mtr = MonotoneRevToCset(repository=self.repository,
                                working_dir=self.repository.rootdir,
                                branch=self.repository.module)
        chlist = mtr.getCset(childs, False)
        return chlist

    def _applyChangeset(self, changeset):
        cmd = self.repository.command("update", "--revision", changeset.revision)
        mtl = ExternalCommand(cwd=self.repository.basedir, command=cmd)
        mtl.execute(stdout=PIPE, stderr=PIPE)
        if mtl.exit_status:
            raise ChangesetApplicationFailure("'mtn update' returned "
                                              "status %s" % mtl.exit_status)
        mtr = MonotoneRevToCset(repository=self.repository,
                                working_dir=self.repository.basedir,
                                branch=self.repository.module)
        mtr.updateCset( changeset )

        return False   # no conflicts

    def _checkoutUpstreamRevision(self, revision):
        """
        Concretely do the checkout of the FIRST upstream revision.
        """
        effrev = self._convert_head_initial(self.repository.repository,
                                           self.repository.module, revision,
                                           self.repository.rootdir)
        if not exists(join(self.repository.basedir, '_MTN')):

            # actually check out the revision
            self.log.info("Checking out a working copy")
            cmd = self.repository.command("co",
                                          "--db", self.repository.repository,
                                          "--revision", effrev,
                                          "--branch", self.repository.module,
                                          self.repository.basedir)
            mtl = ExternalCommand(cwd=self.repository.rootdir, command=cmd)
            mtl.execute(stdout=PIPE, stderr=PIPE)
            if mtl.exit_status:
                raise TargetInitializationFailure(
                    "'mtn co' returned status %s" % mtl.exit_status)
        else:
            self.log.debug("%r already exists, assuming it's a monotone "
                           "working dir already populated", self.repository.basedir)

        # Ok, now the workdir contains the checked out revision. We
        # need to return a changeset describing it.  Since this is the
        # first revision checked out, we don't have a (linearized)
        # ancestor, so we must use None as the lin_ancestor parameter
        chset = MonotoneChangeset(None, effrev)

        # now we update the new chset with basic data - without the
        # linearized ancestor, changeset entries will NOT be filled
        mtr = MonotoneRevToCset(repository=self.repository,
                                working_dir=self.repository.basedir,
                                branch=self.repository.module)
        mtr.updateCset(chset)
        return chset

    ## SynchronizableTargetWorkingDir

    def _addPathnames(self, names):
        """
        Add some new filesystem objects, skipping directories.
        In monotone *explicit* directory addition is always recursive,
        so adding a directory here might interfere with renames.
        Adding files without directories doesn't cause problems,
        because adding a file implicitly adds the parent directory
        (non-recursively).
        """
        fnames=[]
        for fn in names:
            if isdir(join(self.repository.basedir, fn)):
                self.log.debug("ignoring addition of directory %r "
                               "(dirs are implicitly added by files)", fn)
            else:
                fnames.append(fn)
        if len(fnames):
            # ok, we still have something to add
            cmd = self.repository.command("add", "--")
            add = ExternalCommand(cwd=self.repository.basedir, command=cmd)
            add.execute(fnames, stdout=PIPE, stderr=PIPE)
            if add.exit_status:
                raise ChangesetApplicationFailure("%s returned status %s" %
                                                    (str(add),add.exit_status))

    def _addSubtree(self, subdir):
        """
        Add a whole subtree (recursively)
        """
        cmd = self.repository.command("add", "--recursive", "--")
        add = ExternalCommand(cwd=self.repository.basedir, command=cmd)
        add.execute(subdir, stdout=PIPE, stderr=PIPE)
        if add.exit_status:
            raise ChangesetApplicationFailure("%s returned status %s" %
                                              (str(add),add.exit_status))

    def _commit(self, date, author, patchname, changelog=None, entries=None,
                tags = [], isinitialcommit = False):
        """
        Commit the changeset.
        """

        encode = self.repository.encode

        logmessage = []
        if patchname:
            logmessage.append(patchname)
        if changelog:
            logmessage.append(changelog)

        rontf = ReopenableNamedTemporaryFile('mtn', 'tailor')
        log = open(rontf.name, "w")
        log.write(encode('\n'.join(logmessage)))
        log.close()

        date = date.astimezone(UTC).replace(microsecond=0, tzinfo=None) # monotone wants UTC
        cmd = self.repository.command("commit",
                                      "--author", encode(author),
                                      "--date", date.isoformat(),
                                      "--message-file", rontf.name)
        commit = ExternalCommand(cwd=self.repository.basedir, command=cmd)

        entries = None
        if not entries:
            entries = ['.']

        output, error = commit.execute(entries, stdout=PIPE, stderr=PIPE)

        # monotone complaints if there are no changes from the last commit.
        # we ignore those errors ...
        if commit.exit_status:
            text = error.read()
            if not "mtn: misuse: no changes to commit" in text:
                self.log.error("Monotone commit said: %s", text)
                raise ChangesetApplicationFailure(
                    "%s returned status %s" % (str(commit),commit.exit_status))
            else:
                self.log.info("No changes to commit - changeset ignored")

    def _removePathnames(self, names):
        """
        Remove some filesystem object.
        """

        cmd = self.repository.command("drop", "--recursive", "--")
        drop = ExternalCommand(cwd=self.repository.basedir, command=cmd)
        dum, error = drop.execute(names, stdout=PIPE, stderr=PIPE)
        if drop.exit_status:
            errtext = error.read()
            self.log.error("Monotone drop said: %s", errtext)
            raise ChangesetApplicationFailure("%s returned status %s" %
                                                  (str(drop),
                                                   drop.exit_status))

    def _renamePathname(self, oldname, newname):
        """
        Rename a filesystem object.
        """
        cmd = self.repository.command("rename", "--")
        rename = ExternalCommand(cwd=self.repository.basedir, command=cmd)
        rename.execute(oldname, newname, stdout=PIPE, stderr=PIPE)
        if rename.exit_status:
            raise ChangesetApplicationFailure(
                     "%s returned status %s" % (str(rename),rename.exit_status))

    def _prepareTargetRepository(self):
        """
        Check for target repository existence, eventually create it.
        """

        self.repository.create()

    def _prepareWorkingDirectory(self, source_repo):
        """
        Possibly checkout a working copy of the target VC, that will host the
        upstream source tree, when overriden by subclasses.
        """

        from re import escape

        if not self.repository.repository or exists(join(self.repository.basedir, '_MTN')):
            return

        if not self.repository.module:
            raise TargetInitializationFailure("Monotone needs a module "
                                              "defined (to be used as "
                                              "commit branch)")


        cmd = self.repository.command("setup",
                                      "--db", self.repository.repository,
                                      "--branch", self.repository.module)

        if self.repository.keygenid:
           self.repository.keyid = self.repository.keygenid
        if self.repository.keyid:
            cmd.extend( ("--key", self.repository.keyid) )

        setup = ExternalCommand(command=cmd)
        setup.execute(self.repository.basedir, stdout=PIPE, stderr=PIPE)

        if self.repository.passphrase or self.repository.custom_lua:
            monotonerc = open(join(self.repository.basedir, '_MTN', 'monotonerc'), 'w')
            if self.repository.passphrase:
                monotonerc.write(MONOTONERC % self.repository.passphrase)
            else:
                raise TargetInitializationFailure("The passphrase must be specified")
            if self.repository.custom_lua:
                self.log.info("Adding custom lua script")
                monotonerc.write(self.repository.custom_lua)
            monotonerc.close()

        # Add the tailor log file and state file to _MTN's list of
        # ignored files
        ignored = []
        logfile = self.repository.projectref().logfile
        if logfile.startswith(self.repository.basedir):
            ignored.append('^%s$' %
                           escape(logfile[len(self.repository.basedir)+1:]))

        sfname = self.repository.projectref().state_file.filename
        if sfname.startswith(self.repository.basedir):
            sfrelname = sfname[len(self.repository.basedir)+1:]
            ignored.append('^%s$' % escape(sfrelname))
            ignored.append('^%s$' % escape(sfrelname + '.old'))
            ignored.append('^%s$' % escape(sfrelname + '.journal'))

        if len(ignored) > 0:
            mt_ignored = open(join(self.repository.basedir, '.mtn-ignore'), 'a')
            mt_ignored.write('\n'.join(ignored))
            mt_ignored.close()

    def _initializeWorkingDir(self):
        """
        Setup the monotone working copy

        The user must setup a monotone working directory himself or use the
        tailor config file to provide parameters for creation. Then
        we simply use 'mtn commit', without having to specify a database
        file or branch. Monotone looks up the database and branch in it's _MTN
        directory.
        """

        if not exists(join(self.repository.basedir, '_MTN')):
            raise TargetInitializationFailure("Please setup '%s' as a "
                                              "monotone working directory" %
                                              self.repository.basedir)

        SynchronizableTargetWorkingDir._initializeWorkingDir(self)
