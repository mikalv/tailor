# -*- mode: python; coding: utf-8 -*-
# :Progetto: vcpx -- Git target (using git-core)
# :Creato:   Thu  1 Sep 2005 04:01:37 EDT
# :Autore:   Todd Mokros <tmokros@tmokros.net>
#            Brendan Cully <brendan@kublai.com>
#            Yann Dirson <ydirson@altern.org>
# :Licenza:  GNU General Public License
#

"""
This module implements the parts of the backend for Git using git-core,
common to source and target modules.
"""

__docformat__ = 'reStructuredText'

from vcpx.repository import Repository
from vcpx.shwrap import ExternalCommand, PIPE
from vcpx.config import ConfigurationError

class GitRepository(Repository):
    METADIR = '.git'

    def _load(self, project):
        Repository._load(self, project)
        self.EXECUTABLE = project.config.get(self.name, 'git-command', 'git')
        self.PARENT_REPO = project.config.get(self.name, 'parent-repo')
        self.BRANCHPOINT = project.config.get(self.name, 'branchpoint', 'HEAD')
        self.BRANCHNAME = project.config.get(self.name, 'branch')
        if self.BRANCHNAME:
            self.BRANCHNAME = 'refs/heads/' + self.BRANCHNAME

        if self.repository and self.PARENT_REPO:
            self.log.critical('Cannot make sense of both "repository" and "parent-repo" parameters')
            raise ConfigurationError ('Must specify only one of "repository" and "parent-repo"')

        if self.BRANCHNAME and not self.repository:
            self.log.critical('Cannot make sense of "branch" if "repository" is not set')
            raise ConfigurationError ('Missing "repository" to make use o "branch"')

        self.env = {}

        if self.repository:
            self.storagedir = self.repository
            self.env['GIT_DIR'] = self.storagedir
            self.env['GIT_INDEX_FILE'] = self.METADIR + '/index'
        else:
            self.storagedir = self.METADIR

    def _tryCommand(self, cmd, exception=Exception, pipe=True):
        c = GitExternalCommand(self,
                               command = self.command(*cmd), cwd = self.basedir)
        if pipe:
            output = c.execute(stdout=PIPE)[0]
        else:
            c.execute()
        if c.exit_status:
            raise exception(str(c) + ' failed')
        if pipe:
            return output.read().split('\n')

class GitExternalCommand(ExternalCommand):
    def __init__(self, repo, command=None, cwd=None):
        """
        Initialize an ExternalCommand instance tied to a GitRepository
        from which it inherits a set of environment variables to use
        for each execute().
        """

        self.repo = repo
        return ExternalCommand.__init__(self, command, cwd)

    def execute(self, *args, **kwargs):
        """Execute the command, with controlled environment."""

        if not kwargs.has_key('env'):
            kwargs['env'] = {}

        kwargs['env'].update(self.repo.env)

        return ExternalCommand.execute(self, *args, **kwargs)
