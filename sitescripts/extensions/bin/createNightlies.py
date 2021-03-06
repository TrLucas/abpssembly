# This file is part of the Adblock Plus web scripts,
# Copyright (C) 2006-present eyeo GmbH
#
# Adblock Plus is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 3 as
# published by the Free Software Foundation.
#
# Adblock Plus is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Adblock Plus.  If not, see <http://www.gnu.org/licenses/>.

"""

Nightly builds generation script
================================

  This script generates nightly builds of extensions, together
  with changelogs and documentation.

"""

import argparse
import ConfigParser
import binascii
import base64
import hashlib
import hmac
import json
import logging
import os
import pipes
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import uuid
from urllib import urlencode
import urllib2
import urlparse
import zipfile
import contextlib
from xml.dom.minidom import parse as parseXml

from Crypto.PublicKey import RSA
from Crypto.Signature import PKCS1_v1_5
import Crypto.Hash.SHA256

from sitescripts.extensions.utils import (
    compareVersions, Configuration,
    writeAndroidUpdateManifest,
)
from sitescripts.utils import get_config, get_template

MAX_BUILDS = 50


# Google and Microsoft APIs use HTTP error codes with error message in
# body. So we add the response body to the HTTPError to get more
# meaningful error messages.
class HTTPErrorBodyHandler(urllib2.HTTPDefaultErrorHandler):
    def http_error_default(self, req, fp, code, msg, hdrs):
        raise urllib2.HTTPError(req.get_full_url(), code,
                                '{}\n{}'.format(msg, fp.read()), hdrs, fp)


