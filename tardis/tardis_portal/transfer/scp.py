#
# Copyright (c) 2013, Centre for Microscopy and Microanalysis
#   (University of Queensland, Australia)
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#    * Redistributions of source code must retain the above copyright
#      notice, this list of conditions and the following disclaimer.
#    * Redistributions in binary form must reproduce the above copyright
#      notice, this list of conditions and the following disclaimer in the
#      documentation and/or other materials provided with the distribution.
#    * Neither the name of the University of Queensland nor the
#      names of its contributors may be used to endorse or promote products
#      derived from this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" 
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE 
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE 
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDERS AND CONTRIBUTORS BE 
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR 
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF 
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS 
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN 
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) 
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE 
# POSSIBILITY OF SUCH DAMAGE.
#

from urllib import quote
from string import Template
from urlparse import urlparse, urljoin
from contextlib import closing
from subprocess import CalledProcessError, STDOUT
from tempfile import NamedTemporaryFile
import os, sys, subprocess

from .base import TransferError, TransferProvider

import logging
logger = logging.getLogger(__name__)

class ScpTransfer(TransferProvider):
    """So far, this only implements the subset of the TransferProvider API
    needed to do archiving.

    The 'commands' hash provides a 'hook' that allows simple commands to
    be executed via the SSH session on the remote machine, before or after
    the main transfer.  This also allows to to override certain commands
    that are used by default.
    """
    
    def __init__(self, name, base_url, params):
        TransferProvider.__init__(self, name, base_url)
        parts = urlparse(base_url)
        if parts.scheme != 'scp':
            raise ValueError('scp: url required for transfer provider (%s)' %
                             name)
        if parts.username or parts.password:
            raise ValueError('url for transfer provider (%s) cannot use' 
                             ' a username or password' % name)
        if parts.path.find('#') != -1 or parts.path.find('?') != -1 or \
                parts.path.find(';') != -1:
            logger.warning('The base url for transfer provider (%s) appears'
                           ' to contain an http-style path param, query or'
                           ' fragment marker.  It will be treated as a plain'
                           ' pathname character')
        if not parts.hostname or not parts.path:
            raise ValueError('url for transfer provider (%s) requires a '
                             'non-empty hostname and path' % name)
        
        self.username = params.get('username', None)
        if not self.username:
            raise ValueError('No username parameter found')
             
        self.metadata_supported = True
        self.trust_length = self._isTrue(params, 'trust_length', False)
        self.commands = {
            'echo': 'echo hi',
            'mkdirs': 'mkdir -p "${path}"',
            'length': 'stat --format="%s" "${path}"',
            'remove': 'rm "${path}"',
            'scp_from': 'scp ${opts} ${username}@${hostname}:"${remote}" "${local}"',
            'scp_to': 'scp ${opts} "${local}" ${username}@${hostname}:"${remote}"',
            'ssh': 'ssh -o PasswordAuthentication=no ${opts} ${username}@${hostname}'}
        self.commands.update(params.get('commands', {}))
        self.base_url_path = urlparse(self.base_url).path

        self.key_filename = params.get('key_filename', None)
        self.hostname = parts.netloc
        self.port = parts.port if parts.port else 22

    def _get_scp_opts(self):
        opts = ''
        if self.key_filename:
            opts += ' -i %s' % self.key_filename
        if self.port != 22:
            opts += ' -P %s' % self.port
        return opts

    def _get_ssh_command(self):
        opts = ''
        if self.key_filename:
            opts += ' -i %s' % self.key_filename
        if self.port != 22:
            opts += ' -p %s' % self.port
        
        template = self.commands.get('ssh')
        return Template(template).safe_substitute(
                username=self.username,
                hostname=self.hostname,
                opts=opts)

    def alive(self):
        try:
            output = self.run_command('echo', {})
            if output == 'hi\n':
                return True
            else:
                logger.debug("SSH remote echo output is incorrect")
                return False
        except Exception as e:
            logger.warning('SSH aliveness test failed for provider %s' %
                           self.name)
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug('Cause of aliveness failure',
                             exc_info=sys.exc_info())
            return False

    def get_length(self, replica):
        (path, _, _) = self._analyse_url(replica.url)
        return int(self.run_command('length', {'path': path}).strip())
    
    def get_metadata(self, replica):
        raise NotImplementedError
    
    def get_opener(self, replica):
        (path, _, _) = self._analyse_url(replica.url)
        tmpFile = NamedTemporaryFile(mode='rb', prefix='mytardis_scp_', 
                                     delete=False)
        name = tmpFile.name
        self.run_command('scp_from', 
                         {'local': name, 'remote': path,
                          'username': self.username, 
                          'hostname': self.hostname})
        def opener():
            return _TemporaryFileWrapper(name)
        return opener

    def put_file(self, source_replica, target_replica):
        (path, dirname, filename) = self._analyse_url(target_replica.url)
        self.run_hook('pre_put_file', 
                      {'path': path, 'dirname': dirname, 
                       'filename': filename})
        if self.base_url_path != dirname:
            self.run_command('mkdirs', {'path': dirname})
        with closing(source_replica.get_file()) as f:
            # The 'scp' command copies to and from named files.
            if f.name: 
                self.run_command('scp_to', 
                                 {'local': f.name, 'remote': path,
                                  'username': self.username, 
                                  'hostname': self.hostname})
            else:
                with closing(NamedTemporaryFile(
                        mode='w+b', prefix='mytardis_scp_')) as t:
                    shutil.copyFileObj(f, t)
                    self.run_command('scp_to', 
                                     {'local': t.name, 'remote': path,
                                      'username': self.username, 
                                      'hostname': self.hostname})
            self.run_hook('post_put_file', 
                          {'path': path, 'dirname': dirname, 
                           'filename': filename})

    def put_archive(self, archive_filename, experiment):
        archive_url = self._generate_archive_url(experiment)
        (path, dirname, filename) = self._analyse_url(archive_url)
        self.run_command('pre_put_archive', 
                         {'path': path, 'dirname': dirname, 
                          'filename': filename},
                         optional=True)
        if self.base_url_path != dirname:
            self.run_command('mkdirs', {'path': dirname})
        self.run_command('scp_to', 
                         {'local': archive_filename, 'remote': path,
                          'username': self.username, 'hostname': self.hostname})
        self.run_command('post_put_archive', 
                         {'path': path, 'dirname': dirname, 
                          'filename': filename},
                         optional=True)
        return archive_url

    def remove_file(self, replica):
        (path, _, _) = self._analyse_url(replica.url)
        self.run_command('remove', {'path': path})

    def close(self):
        pass

    def _analyse_url(self, url):
        self._check_url(url)
        path = urlparse(url).path
        dirname = os.path.dirname(path)
        filename = os.path.basename(path)
        return (path, dirname, filename)

    def run_hook(self, key, params):
        return self.run_command(key, params, optional=True)

    def run_command(self, key, params, optional=False):
        template = self.commands.get(key)
        if not template:
            if optional:
                return
            raise TransferError('No command found for %s' % key)
      
        if key.startswith('scp'):
            params['opts'] = self._get_scp_opts()
            ssh_cmd = ''
        else:
            ssh_cmd = self._get_ssh_command()
        remote_cmd = Template(template).safe_substitute(params)
        command = '%s %s' % (ssh_cmd, remote_cmd)
        try:
            logger.debug(command)
            return subprocess.check_output(command, stderr=STDOUT, shell=True)
        except CalledProcessError as e:
            logger.debug('error output: %s\n' % e.output)
            raise TransferError('command %s failed: rc %s' %
                                (command, e.returncode))

class _TemporaryFileWrapper:
    # This is a cut-down / hacked about version of the same named
    # class in tempfile.  Main differences are 1) delete is hard-wired
    # 2) we open our own file object, 3) there is no __del__ because
    # it causes premature closing, and 4) stripped out the Windows.NT stuff.
    def __init__(self, name):
        self.name = name
        self.file = open(name, 'rb')
        self.close_called = False

    def __getattr__(self, name):
        file = self.__dict__['file']
        a = getattr(file, name)
        if not issubclass(type(a), type(0)):
            setattr(self, name, a)
        return a

    def __enter__(self):
        self.file.__enter__()
        return self

    def close(self):
        if not self.close_called:
            self.close_called = True
            self.file.close()
            if self.delete:
                os.unlink(self.name)
                    
    def __exit__(self, exc, value, tb):
        result = self.file.__exit__(exc, value, tb)
        self.close()
        return result
