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


from urllib2 import Request, urlopen, HTTPError, URLError
from urllib import quote
from urlparse import urlparse
from tempfile import NamedTemporaryFile
from tarfile import TarFile, TarInfo
import os, tarfile, shutil, os.path

from django.conf import settings
from django.db import transaction
from django.contrib.auth.models import User

from tardis.tardis_portal.metsexporter import MetsExporter

from tardis.apps.migration import MigrationError
from tardis.apps.migration.models import Archive
from tardis.tardis_portal.models import \
    Experiment, Dataset, Dataset_File, Replica, Location

import logging

logger = logging.getLogger(__name__)

def create_experiment_archive(exp, outfile):
    """Create an experiment archive for 'exp' writing it to the 
    file object given by 'outfile'.  The archive is in tar/gzip
    format, and contains a METs manifest and the data files for
    all Datasets currently in the Experiment.

    On completion, 'outfile' is closed.
    """
    with NamedTemporaryFile() as manifest:
        tf = tarfile.open(mode='w:gz', fileobj=outfile)
        MetsExporter().export_to_file(exp, manifest)
        manifest.flush()
        # (Note to self: path creation by string bashing is correct
        # here because these are not 'os' paths.  They are paths in 
        # the namespace of a TAR file, and '/' is always the separator.)
        tf.add(manifest.name, arcname=('%s/Manifest' % exp.id))
        for datafile in exp.get_datafiles():
            replica = datafile.get_preferred_replica(verified=True)
            try:
                fdst = NamedTemporaryFile(prefix='mytardis_tmp_ar_')
                f = datafile.get_file()
                shutil.copyfileobj(f, fdst)
                fdst.flush()
                arcname = '%s/%s/%s' % (exp.id, datafile.dataset.id,
                                        datafile.filename)
                tf.add(fdst.name, arcname=arcname)
            except URLError:
                logger.warn("Unable to fetch %s for archive creation." % 
                            datafile.filename)
            finally:
                fdst.close()
                f.close()
        tf.close()
        outfile.close()

def remove_experiment(exp):
    """Completely remove an Experiment, together with any Datasets,
    Datafiles and Replicas that belong to it exclusively.
    """
    for ds in Dataset.objects.filter(experiments=exp):
        if ds.experiments.count() == 1:
            for df in Dataset_File.objects.filter(dataset=ds):
                replicas = Replica.objects.filter(datafile=df, 
                                                  location__type='online')
                for replica in replicas:
                    location = Location.get_location(replica.location.name)
                    location.provider.remove_file(replica)
            ds.delete()
        else:
            ds.experiments.remove(exp)
    exp.delete()
    pass

def remove_experiment_data(exp, archive_url, archive_location):
    """Remove the online Replicas for an Experiment that are not shared with
    other Experiments.  When Replicas are removed, they are replaced with
    offline replicas whose 'url' consists of the archive_url, with the 
    archive pathname for the datafile as a url fragment id.
    """
    for ds in Dataset.objects.filter(experiments=exp):
        if ds.experiments.count() == 1:
            for df in Dataset_File.objects.filter(dataset=ds):
                replicas = Replica.objects.filter(datafile=df, 
                                                  location__type='online')
                if replicas.count() > 0:
                    for replica in replicas:
                        location = Location.get_location(replica.location.name)
                        location.provider.remove_file(replica)
                        if archive_url:
                            old_replica = replicas[0]
                            path_in_archive = '%s/%s/%s' % (
                                exp.id, ds.id, df.filename)
                            new_replica_url = '%s#%s' % (
                                archive_url, quote(path_in_archive))
                            new_replica = Replica(datafile=old_replica.datafile,
                                                  url=new_replica_url,
                                                  protocol=old_replica.protocol,
                                                  verified=True,
                                                  stay_remote=False,
                                                  location=archive_location)
                            new_replica.save()
                    replicas.delete()
                            
def create_archive_record(exp, url):
    """Create an Archive for an archive of the 'exp' Experiment.  The
    'url' is the Experiment archive URL
    """

    owner = User.objects.get(id=exp.created_by.id).username
    if exp.url:
        exp_url = exp.url
    else:
        exp_url = '%s/%s' % (settings.DEFAULT_EXPERIMENT_URL_BASE, exp.id)
    archive = Archive(experiment=exp,
                      experiment_title=exp.title,
                      experiment_owner=owner,
                      experiment_url=exp_url,
                      archive_url=url)
    archive.save()
    return archive
