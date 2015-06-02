#!/usr/bin/env python
#    Copyright Reliance Jio Infocomm, Ltd.
#    Author: Soren Hansen <Soren.Hansen@ril.com>
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.
#
import argparse
import errno
import sys
import socket
import time
import urllib3
import urlparse
import os
import netifaces
import re
import json
import yaml
import consulate
from urllib3.exceptions import HTTPError

class DeploymentOrchestrator(object):
    UPDATE_AVAILABLE = 0
    UP_TO_DATE = 1
    NO_CLUE = 2
    NO_CLUE_BUT_WERE_JUST_GETTING_STARTED = 3

    def __init__(self, host='127.0.0.1', port=8500):
        self.host = host
        self.port = port
        self._consul = None
        self._kv = None

    @property
    def consul(self):
        if not self._consul:
            self._consul = session = consulate.Consulate(self.host, self.port)
        return self._consul

    def trigger_update(self, new_version):
        self.consul.kv.set('/current_version', new_version)

    def local_health(self, hostname=socket.gethostname(), verbose=False):
        results = self.consul.health.node(hostname)
        failing = [x for x in results if (x['Status'] == 'critical'
                                          or (x['Status'] == 'warning' and (x['Name'] == 'puppet' or x['Name'] == 'validation'))) ]
        if verbose:
            for x in failing:
                print '%s: %s' % (x['Name'], x['Output'])
        return failing

    def pending_update(self):
        local_version = self.local_version()
        try:
            if (self.current_version() == local_version):
                return self.UP_TO_DATE
            elif (self.current_version() == None):
                return self.NO_CLUE_BUT_WERE_JUST_GETTING_STARTED
            else:
                return self.UPDATE_AVAILABLE
        except:
            if local_version:
                return self.NO_CLUE
            else:
                return self.NO_CLUE_BUT_WERE_JUST_GETTING_STARTED

    def current_version(self):
        cur_ver = self.consul.kv.get('/current_version')
        if cur_ver == None:
            return None
        else:
            return str(cur_ver).strip()

    def ping(self):
        try:
            return bool(self.consul.agent.members())
        except (IOError, HTTPError):
            return False

    def update_own_status(self, hostname, status_type, status_result):
        status_dir = '/status/%s' % status_type
        if status_type == 'puppet':
            if int(status_result) in (4, 6, 1):
                self.consul.agent.check.ttl_warn('puppet')
            elif int(status_result) == -1:
                self.consul.agent.check.ttl_warn('puppet')
            else:
                self.consul.agent.check.ttl_pass('puppet')
        elif status_type == 'puppet_service':
            if int(status_result) in (4, 6, 1):
                self.consul.agent.check.ttl_fail('service:puppet')
            elif int(status_result) == -1:
                self.consul.agent.check.ttl_fail('service:puppet')
            else:
                self.consul.agent.check.ttl_pass('service:puppet')
        elif status_type == 'validation':
            if int(status_result) == 0:
                self.consul.agent.check.ttl_pass('validation')
            else:
                self.consul.agent.check.ttl_warn('validation')
        elif status_type == 'validation_service':
            if int(status_result) == 0:
                self.consul.agent.check.ttl_pass('service:validation')
            else:
                self.consul.agent.check.ttl_fail('service:validation')
        else:
            raise Exception('Invalid status_type:%s' % status_type)

    # this is not removing outdated versions?
    def update_own_info(self, hostname, version=None):
        version = version or self.local_version()
        if not version:
            return
        version_dir = '/running_version/%s' % version
        self.consul.kv.set('%s/%s' % (version_dir, hostname), str(time.time()))
        versions = self.running_versions()
        versions.discard(version)
        # check if other versions are registered for the same host
        for v in versions:
            if hostname in self.hosts_at_version(v):
                self.consul.kv.__delitem__('%s/%s/%s' % ('running_version', v, hostname))

    # this call may not scale
    # if pulls down all host version records as
    # a single hash
    def running_versions(self):
        try:
            res = self.consul.kv.find('/running_version')
            return set([x.split('/')[1] for x in res])
        except (KeyError, IndexError):
            return set()

    # this call may not scale
    # if pulls down all host version records as
    # a single hash
    def hosts_at_version(self, version):
        version_dir = '/running_version/%s' % (version,)
        try:
            res = self.consul.kv.find(version_dir)
        except KeyError:
            return []
        result_set = set()
        for x in res:
            if x.split('/')[-2] == version:
                host = x.split('/')[-1]
                if host:
                    result_set.add(host)
        return result_set

    def get_failures(self, hosts=False, show_warnings=False):
        failures = self.consul.health.state('critical')
        warnings = self.consul.health.state('warning')
        # validation and puppet failures are being treated as warnings in consul
        # that when they fail during bootstrapping, it does not cause services
        # to be deregistered by consul. This code ensures that those "warnings"
        # are treated as failures in this context
        puppet_failures = [w for w in warnings if w['Name'] == 'puppet']
        validation_failures = [w for w in warnings if w['Name'] == 'validation']
        failures = failures + puppet_failures + validation_failures
        if hosts:
            if len(failures) != 0: print "Failures:"
            for x in failures:
                print "  Node: %s, Check: %s" % (x['Node'], x['Name'])
        other_warnings = [w for w in warnings if w['Name'] != 'validation' and w['Name'] != 'puppet']
        if show_warnings:
            if hosts:
                if len(other_warnings) != 0: print "Warnings:"
                for x in other_warnings:
                    print "  Node: %s, Check: %s" % (x['Node'], x['Name'])
            failures = failures + other_warnings
        return len(failures) == 0

    def verify_hosts(self, version, hosts):
        return set(hosts).issubset(self.hosts_at_version(version))

    def check_single_version(self, version, verbose=False):
        running_versions = self.running_versions()
        unwanted_versions = filter(lambda x: x != version,
                                   running_versions)
        wanted_version_found = version in running_versions
        if verbose:
            print 'Wanted version found:', wanted_version_found
            print 'Unwanted versions found:', ', '.join(unwanted_versions)
        return wanted_version_found and not unwanted_versions

    def local_version(self, new_value=None):
        mode = new_value is None and 'r' or 'w'

        try:
            with open('/etc/current_version', mode) as fp:
                if new_value is None:
                    return fp.read().strip()
                else:
                    fp.write(new_value)
                    return new_value
        except IOError, e:
            if e.errno == errno.ENOENT:
                return ''
            raise

    def debug_timeout(self, version):
        self.get_failures(True) 
        if self.hosts_at_version(version):
            print "Registered hosts in consul with key name Running_Versions are:"
            for reg_host in self.hosts_at_version(version):
                print "   %s" % reg_host 
        else:
            print "No Hosts registered!"


