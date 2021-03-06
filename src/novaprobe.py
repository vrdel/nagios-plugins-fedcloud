#!/usr/bin/python

# Copyright (C) 2015 SRCE
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import argparse, re
import requests, sys, os, json
import urlparse
import time

from nagios_plugins_fedcloud import helpers

DEFAULT_PORT = 443
TIMEOUT_CREATE_DELETE = 600
SERVER_NAME = 'cloudmonprobe-servertest'

strerr = ''
num_excp_expand = 0

def get_info_v3(tenant, last_response):
    try:
       tenant_id = last_response.json()['token']['project']['id']
    except(KeyError, IndexError) as e:
        helpers.nagios_out('Critical', 'Could not fetch id for tenant %s: %s' % (tenant, helpers.errmsg_from_excp(e)), 2)

    try:
        service_catalog = last_response.json()['token']['catalog']
    except(KeyError, IndexError) as e:
        helpers.nagios_out('Critical', 'Could not fetch service catalog: %s' % (helpers.errmsg_from_excp(e)), 2)

    try:
        nova_url = None
        for e in service_catalog:
            if e['type'] == 'compute':
                for ep in e['endpoints']:
                    if ep['interface'] == 'public':
                        nova_url = ep['url']
            if e['type'] == 'image':
                for ep in e['endpoints']:
                    if ep['interface'] == 'public':
                        glance_url = ep['url']
        assert nova_url is not None
        assert glance_url is not None
    except(KeyError, IndexError, AssertionError) as e:
        helpers.nagios_out('Critical', 'Could not fetch nova compute service URL: Key not found %s' % (helpers.errmsg_from_excp(e)), 2)

    return tenant_id, nova_url, glance_url

def get_info_v2(tenant, last_response):
    try:
        tenant_id = last_response.json()['access']['token']['tenant']['id']
    except(KeyError, IndexError) as e:
        helpers.nagios_out('Critical', 'Could not fetch id for tenant %s: %s' % (tenant, helpers.errmsg_from_excp(e)), 2)

    try:
        service_catalog = last_response.json()['access']['serviceCatalog']
    except(KeyError, IndexError) as e:
        helpers.nagios_out('Critical', 'Could not fetch service catalog: %s' % (helpers.errmsg_from_excp(e)))

    try:
        nova_url = None
        glance_url = None
        for e in service_catalog:
            if e['type'] == 'compute':
                nova_url = e['endpoints'][0]['publicURL']
            if e['type'] == 'image':
                glance_url = e['endpoints'][0]['publicURL']
        assert nova_url is not None
        assert glance_url is not None
    except(KeyError, IndexError, AssertionError) as e:
        helpers.nagios_out('Critical', 'Could not fetch nova compute service URL: %s' % (helpers.errmsg_from_excp(e)))

    return tenant_id, nova_url, glance_url


def get_image_id(glance_url, ks_token, appdb_id):
    next_url = 'v2/images'
    try:
        # TODO: query for the exact image directly once that info is available in glance
        # that should remove the need for the loop
        while next_url:
            images_url  = urlparse.urljoin(glance_url, next_url)
            response = requests.get(images_url, headers = {'x-auth-token': ks_token}, verify=True)
            response.raise_for_status()
            for img in response.json()['images']:
                attrs = json.loads(img.get('APPLIANCE_ATTRIBUTES', '{}'))
                if attrs.get('ad:appid', '') == appdb_id:
                    return img['id']
            next_url = response.json().get('next', '')
    except (requests.exceptions.ConnectionError,
            requests.exceptions.Timeout, requests.exceptions.HTTPError) as e:
        helpers.nagios_out('Critical', 'Could not fetch image ID: %s' % helpers.errmsg_from_excp(e), 2)
    except (AssertionError, IndexError, AttributeError) as e:
        helpers.nagios_out('Critical', 'Could not fetch image ID: %s' % str(e), 2)
    helpers.nagios_out('Critical', 'Could not find image ID for AppDB image %s' % appdb_id, 2)