class NightlyBuild(object):
    """
      Performs the build process for an extension,
      generating changelogs and documentation.
    """

    downloadable_repos = {'gecko'}

    def __init__(self, config):
        """
          Creates a NightlyBuild instance; we are simply
          recording the configuration settings here.
        """
        self.config = config
        self.revision = self.getCurrentRevision()
        try:
            self.previousRevision = config.latestRevision
        except:
            self.previousRevision = '0'
        self.buildNum = None
        self.tempdir = None
        self.outputFilename = None
        self.changelogFilename = None

    def hasChanges(self):
        return self.revision != self.previousRevision

    def getCurrentRevision(self):
        """
            retrieves the current revision ID from the repository
        """
        command = [
            'hg', 'id', '-i', '-r', self.config.revision, '--config',
            'defaults.id=', self.config.repository,
        ]
        return subprocess.check_output(command).strip()

    def getCurrentBuild(self):
        """
            calculates the (typically numerical) build ID for the current build
        """
        command = ['hg', 'id', '-n', '--config', 'defaults.id=', self.tempdir]
        build = subprocess.check_output(command).strip()
        return build

    def getChanges(self):
        """
          retrieve changes between the current and previous ("first") revision
        """
        command = [
            'hg', 'log', '-R', self.tempdir, '-r',
            'reverse(ancestors({}))'.format(self.config.revision), '-l', '50',
            '--encoding', 'utf-8', '--template',
            '{date|isodate}\\0{author|person}\\0{rev}\\0{desc}\\0\\0',
            '--config', 'defaults.log=',
        ]
        result = subprocess.check_output(command).decode('utf-8')

        for change in result.split('\x00\x00'):
            if change:
                date, author, revision, description = change.split('\x00')
                yield {'date': date, 'author': author, 'revision': revision, 'description': description}

    def copyRepository(self):
        """
          Create a repository copy in a temporary directory
        """
        self.tempdir = tempfile.mkdtemp(prefix=self.config.repositoryName)
        command = ['hg', 'clone', '-q', self.config.repository, '-u',
                   self.config.revision, self.tempdir]
        subprocess.check_call(command)

        # Make sure to run ensure_dependencies.py if present
        depscript = os.path.join(self.tempdir, 'ensure_dependencies.py')
        if os.path.isfile(depscript):
            subprocess.check_call([sys.executable, depscript, '-q'])

    def symlink_or_copy(self, source, target):
        if hasattr(os, 'symlink'):
            if os.path.exists(target):
                os.remove(target)
            os.symlink(os.path.basename(source), target)
        else:
            shutil.copyfile(source, target)

    def writeChangelog(self, changes):
        """
          write the changelog file into the cloned repository
        """
        baseDir = os.path.join(self.config.nightliesDirectory, self.basename)
        if not os.path.exists(baseDir):
            os.makedirs(baseDir)
        changelogFile = '%s-%s.changelog.xhtml' % (self.basename, self.version)
        changelogPath = os.path.join(baseDir, changelogFile)
        self.changelogURL = urlparse.urljoin(self.config.nightliesURL, self.basename + '/' + changelogFile)

        template = get_template(get_config().get('extensions', 'changelogTemplate'))
        template.stream({'changes': changes}).dump(changelogPath, encoding='utf-8')

        linkPath = os.path.join(baseDir, '00latest.changelog.xhtml')
        self.symlink_or_copy(changelogPath, linkPath)

    def readGeckoMetadata(self):
        """
          read Gecko-specific metadata file from a cloned repository
          and parse id, version, basename and the compat section
          out of the file
        """
        import buildtools.packagerChrome as packager
        metadata = packager.readMetadata(self.tempdir, self.config.type)
        self.extensionID = packager.get_app_id(False, metadata)
        self.version = packager.getBuildVersion(self.tempdir, metadata, False,
                                                self.buildNum)
        self.basename = metadata.get('general', 'basename')
        self.min_version = metadata.get('compat', 'gecko')

    def readAndroidMetadata(self):
        """
          Read Android-specific metadata from AndroidManifest.xml file.
        """
        manifestFile = open(os.path.join(self.tempdir, 'AndroidManifest.xml'), 'r')
        manifest = parseXml(manifestFile)
        manifestFile.close()

        root = manifest.documentElement
        self.version = root.attributes['android:versionName'].value
        while self.version.count('.') < 2:
            self.version += '.0'
        self.version = '%s.%s' % (self.version, self.buildNum)

        usesSdk = manifest.getElementsByTagName('uses-sdk')[0]
        self.minSdkVersion = usesSdk.attributes['android:minSdkVersion'].value
        self.basename = os.path.basename(self.config.repository)

    def readChromeMetadata(self):
        """
          Read Chrome-specific metadata from metadata file. This will also
          calculate extension ID from the private key.
        """

        # Calculate extension ID from public key
        # (see http://supercollider.dk/2010/01/calculating-chrome-extension-id-from-your-private-key-233)
        import buildtools.packagerChrome as packager
        publicKey = packager.getPublicKey(self.config.keyFile)
        hash = hashlib.sha256()
        hash.update(publicKey)
        self.extensionID = hash.hexdigest()[0:32]
        self.extensionID = ''.join(map(lambda c: chr(97 + int(c, 16)), self.extensionID))

        # Now read metadata file
        metadata = packager.readMetadata(self.tempdir, self.config.type)
        self.version = packager.getBuildVersion(self.tempdir, metadata, False,
                                                self.buildNum)
        self.basename = metadata.get('general', 'basename')

        self.compat = []
        if metadata.has_section('compat') and metadata.has_option('compat', 'chrome'):
            self.compat.append({'id': 'chrome', 'minVersion': metadata.get('compat', 'chrome')})

    def readSafariMetadata(self):
        import sitescripts.extensions.bin.legacy.packagerSafari as packager
        from sitescripts.extensions.bin.legacy import xarfile
        metadata = packager.readMetadata(self.tempdir, self.config.type)
        certs = xarfile.read_certificates_and_key(self.config.keyFile)[0]

        self.certificateID = packager.get_developer_identifier(certs)
        self.version = packager.getBuildVersion(self.tempdir, metadata, False,
                                                self.buildNum)
        self.shortVersion = metadata.get('general', 'version')
        self.basename = metadata.get('general', 'basename')
        self.updatedFromGallery = False

    def read_edge_metadata(self):
        """
          Read Edge-specific metadata from metadata file.
        """
        from buildtools import packager
        # Now read metadata file
        metadata = packager.readMetadata(self.tempdir, self.config.type)
        self.version = packager.getBuildVersion(self.tempdir, metadata, False,
                                                self.buildNum)
        self.basename = metadata.get('general', 'basename')

        self.compat = []

    def writeUpdateManifest(self):
        """
          Writes update manifest for the current build
        """
        baseDir = os.path.join(self.config.nightliesDirectory, self.basename)
        if self.config.type == 'safari':
            manifestPath = os.path.join(baseDir, 'updates.plist')
            templateName = 'safariUpdateManifest'
            autoescape = True
        elif self.config.type == 'android':
            manifestPath = os.path.join(baseDir, 'updates.xml')
            templateName = 'androidUpdateManifest'
            autoescape = True
        elif self.config.type == 'gecko':
            manifestPath = os.path.join(baseDir, 'updates.json')
            templateName = 'geckoUpdateManifest'
            autoescape = True
        else:
            return

        if not os.path.exists(baseDir):
            os.makedirs(baseDir)

        # ABP for Android used to have its own update manifest format. We need to
        # generate both that and the new one in the libadblockplus format as long
        # as a significant amount of users is on an old version.
        if self.config.type == 'android':
            newManifestPath = os.path.join(baseDir, 'update.json')
            writeAndroidUpdateManifest(newManifestPath, [{
                'basename': self.basename,
                'version': self.version,
                'updateURL': self.updateURL,
            }])

        template = get_template(get_config().get('extensions', templateName),
                                autoescape=autoescape)
        template.stream({'extensions': [self]}).dump(manifestPath)

    def writeIEUpdateManifest(self, versions):
        """
          Writes update.json file for the latest IE build
        """
        if len(versions) == 0:
            return

        version = versions[0]
        packageName = self.basename + '-' + version + self.config.packageSuffix
        updateURL = urlparse.urljoin(self.config.nightliesURL, self.basename + '/' + packageName + '?update')
        baseDir = os.path.join(self.config.nightliesDirectory, self.basename)
        manifestPath = os.path.join(baseDir, 'update.json')

        from sitescripts.extensions.utils import writeIEUpdateManifest as doWrite
        doWrite(manifestPath, [{
            'basename': self.basename,
            'version': version,
            'updateURL': updateURL,
        }])

        for suffix in ['-x86.msi', '-x64.msi', '-gpo-x86.msi', '-gpo-x64.msi']:
            linkPath = os.path.join(baseDir, '00latest%s' % suffix)
            outputPath = os.path.join(baseDir, self.basename + '-' + version + suffix)
            self.symlink_or_copy(outputPath, linkPath)

    def build(self):
        """
          run the build command in the tempdir
        """
        if self.config.type not in self.downloadable_repos:
            baseDir = os.path.join(self.config.nightliesDirectory,
                                   self.basename)
        else:
            baseDir = self.tempdir

        if not os.path.exists(baseDir):
            os.makedirs(baseDir)
        outputFile = '%s-%s%s' % (self.basename, self.version, self.config.packageSuffix)
        self.path = os.path.join(baseDir, outputFile)
        self.updateURL = urlparse.urljoin(self.config.nightliesURL, self.basename + '/' + outputFile + '?update')

        if self.config.type == 'android':
            apkFile = open(self.path, 'wb')

            try:
                try:
                    port = get_config().get('extensions', 'androidBuildPort')
                except ConfigParser.NoOptionError:
                    port = '22'
                command = ['ssh', '-p', port, get_config().get('extensions', 'androidBuildHost')]
                command.extend(map(pipes.quote, [
                    '/home/android/bin/makedebugbuild.py', '--revision',
                    self.buildNum, '--version', self.version, '--stdout',
                ]))
                subprocess.check_call(command, stdout=apkFile, close_fds=True)
            except:
                # clear broken output if any
                if os.path.exists(self.path):
                    os.remove(self.path)
                raise
        else:
            env = os.environ
            spiderMonkeyBinary = self.config.spiderMonkeyBinary
            if spiderMonkeyBinary:
                env = dict(env, SPIDERMONKEY_BINARY=spiderMonkeyBinary)

            command = [os.path.join(self.tempdir, 'build.py')]
            command.extend(['build', '-t', self.config.type, '-b',
                            self.buildNum])

            if self.config.type not in {'gecko', 'edge'}:
                command.extend(['-k', self.config.keyFile])
            command.append(self.path)
            subprocess.check_call(command, env=env)

        if not os.path.exists(self.path):
            raise Exception("Build failed, output file hasn't been created")

        if self.config.type not in self.downloadable_repos:
            linkPath = os.path.join(baseDir,
                                    '00latest' + self.config.packageSuffix)
            self.symlink_or_copy(self.path, linkPath)

    def retireBuilds(self):
        """
          removes outdated builds, returns the sorted version numbers of remaining
          builds
        """
        baseDir = os.path.join(self.config.nightliesDirectory, self.basename)
        versions = []
        prefix = self.basename + '-'
        suffix = self.config.packageSuffix
        for fileName in os.listdir(baseDir):
            if fileName.startswith(prefix) and fileName.endswith(suffix):
                versions.append(fileName[len(prefix):len(fileName) - len(suffix)])
        versions.sort(compareVersions, reverse=True)
        while len(versions) > MAX_BUILDS:
            version = versions.pop()
            os.remove(os.path.join(baseDir, prefix + version + suffix))
            changelogPath = os.path.join(baseDir, prefix + version + '.changelog.xhtml')
            if os.path.exists(changelogPath):
                os.remove(changelogPath)
        return versions

    def updateIndex(self, versions):
        """
          Updates index page listing all existing versions
        """
        baseDir = os.path.join(self.config.nightliesDirectory, self.basename)
        if not os.path.exists(baseDir):
            os.makedirs(baseDir)
        outputFile = 'index.html'
        outputPath = os.path.join(baseDir, outputFile)

        links = []
        for version in versions:
            packageFile = self.basename + '-' + version + self.config.packageSuffix
            changelogFile = self.basename + '-' + version + '.changelog.xhtml'
            if not os.path.exists(os.path.join(baseDir, packageFile)):
                # Oops
                continue

            link = {
                'version': version,
                'download': packageFile,
                'mtime': os.path.getmtime(os.path.join(baseDir, packageFile)),
                'size': os.path.getsize(os.path.join(baseDir, packageFile)),
            }
            if os.path.exists(os.path.join(baseDir, changelogFile)):
                link['changelog'] = changelogFile
            links.append(link)
        template = get_template(get_config().get('extensions', 'nightlyIndexPage'))
        template.stream({'config': self.config, 'links': links}).dump(outputPath)

    def read_downloads_lockfile(self):
        path = get_config().get('extensions', 'downloadLockFile')
        try:
            with open(path, 'r') as fp:
                current = json.load(fp)
        except IOError:
            logging.debug('No lockfile found at ' + path)
            current = {}

        return current

    def write_downloads_lockfile(self, values):
        path = get_config().get('extensions', 'downloadLockFile')
        with open(path, 'w') as fp:
            json.dump(values, fp)

    def add_to_downloads_lockfile(self, platform, values):
        current = self.read_downloads_lockfile()

        current.setdefault(platform, [])
        current[platform].append(values)

        self.write_downloads_lockfile(current)

    def remove_from_downloads_lockfile(self, platform, filter_key,
                                       filter_value):
        current = self.read_downloads_lockfile()
        try:
            for i, entry in enumerate(current[platform]):
                if entry[filter_key] == filter_value:
                    del current[platform][i]
                if len(current[platform]) == 0:
                    del current[platform]
        except KeyError:
            pass
        self.write_downloads_lockfile(current)

    def azure_jwt_signature_fnc(self):
        return (
            'RS256',
            lambda s, m: PKCS1_v1_5.new(s).sign(Crypto.Hash.SHA256.new(m)),
        )

    def mozilla_jwt_signature_fnc(self):
        return (
            'HS256',
            lambda s, m: hmac.new(s, msg=m, digestmod=hashlib.sha256).digest(),
        )

    def sign_jwt(self, issuer, secret, url, signature_fnc, jwt_headers={}):
        alg, fnc = signature_fnc()

        header = {'typ': 'JWT'}
        header.update(jwt_headers)
        header.update({'alg': alg})

        issued = int(time.time())
        expires = issued + 60

        payload = {
            'aud': url,
            'iss': issuer,
            'sub': issuer,
            'jti': str(uuid.uuid4()),
            'iat': issued,
            'nbf': issued,
            'exp': expires,
        }

        segments = [base64.urlsafe_b64encode(json.dumps(header)),
                    base64.urlsafe_b64encode(json.dumps(payload))]

        signature = fnc(secret, b'.'.join(segments))
        segments.append(base64.urlsafe_b64encode(signature))
        return b'.'.join(segments)

    def generate_mozilla_jwt_request(self, issuer, secret, url, method,
                                     data=None, add_headers=[]):
        signed = self.sign_jwt(issuer, secret, url,
                               self.mozilla_jwt_signature_fnc)

        request = urllib2.Request(url, data)
        request.add_header('Authorization', 'JWT ' + signed)
        for header in add_headers:
            request.add_header(*header)
        request.get_method = lambda: method

        return request

    def uploadToMozillaAddons(self):
        import urllib3

        config = get_config()

        upload_url = ('https://addons.mozilla.org/api/v3/addons/{}/'
                      'versions/{}/').format(self.extensionID, self.version)

        with open(self.path, 'rb') as file:
            data, content_type = urllib3.filepost.encode_multipart_formdata({
                'upload': (
                    os.path.basename(self.path),
                    file.read(),
                    'application/x-xpinstall',
                ),
            })

        request = self.generate_mozilla_jwt_request(
            config.get('extensions', 'amo_key'),
            config.get('extensions', 'amo_secret'),
            upload_url,
            'PUT',
            data,
            [('Content-Type', content_type)],
        )

        try:
            urllib2.urlopen(request).close()
        except urllib2.HTTPError as e:
            shutil.copyfile(
                self.path,
                os.path.join(get_config().get('extensions', 'root'),
                             'failed.' + self.config.packageSuffix),
            )
            try:
                logging.error(e.read())
            finally:
                e.close()
            raise

        self.add_to_downloads_lockfile(
            self.config.type,
            {
                'buildtype': 'devbuild',
                'app_id': self.extensionID,
                'version': self.version,
            },
        )
        os.remove(self.path)

    def download_from_mozilla_addons(self, buildtype, version, app_id):
        config = get_config()
        iss = config.get('extensions', 'amo_key')
        secret = config.get('extensions', 'amo_secret')

        url = ('https://addons.mozilla.org/api/v3/addons/{}/'
               'versions/{}/').format(app_id, version)

        request = self.generate_mozilla_jwt_request(
            iss, secret, url, 'GET',
        )
        response = json.load(urllib2.urlopen(request))

        filename = '{}-{}.xpi'.format(self.basename, version)
        self.path = os.path.join(
            config.get('extensions', 'nightliesDirectory'),
            self.basename,
            filename,
        )

        necessary = ['passed_review', 'reviewed', 'processed', 'valid']
        if all(response[x] for x in necessary):
            download_url = response['files'][0]['download_url']
            checksum = response['files'][0]['hash']

            request = self.generate_mozilla_jwt_request(
                iss, secret, download_url, 'GET',
            )
            try:
                response = urllib2.urlopen(request)
            except urllib2.HTTPError as e:
                logging.error(e.read())

            # Verify the extension's integrity
            file_content = response.read()
            sha256 = hashlib.sha256(file_content)
            returned_checksum = '{}:{}'.format(sha256.name, sha256.hexdigest())

            if returned_checksum != checksum:
                logging.error('Checksum could not be verified: {} vs {}'
                              ''.format(checksum, returned_checksum))

            with open(self.path, 'w') as fp:
                fp.write(file_content)

            self.update_link = os.path.join(
                config.get('extensions', 'nightliesURL'),
                self.basename,
                filename,
            )

            self.remove_from_downloads_lockfile(self.config.type,
                                                'version',
                                                version)
        elif not response['passed_review'] or not response['valid']:
            # When the review failed for any reason, we want to know about it
            logging.error(json.dumps(response, indent=4))
            self.remove_from_downloads_lockfile(self.config.type,
                                                'version',
                                                version)

    def uploadToChromeWebStore(self):

        opener = urllib2.build_opener(HTTPErrorBodyHandler)

        # use refresh token to obtain a valid access token
        # https://developers.google.com/accounts/docs/OAuth2WebServer#refresh

        response = json.load(opener.open(
            'https://accounts.google.com/o/oauth2/token',

            urlencode([
                ('refresh_token', self.config.refreshToken),
                ('client_id', self.config.clientID),
                ('client_secret', self.config.clientSecret),
                ('grant_type', 'refresh_token'),
            ]),
        ))

        auth_token = '%s %s' % (response['token_type'], response['access_token'])

        # upload a new version with the Chrome Web Store API
        # https://developer.chrome.com/webstore/using_webstore_api#uploadexisitng

        request = urllib2.Request('https://www.googleapis.com/upload/chromewebstore/v1.1/items/' + self.config.devbuildGalleryID)
        request.get_method = lambda: 'PUT'
        request.add_header('Authorization', auth_token)
        request.add_header('x-goog-api-version', '2')

        with open(self.path, 'rb') as file:
            if file.read(8) != 'Cr24\x02\x00\x00\x00':
                raise Exception('not a chrome extension or unknown CRX version')

            # skip public key and signature
            file.seek(sum(struct.unpack('<II', file.read(8))), os.SEEK_CUR)

            request.add_header('Content-Length', os.fstat(file.fileno()).st_size - file.tell())
            request.add_data(file)

            response = json.load(opener.open(request))

        if response['uploadState'] == 'FAILURE':
            raise Exception(response['itemError'])

        # publish the new version on the Chrome Web Store
        # https://developer.chrome.com/webstore/using_webstore_api#publishpublic

        request = urllib2.Request('https://www.googleapis.com/chromewebstore/v1.1/items/%s/publish' % self.config.devbuildGalleryID)
        request.get_method = lambda: 'POST'
        request.add_header('Authorization', auth_token)
        request.add_header('x-goog-api-version', '2')
        request.add_header('Content-Length', '0')

        response = json.load(opener.open(request))

        if any(status not in ('OK', 'ITEM_PENDING_REVIEW') for status in response['status']):
            raise Exception({'status': response['status'], 'statusDetail': response['statusDetail']})

    def generate_certificate_token_request(self, url, private_key):
        # Construct the token request according to
        # https://docs.microsoft.com/en-us/azure/active-directory/develop/active-directory-certificate-credentials
        hex_val = binascii.a2b_hex(self.config.thumbprint)
        x5t = base64.urlsafe_b64encode(hex_val).decode()

        key = RSA.importKey(private_key)

        signed = self.sign_jwt(self.config.clientID, key, url,
                               self.azure_jwt_signature_fnc,
                               jwt_headers={'x5t': x5t})

        # generate oauth parameters for login.microsoft.com
        oauth_params = {
            'grant_type': 'client_credentials',
            'client_id': self.config.clientID,
            'resource': 'https://graph.windows.net',
            'client_assertion_type': 'urn:ietf:params:oauth:client-assertion-'
                                     'type:jwt-bearer',
            'client_assertion': signed,
        }

        request = urllib2.Request(url, urlencode(oauth_params))
        request.get_method = lambda: 'POST'

        return request

    def get_windows_store_access_token(self):
        # use client certificate to obtain a valid access token
        url_template = 'https://login.microsoftonline.com/{}/oauth2/token'
        url = url_template.format(self.config.tenantID)

        with open(self.config.privateKey, 'r') as fp:
            private_key = fp.read()

        opener = urllib2.build_opener(HTTPErrorBodyHandler)
        request = self.generate_certificate_token_request(url, private_key)

        with contextlib.closing(opener.open(request)) as response:
            data = json.load(response)
            auth_token = '{0[token_type]} {0[access_token]}'.format(data)

        return auth_token

    def upload_appx_file_to_windows_store(self, file_upload_url):
        # Add .appx file to a .zip file
        zip_path = os.path.splitext(self.path)[0] + '.zip'
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            zf.write(self.path, os.path.basename(self.path))

        # Upload that .zip file
        file_upload_url = file_upload_url.replace('+', '%2B')
        request = urllib2.Request(file_upload_url)
        request.get_method = lambda: 'PUT'
        request.add_header('x-ms-blob-type', 'BlockBlob')

        opener = urllib2.build_opener(HTTPErrorBodyHandler)

        with open(zip_path, 'rb') as file:
            request.add_header('Content-Length',
                               os.fstat(file.fileno()).st_size - file.tell())
            request.add_data(file)
            opener.open(request).close()

    # Clone the previous submission for the new one. Largely based on code
    # from https://msdn.microsoft.com/en-us/windows/uwp/monetize/python-code-examples-for-the-windows-store-submission-api#create-an-app-submission
    def upload_to_windows_store(self):
        opener = urllib2.build_opener(HTTPErrorBodyHandler)

        headers = {'Authorization': self.get_windows_store_access_token(),
                   'Content-type': 'application/json'}

        # Get application
        # https://docs.microsoft.com/en-us/windows/uwp/monetize/get-an-app
        api_path = '{}/v1.0/my/applications/{}'.format(
            'https://manage.devcenter.microsoft.com',
            self.config.devbuildGalleryID,
        )

        request = urllib2.Request(api_path, None, headers)
        with contextlib.closing(opener.open(request)) as response:
            app_obj = json.load(response)

        # Delete existing in-progress submission
        # https://docs.microsoft.com/en-us/windows/uwp/monetize/delete-an-app-submission
        submissions_path = api_path + '/submissions'
        if 'pendingApplicationSubmission' in app_obj:
            remove_id = app_obj['pendingApplicationSubmission']['id']
            remove_path = '{}/{}'.format(submissions_path, remove_id)
            request = urllib2.Request(remove_path, '', headers)
            request.get_method = lambda: 'DELETE'
            opener.open(request).close()

        # Create submission
        # https://msdn.microsoft.com/en-us/windows/uwp/monetize/create-an-app-submission
        request = urllib2.Request(submissions_path, '', headers)
        request.get_method = lambda: 'POST'
        with contextlib.closing(opener.open(request)) as response:
            submission = json.load(response)

        submission_id = submission['id']
        file_upload_url = submission['fileUploadUrl']

        # Update submission
        submission['applicationPackages'][0]['fileStatus'] = 'PendingDelete'
        submission['applicationPackages'].append({
            'fileStatus': 'PendingUpload',
            'fileName': os.path.basename(self.path),
        })

        new_submission_path = '{}/{}'.format(submissions_path,
                                             submission_id)
        new_submission = json.dumps(submission)

        request = urllib2.Request(new_submission_path, new_submission, headers)
        request.get_method = lambda: 'PUT'
        opener.open(request).close()

        self.upload_appx_file_to_windows_store(file_upload_url)

        # Commit submission
        # https://msdn.microsoft.com/en-us/windows/uwp/monetize/commit-an-app-submission
        commit_path = '{}/commit'.format(new_submission_path)
        request = urllib2.Request(commit_path, '', headers)
        request.get_method = lambda: 'POST'
        with contextlib.closing(opener.open(request)) as response:
            submission = json.load(response)

        if submission['status'] != 'CommitStarted':
            raise Exception({'status': submission['status'],
                             'statusDetails': submission['statusDetails']})

    def run(self):
        """
          Run the nightly build process for one extension
        """
        try:
            if self.config.type == 'ie':
                # We cannot build IE builds, simply list the builds already in
                # the directory. Basename has to be deduced from the repository name.
                self.basename = os.path.basename(self.config.repository)
            else:
                # copy the repository into a temporary directory
                self.copyRepository()
                self.buildNum = self.getCurrentBuild()

                # get meta data from the repository
                if self.config.type == 'android':
                    self.readAndroidMetadata()
                elif self.config.type == 'chrome':
                    self.readChromeMetadata()
                elif self.config.type == 'safari':
                    self.readSafariMetadata()
                elif self.config.type == 'gecko':
                    self.readGeckoMetadata()
                elif self.config.type == 'edge':
                    self.read_edge_metadata()
                else:
                    raise Exception('Unknown build type {}' % self.config.type)

                # create development build
                self.build()
                if self.config.type not in self.downloadable_repos:
                    # write out changelog
                    self.writeChangelog(self.getChanges())

                    # write update manifest
                    self.writeUpdateManifest()

            # retire old builds
            versions = self.retireBuilds()

            if self.config.type == 'ie':
                self.writeIEUpdateManifest(versions)

            if self.config.type not in self.downloadable_repos:
                # update index page
                self.updateIndex(versions)

            # update nightlies config
            self.config.latestRevision = self.revision

            if (self.config.type == 'gecko' and
                    self.config.galleryID and
                    get_config().has_option('extensions', 'amo_key')):
                self.uploadToMozillaAddons()
            elif self.config.type == 'chrome' and self.config.clientID and self.config.clientSecret and self.config.refreshToken:
                self.uploadToChromeWebStore()
            elif self.config.type == 'edge' and self.config.clientID and self.config.tenantID and self.config.privateKey and self.config.thumbprint:
                self.upload_to_windows_store()

        finally:
            # clean up
            if self.tempdir:
                shutil.rmtree(self.tempdir, ignore_errors=True)

    def download(self):
        download_info = self.read_downloads_lockfile()
        downloads = self.downloadable_repos.intersection(download_info.keys())

        if self.config.type in downloads:
            try:
                self.copyRepository()
                self.readGeckoMetadata()

                for data in download_info[self.config.type]:
                    self.version = data['version']

                    self.download_from_mozilla_addons(**data)
                    if os.path.exists(self.path):
                        # write out changelog
                        self.writeChangelog(self.getChanges())

                        # write update manifest
                        self.writeUpdateManifest()

                        # retire old builds
                        versions = self.retireBuilds()
                        # update index page
                        self.updateIndex(versions)

                        # Update soft link to latest build
                        baseDir = os.path.join(
                            self.config.nightliesDirectory, self.basename,
                        )
                        linkPath = os.path.join(
                            baseDir, '00latest' + self.config.packageSuffix,
                        )

                        self.symlink_or_copy(self.path, linkPath)
            finally:
                # clean up
                if self.tempdir:
                    shutil.rmtree(self.tempdir, ignore_errors=True)


def main(download=False):
    """
      main function for createNightlies.py
    """
    nightlyConfig = ConfigParser.SafeConfigParser()
    nightlyConfigFile = get_config().get('extensions', 'nightliesData')

    if os.path.exists(nightlyConfigFile):
        nightlyConfig.read(nightlyConfigFile)

    # build all extensions specified in the configuration file
    # and generate changelogs and documentations for each:
    data = None
    for repo in Configuration.getRepositoryConfigurations(nightlyConfig):
        build = None
        try:
            build = NightlyBuild(repo)
            if download:
                build.download()
            elif build.hasChanges():
                build.run()
        except Exception as ex:
            logging.error('The build for %s failed:', repo)
            logging.exception(ex)

    file = open(nightlyConfigFile, 'wb')
    nightlyConfig.write(file)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--download', action='store_true', default=False)
    args = parser.parse_args()
    main(args.download)