def main(argv=sys.argv[1:]):
    parser = argparse.ArgumentParser(description='Utility for '
                                                 'orchestrating updates')
    parser.add_argument('--host', type=str,
                        default='127.0.0.1', help="local consul agent")
    parser.add_argument('--port', type=int, default=8500, help="consul port")
    subparsers = parser.add_subparsers(dest='subcmd')

    trigger_parser = subparsers.add_parser('trigger_update',
                                           help='Trigger an update')
    trigger_parser.add_argument('version', type=str, help='Version to deploy')

    current_version_parser = subparsers.add_parser('current_version',
                                                   help='Get available version')

    ping_parser = subparsers.add_parser('ping', help='Ping consul')

    pending_update = subparsers.add_parser('pending_update',
                                           help='Check for pending update')

    local_health_parser = subparsers.add_parser('local_health', help='Check health of local system')
    local_health_parser.add_argument('--verbose', '-v', action='store_true', help='Be verbose')

    local_version_parser = subparsers.add_parser('local_version',
                                                 help='Get or set local version')
    local_version_parser.add_argument('version', nargs='?', help="If given, set this as the local version")
    update_own_status_parser = subparsers.add_parser('update_own_status', help="Update info related to the current status of a host")
    update_own_status_parser.add_argument('--hostname', type=str, default=socket.gethostname(),
                                          help="This system's hostname")
    update_own_status_parser.add_argument('status_type', type=str, help="Type of status to update")
    update_own_status_parser.add_argument('status_result', type=int, help="Command exit code used to derive status")
    list_failures_parser = subparsers.add_parser('get_failures', help="Return a list of every failed host. Returns the number of hosts in a failed state")
    list_failures_parser.add_argument('--hosts', action='store_true', help="list out all hosts in each state and not just the number in each state")
    list_failures_parser.add_argument('--show_warnings', action='store_true', help="Whether to count warnings as failures")
    update_own_info_parser = subparsers.add_parser('update_own_info', help="Update host's own info")
    update_own_info_parser.add_argument('--hostname', type=str, default=socket.gethostname(),
                                        help="This system's hostname")
    update_own_info_parser.add_argument('--version', type=str,
                                        help="Override version to report into consul")

    running_versions_parser = subparsers.add_parser('running_versions', help="List currently running versions")
    hosts_at_version_parser = subparsers.add_parser('hosts_at_version', help="List hosts at specified version")
    hosts_at_version_parser.add_argument('version', type=str, help="Version to retrieve list of hosts for")

    verify_hosts_parser = subparsers.add_parser('verify_hosts', help="Verify that list of hosts are all available")
    verify_hosts_parser.add_argument('version', help="Version to look for")

    check_single_version_parser = subparsers.add_parser('check_single_version', help="Check if the given version is the only one currently running")
    check_single_version_parser.add_argument('version', help='The version to check for')

    debug_timeout_parser = subparsers.add_parser('debug_timeout', help="Provides debug information when script gets  timed out")
    debug_timeout_parser.add_argument('version', help="Version to look for")
    check_single_version_parser.add_argument('--verbose', '-v', action='store_true', help='Be verbose')
    args = parser.parse_args(argv)

    do = DeploymentOrchestrator(args.host, args.port)
    if args.subcmd == 'trigger_update':
        do.trigger_update(args.version)
    elif args.subcmd == 'current_version':
        print do.current_version()
    elif args.subcmd == 'check_single_version':
        sys.exit(not do.check_single_version(args.version, args.verbose))
    elif args.subcmd == 'update_own_status':
        do.update_own_status(args.hostname, args.status_type, args.status_result)
    elif args.subcmd == 'update_own_info':
        do.update_own_info(args.hostname, version=args.version)
    elif args.subcmd == 'ping':
        did_it_work = do.ping()
        if did_it_work:
            print 'Connection succesful'
            return 0
        else:
            print 'Connection failed'
            return 1
    elif args.subcmd == 'local_version':
        print do.local_version(args.version)
    elif args.subcmd == 'running_versions':
        print '\n'.join(do.running_versions())
    elif args.subcmd == 'hosts_at_version':
        print '\n'.join(do.hosts_at_version(args.version))
    elif args.subcmd == 'verify_hosts':
        buffer = sys.stdin.read().strip()
        hosts = buffer.split('\n')
        return not do.verify_hosts(args.version, hosts)
    elif args.subcmd == 'get_failures':
        return not do.get_failures(args.hosts, args.show_warnings)
    elif args.subcmd == 'local_health':
        failures = do.local_health(socket.gethostname(), args.verbose)
        return len(failures)
    elif args.subcmd == 'pending_update':
        pending_update = do.pending_update()
        msg = {do.UPDATE_AVAILABLE: "Yes, there is an update pending",
               do.UP_TO_DATE: "No updates pending",
               do.NO_CLUE: "Could not get current_version",
               do.NO_CLUE_BUT_WERE_JUST_GETTING_STARTED: "Could not get current_version, but there's also no local version set"
               }[pending_update]
        print msg
        return pending_update
    elif args.subcmd == 'debug_timeout':
        return not do.debug_timeout(args.version)

if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
