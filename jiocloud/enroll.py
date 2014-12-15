#!/usr/bin/env python
from __future__ import print_function
import argparse
import hpilo
from ironicclient import client
import os
import sys

def get_ilo_connection(hostname, username, password):
    return hpilo.Ilo(hostname, username, password)

def get_host_data(ilo):
    return ilo.get_host_data()

def extract_cpu_info(host_data):
    cpus = filter(lambda x:x['type'] == 4, host_data)
    core_count = 0
    for cpu in cpus:
        try:
            core_count += int(cpu['Execution Technology'].split(' ')[0])
        except:
            print('Failure parsing CPU info:')
            print(cpu)
            raise
    return core_count

def extract_mem_info(host_data):
    dimms = filter(lambda x:x['type'] == 17, host_data)
    total_memory = 0
    for dimm in dimms:
        size_str = dimm['Size']
        if size_str.endswith(' MB'):
            total_memory += int(size_str.split(' ')[0])
        elif size_str == 'not installed':
            continue
        else:
            raise Exception('Could not parse dimm info: %r' % dimm)
    return total_memory

def extract_net_info(host_data):
    return filter(lambda x:x['type'] == 209, host_data)[0]

def extract_macs(net_info):
    info = {}
    curport = None

    for f in net_info['fields']:
        if f['name'] == 'Port':
            curport = f['value']
        if f['name'] == 'MAC':
            info[str(curport)] = f['value'].replace('-', ':').lower()
    return info

def get_ironic_client(username, password, auth_url, tenant_name):
    kwargs = {'os_username': username,
              'os_password': password,
              'os_auth_url': auth_url,
              'os_tenant_name': tenant_name }

    return client.get_client(1, **kwargs)

def p(*args):
    print(*args, end='')
    sys.stdout.flush()

def create_node(ironic, username, password, address, mac, total_memory, total_cores):
    p('Creating chassis.. ',)
    chassis = ironic.chassis.create()
    print(chassis.uuid)
    p('Creating node.. ',)
    node = ironic.node.create(chassis_uuid=chassis.uuid,
                              driver='pxe_ipmitool',
                              driver_info={'ipmi_username': username,
                                           'ipmi_password': password,
                                           'ipmi_terminal_port': 0,
                                           'ipmi_address': address},
                              properties={'cpus': total_cores,
                                          'memory_mb': total_memory,
                                          'local_gb': 678,
                                          'cpu_arch': 'x86_64'}
                              )
    print(node.uuid)
    p('Creating port.. ',)
    port = ironic.port.create(address=mac, node_uuid=node.uuid)
    print(port.uuid)

def get_port_by_mac(ironic, mac):
    p('Looking up port in Ironic... ',)
    port = None
    for _port in ironic.port.list():
        if _port.address.lower() == mac:
            return port

def main(argv=sys.argv):
    parser = argparse.ArgumentParser(description='Enroll HP server to Ironic.')
    parser.add_argument('--ilo_username', type=str,
                       help='iLO username')
    parser.add_argument('--ilo_password', type=str,
                       help='iLO password')
    parser.add_argument('--ilo_address', type=str,
                       help='iLO address')
    parser.add_argument('--os_username', type=str,
                       default=os.environ.get('OS_USERNAME'),
                       help='Ironic username')
    parser.add_argument('--os_tenant', type=str,
                       default=os.environ.get('OS_TENANT_NAME'),
                       help='Ironic tenant name')
    parser.add_argument('--os_password', type=str,
                       default=os.environ.get('OS_PASSWORD'),
                       help='Ironic password')
    parser.add_argument('--os_auth_url', type=str,
                       default=os.environ.get('OS_AUTH_URL'),
                       help='Ironic auth URL')
    parser.add_argument('--delete', action='store_true',
                       default=False,
                       help='Delete instead of create')
    parser.add_argument('--nic', type=str,
                       default='1',
                       help='ID of NIC to use')
    parser.add_argument('--noop', action='store_true',
                       help="Only pretend to add the node to Ironic")
    args = parser.parse_args()
    if (not args.os_username
        or not args.os_tenant
        or not args.os_password
        or not args.os_auth_url
        or not args.ilo_username
        or not args.ilo_password
        or not args.ilo_address):
       print('You must supply all details')
       parser.print_help()
       sys.exit(1)

    ilo = get_ilo_connection(args.ilo_address, args.ilo_username, args.ilo_password)
    host_data = get_host_data(ilo)
    total_memory = extract_mem_info(host_data)
    total_cores = extract_cpu_info(host_data)
    mac = extract_macs(extract_net_info(host_data))[args.nic]
    print('Total memory: %d MB' % total_memory)
    print('Total cores: %d' % total_cores)
    print('MAC: %s' % mac)
    if args.noop:
        return True
    ironic = get_ironic_client(args.os_username, args.os_password,
                                args.os_auth_url, args.os_tenant)
    port = get_port_by_mac(ironic, mac)
    if args.delete:
        if port is None:
            raise Exception('Could not find port')
        port = ironic.port.get(port.uuid)
        print(port.uuid)
        p('Getting node... ',)
        node = ironic.node.get(port.node_uuid)
        print(port.node_uuid)
        p('Getting chassis... ',)
        chassis = ironic.chassis.get(node.chassis_uuid)
        print(node.chassis_uuid)
        p('Deleting port... ',)
        ironic.port.delete(port.uuid)
        print('deleted.')
        p('Deleting node... ',)
        ironic.node.delete(node.uuid)
        print('deleted.')
        p('Deleting chassis... ',)
        ironic.chassis.delete(chassis.uuid)
        print('deleted.')
    else:
        if port is not None:
            p('Node seems to already exist')
            return
        print('Adding to Ironic')
        create_node(ironic, args.ilo_username, args.ilo_password, args.ilo_address, mac, total_memory, total_cores)

if __name__ == '__main__':
    sys.exit(not main(sys.argv))
