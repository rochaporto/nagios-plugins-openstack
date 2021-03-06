#!/usr/bin/env python
# -*- encoding: utf-8 -*-
#
# Nagios event handler to evacuate VMs on a compute node failure.
#
# Copyright 2014 Catalyst IT.
#
# Author: Ricardo Rocha <ricardo@catalyst.net.nz>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#

import argparse
import random
import re
import sys
import syslog
from datetime import datetime, timedelta
import time
from keystoneclient.v2_0 import client as kclient
from keystoneclient import exceptions
from novaclient.v1_1 import client as nclient

syslog.openlog('nagios-nova-evacuate', 0, syslog.LOG_USER)

#
# State ID values for HOST states in nagios
#
UP = 0
DOWN = 1
UNREACHABLE = 2

#
# Argument parsing, as given by Nagios
#
parser = argparse.ArgumentParser(description='Evacuate VMs from compute node')
parser.add_argument('--auth_url', metavar='URL', type=str, required=True,
    help='Endpoint to the keystone service')
parser.add_argument('--username', metavar='username', type=str, required=True,
    help='username to use for authentication')
parser.add_argument('--password', metavar='password', type=str, required=True,
    help='password to use for authentication')
parser.add_argument('--tenant', metavar='tenant', type=str, required=True,
    help='tenant name to use for authentication')
parser.add_argument('--region_name', metavar='region_name', type=str,
    required=True, help='Region to select for authentication')
parser.add_argument('--ca-cert', metavar='ca_cert', type=str,
    help='Location of CA validation cert')
parser.add_argument('--insecure', action='store_true', default=False,
    help='Do not perform certificate validation')
parser.add_argument('--unreachable-is-down', action='store_true', default=False,
    help='True if we trigger evacuate on node unreachable')
parser.add_argument('--wait-timeout', metavar='wait_timeout', default=10,
    help='Time (in seconds) to wait for a successful evacuation before reporting failure')
parser.add_argument('compute_host', metavar='compute_host', type=str,
    help='Hostname of the compute node to evacuate')
parser.add_argument('state', metavar='state', type=str,
    help='Current state of probe (UP, DOWN, UNREACHEABLE)')
parser.add_argument('state_type', metavar='state_type', type=str,
    help='Current state type of probe (HARD, SOFT)')
args = parser.parse_args()

# By default unreachable does not trigger evacuate, but it's configurable
down_states = [DOWN]
if args.unreachable_is_down:
  down_states.append(UNREACHABLE)

# Is the state DOWN, and the state type HARD? Otherwise we do nothing for now...
if args.state_type != 'HARD' and args.state not in down_states:
  syslog.syslog("%s down, but probe not in HARD state yet (not running)" % args.compute_host)
  sys.exit(0)

syslog.syslog("%s DOWN and probe in HARD state, evacuating VMs" % args.compute_host)
# Get a nova client object (it takes care of keystone auth too)
try:
  nova = nclient.Client(args.username, args.password, args.tenant,
      auth_url=args.auth_url, region_name=args.region_name,
      insecure=args.insecure)
except Exception as e:
  syslog.syslog(syslog.LOG_ERR, "Failed to authenticate to keystone: %s" % str(e))
  sys.exit(-1)

# Check nova-compute is marked down for the compute host
for binary in ['nova-compute']:
  try:
    service_state = nova.services.list(host=args.compute_host, binary=binary)

    if len(service_state) != 1:
      syslog.syslog(syslog.LOG_ERR, "Got more than one %s on host %s" % (binary, args.compute_host))
      sys.exit(-1)
    if service_state.pop().state != 'down':
      syslog.syslog(syslog.LOG_ERR, "Nagios says down, but %s is still up in %s when querying nova" % (
          binary, args.compute_host))
      sys.exit(-1)
  except Exception as e:
    syslog.syslog(syslog.LOG_ERR, "Failed to list services %s for host %s :: %s" % (
      binary, args.compute_host, e))
    sys.exit(-1)

# List VMs associated to that compute node
try:
  vms = nova.servers.list(search_opts={'host': args.compute_host})
except Exception as e:
  syslog.syslog(syslog.LOG_ERR, "Failed to list VMs for host %s :: %s" % (args.compute_host, e))
  sys.exit(-1)

# Build target list (for the round robin approach)
# TODO: we should be using the scheduler instead of only a random assign
# https://blueprints.launchpad.net/nova/+spec/flexible-evacuate-scheduler
current_host = None
targets = []
try:
  services = nova.services.list()
  for service in services:
    if service.binary == 'nova-compute' and service.host != args.compute_host:
      targets.append(service.host)
except Exception as e:
  syslog.syslog(syslog.LOG_ERR, "Failed while filtering target hosts from service list :: %s" % e)
  sys.exit(-1)

# Trigger evacuation for each VM
# We collect the results to build a final report
results = {'success': [], 'failures': []}
for vm in vms:
  target = random.choice(targets)
  syslog.syslog("Evacuating '%s' to compute host '%s'" % (vm.name, target))
  try:
    success = False
    vm.evacuate(target, True)
    # wait for ACTIVE, or give up after 30 seconds
    start = datetime.now()
    cvm = nova.servers.list(search_opts={'name': vm.name})
    while datetime.now() < (start + timedelta(seconds=args.wait_timeout)):
      if cvm[0].status == 'ACTIVE':
        results['success'].append((vm.name, target))
        break
      time.sleep(3)
      cvm = nova.servers.list(search_opts={'name': vm.name})
    if cvm[0].status != 'ACTIVE':
      results['failures'].append((vm.name, "VM is in ERROR or UNKNOWN status, needs manual migration"))
  except Exception as e:
    results['failures'].append((vm.name, str(e)))
    syslog.syslog(syslog.LOG_ERR, "Failed to evacuate vm '%s' :: %s" % (vm.name, e))
syslog.syslog(syslog.LOG_ERR, "Evacuation of %s :: Successes (%d, %s) :: Failures (%d, %s)" % (
  args.compute_host, len(results['success']), results['success'], len(results['failures']), results['failures']))