def get_smaller_flavor_id(nova_url, ks_token):
    flavor_url = nova_url + '/flavors/detail'
    # flavors with at least 8GB of disk, sorted by number of cpus
    query = {'minDisk': '8', 'sort_dir': 'asc', 'sort_key': 'vcpus'}
    headers = {'x-auth-token': ks_token}
    try:

        response = requests.get(flavor_url, headers=headers, params=query, verify=True)
        response.raise_for_status()
        flavors = response.json()['flavors']
        # minimum number of CPUs from first result (they are sorted)
        min_cpu = flavors[0]['vcpus']
        # take the first one after ordering by RAM
        return sorted(filter(lambda x: x['vcpus'] == min_cpu, flavors),
                      key=lambda x: x['ram']).pop(0)['id']
    except (requests.exceptions.ConnectionError,
            requests.exceptions.Timeout, requests.exceptions.HTTPError) as e:
        helpers.nagios_out('Critical', 'Could not fetch flavor ID: %s' % helpers.errmsg_from_excp(e), 2)
    except (AssertionError, IndexError, AttributeError) as e:
        helpers.nagios_out('Critical', 'Could not fetch flavor ID: %s' % str(e), 2)


def main():
    class ArgHolder(object):
        pass
    argholder = ArgHolder()

    argnotspec = []
    parser = argparse.ArgumentParser()
    parser.add_argument('--endpoint', dest='endpoint', nargs='?')
    parser.add_argument('-v', dest='verb', action='store_true')
    parser.add_argument('--flavor', dest='flavor', nargs='?')
    parser.add_argument('--image', dest='image', nargs='?')
    parser.add_argument('--cert', dest='cert', nargs='?')
    parser.add_argument('--access-token', dest='access_token', nargs='?')
    parser.add_argument('-t', dest='timeout', type=int, nargs='?', default=120)
    parser.add_argument('--appdb-image', dest='appdb_img', nargs='?')
    parser.add_argument('--protocol', dest='protocol', default='oidc', nargs='?')
    parser.add_argument('--identity-provider', dest='identity_provider', default='egi.eu', nargs='?')

    parser.parse_args(namespace=argholder)

    for arg in ['endpoint', 'timeout']:
        if eval('argholder.'+arg) == None:
            argnotspec.append(arg)

    if argholder.cert is None and argholder.access_token is None:
        helpers.nagios_out('Unknown', 'cert or access-token command-line arguments not specified', 3)

    if argholder.image is None and argholder.appdb_img is None:
        helpers.nagios_out('Unknown', 'image or appdb-image command-line arguments not specified', 3)

    if len(argnotspec) > 0:
        msg_error_args = ''
        for arg in argnotspec:
            msg_error_args += '%s ' % (arg)
        helpers.nagios_out('Unknown', 'command-line arguments not specified, '+msg_error_args, 3)
    else:
        if not argholder.endpoint.startswith("http") \
                or not type(argholder.timeout) == int:
            helpers.nagios_out('Unknown', 'command-line arguments are not correct', 3)
        if argholder.cert and not os.path.isfile(argholder.cert):
            helpers.nagios_out('Unknown', 'cert file does not exist', 3)
        if argholder.access_token and not os.path.isfile(argholder.access_token):
            helpers.nagios_out('Unknown', 'access-token file does not exist', 3)

    ks_token = None
    if argholder.access_token:
        access_file = open(argholder.access_token, 'r')
        access_token = access_file.read().rstrip("\n")
        access_file.close()
        try:
            ks_token, tenant, last_response = helpers.get_keystone_token_oidc_v3(argholder.endpoint,
                                                                                 argholder.timeout,
                                                                                 token=access_token,
                                                                                 identity_provider=argholder.identity_provider,
                                                                                 protocol=argholder.protocol)
            tenant_id, nova_url, glance_url = get_info_v3(tenant, last_response)
        except helpers.AuthenticationException as e:
            # log the error but don't really fail
            print 'Unable to authenticate with OIDC: %s' % e
    if not ks_token:
        if argholder.cert:
            # try with certificate v3
            try:
                ks_token, tenant, last_response = helpers.get_keystone_token_x509_v3(argholder.endpoint,
                                                                                     argholder.timeout,
                                                                                     userca=argholder.cert)
                tenant_id, nova_url, glance_url = get_info_v3(tenant, last_response)
            except helpers.AuthenticationException as e:
                # no more authentication methods to try, fail here
                print 'Unable to authenticate with VOMS + Keystone V3: %s' % e

    if not ks_token:
        if argholder.cert:
            # try with certificate v2
            try:
                ks_token, tenant, last_response = helpers.get_keystone_token_x509_v2(argholder.endpoint,
                                                                                     argholder.timeout,
                                                                                     userca=argholder.cert)
                tenant_id, nova_url, glance_url = get_info_v2(tenant, last_response)
            except helpers.AuthenticationException as e:
                # no more authentication methods to try, fail here
                helpers.nagios_out('Critical', str(e), 2)
        else:
            # just fail
            helpers.nagios_out('Critical', 'Unable to authenticate against Keystone', 2)

    if argholder.verb:
        print 'Endpoint: %s' % (argholder.endpoint)
        print 'Auth token (cut to 64 chars): %.64s' % ks_token
        print 'Project OPS, ID: %s' % tenant_id
        print 'Nova: %s' % nova_url
        print 'Glance: %s' % glance_url


    if not argholder.image:
        image = get_image_id(glance_url, ks_token, argholder.appdb_img)
    else:
        image = argholder.image

    if argholder.verb:
        print "Image: %s" % image

    if not argholder.flavor:
        flavor_id = get_smaller_flavor_id(nova_url, ks_token)
    else:
        # fetch flavor_id for given flavor (resource)
        try:
            headers, payload= {}, {}
            headers.update({'x-auth-token': ks_token})
            response = requests.get(nova_url + '/flavors', headers=headers, cert=argholder.cert,
                                    verify=True, timeout=argholder.timeout)
            response.raise_for_status()

            flavors = response.json()['flavors']
            flavor_id = None
            for f in flavors:
                if f['name'] == argholder.flavor:
                    flavor_id = f['id']
            assert flavor_id is not None
        except (requests.exceptions.ConnectionError,
                requests.exceptions.Timeout, requests.exceptions.HTTPError) as e:
            helpers.nagios_out('Critical', 'could not fetch flavor ID, endpoint does not correctly exposes available flavors: %s' % helpers.errmsg_from_excp(e), 2)
        except (AssertionError, IndexError, AttributeError) as e:
            helpers.nagios_out('Critical', 'could not fetch flavor ID, endpoint does not correctly exposes available flavors: %s' % str(e), 2)

    if argholder.verb:
        print "Flavor ID: %s" % flavor_id

    # create server
    try:
        headers, payload= {}, {}
        headers = {'content-type': 'application/json', 'accept': 'application/json'}
        headers.update({'x-auth-token': ks_token})
        payload = {'server': {'name': SERVER_NAME,
                              'imageRef': image,
                              'flavorRef': flavor_id}}
        response = requests.post(nova_url + '/servers', headers=headers,
                                    data=json.dumps(payload),
                                    cert=argholder.cert, verify=True,
                                    timeout=argholder.timeout)
        response.raise_for_status()
        server_id = response.json()['server']['id']
        if argholder.verb:
            print "Creating server:%s name:%s" % (server_id, SERVER_NAME)
    except (requests.exceptions.ConnectionError,
            requests.exceptions.Timeout, requests.exceptions.HTTPError,
            AssertionError, IndexError, AttributeError) as e:
        helpers.nagios_out('Critical', 'Could not launch server from image UUID:%s: %s' % (image, helpers.errmsg_from_excp(e)), 2)


    i, s, e, sleepsec, tss = 0, 0, 0, 1, 3
    server_createt, server_deletet= 0, 0
    server_built = False
    st = time.time()
    if argholder.verb:
        sys.stdout.write('Check server status every %ds: ' % (sleepsec))
    while i < TIMEOUT_CREATE_DELETE/sleepsec:
        # server status
        try:
            headers, payload= {}, {}
            headers.update({'x-auth-token': ks_token})
            response = requests.get(nova_url + '/servers/%s' % (server_id),
                                    headers=headers, cert=argholder.cert,
                                    verify=True,
                                    timeout=argholder.timeout)
            response.raise_for_status()
            status = response.json()['server']['status']
            if argholder.verb:
                sys.stdout.write(status+' ')
                sys.stdout.flush()
            if 'ACTIVE' in status:
                server_built = True
                et = time.time()
                break
            time.sleep(sleepsec)
        except (requests.exceptions.ConnectionError,
                requests.exceptions.Timeout, requests.exceptions.HTTPError,
                AssertionError, IndexError, AttributeError) as e:
            if i < tss and argholder.verb:
                sys.stdout.write('\n')
                sys.stdout.write('Try to fetch server:%s status one more time. Error was %s\n' % (server_id,
                                                                                                helpers.errmsg_from_excp(e)))
                sys.stdout.write('Check server status every %ds: ' % (sleepsec))
            else:
                helpers.nagios_out('Critical', 'could not fetch server:%s status: %s' % (server_id, helpers.errmsg_from_excp(e)), 2)
        i += 1
    else:
        if argholder.verb:
            sys.stdout.write('\n')
        helpers.nagios_out('Critical', 'could not create server:%s, timeout:%d exceeded' % (server_id, TIMEOUT_CREATE_DELETE), 2)

    server_createt = round(et - st, 2)

    if server_built:
        if argholder.verb:
            print "\nServer created in %.2f seconds" % (server_createt)

        # server delete
        try:
            headers, payload= {}, {}
            headers.update({'x-auth-token': ks_token})
            response = requests.delete(nova_url + '/servers/%s' %
                                        (server_id), headers=headers,
                                        cert=argholder.cert, verify=True,
                                        timeout=argholder.timeout)
            if argholder.verb:
                print "Trying to delete server=%s" % server_id
            response.raise_for_status()
        except (requests.exceptions.ConnectionError,
                requests.exceptions.Timeout, requests.exceptions.HTTPError,
                AssertionError, IndexError, AttributeError) as e:
            helpers.nagios_out('Critical', 'could not execute DELETE server=%s: %s' % (server_id, helpers.errmsg_from_excp(e)), 2)

        # waiting for DELETED status
        i = 0
        server_deleted = False
        st = time.time()
        if argholder.verb:
            sys.stdout.write('Check server status every %ds: ' % (sleepsec))
        while i < TIMEOUT_CREATE_DELETE/sleepsec:
            # server status
            try:
                headers, payload= {}, {}
                headers.update({'x-auth-token': ks_token})

                response = requests.get(nova_url + '/servers', headers=headers,
                                        cert=argholder.cert, verify=True,
                                        timeout=argholder.timeout)
                servfound = False
                for s in response.json()['servers']:
                    if server_id == s['id']:
                        servfound = True
                        response = requests.get(nova_url + '/servers/%s' %
                                                (server_id), headers=headers,
                                                cert=argholder.cert, verify=True,
                                                timeout=argholder.timeout)
                        response.raise_for_status()
                        status = response.json()['server']['status']
                        if argholder.verb:
                            sys.stdout.write(status+' ')
                            sys.stdout.flush()
                        if status.startswith('DELETED'):
                            server_deleted = True
                            et = time.time()
                            break

                if not servfound:
                    server_deleted = True
                    et = time.time()
                    if argholder.verb:
                        sys.stdout.write('DELETED')
                        sys.stdout.flush()
                    break

                time.sleep(sleepsec)
            except (requests.exceptions.ConnectionError,
                    requests.exceptions.Timeout,
                    requests.exceptions.HTTPError, AssertionError,
                    IndexError, AttributeError) as e:

                server_deleted = True
                et = time.time()

                if argholder.verb:
                    sys.stdout.write('\n')
                    sys.stdout.write('Could not fetch server:%s status: %s - server is DELETED' % (server_id,
                                                                                                    helpers.errmsg_from_excp(e)))
                    break
            i += 1
        else:
            if argholder.verb:
                sys.stdout.write('\n')
            helpers.nagios_out('Critical', 'could not delete server:%s, timeout:%d exceeded' % (server_id, TIMEOUT_CREATE_DELETE), 2)

    server_deletet = round(et - st, 2)

    if server_built and server_deleted:
        if argholder.verb:
            print "\nServer=%s deleted in %.2f seconds" % (server_id, server_deletet)
        helpers.nagios_out('OK', 'Compute instance=%s created(%.2fs) and destroyed(%.2fs)' % (server_id, server_createt, server_deletet), 0)

main()
