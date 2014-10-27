#!/usr/bin/env python
import argparse
import os
import time
import utils
import yaml
from novaclient import client as novaclient

"""
Parses a specification of nodes to install and makes it so
"""

def get_nova_creds_from_env():
    d = {}
    d['username'] = os.environ['OS_USERNAME']
    d['api_key'] = os.environ['OS_PASSWORD']
    d['auth_url'] = os.environ['OS_AUTH_URL']
    d['project_id'] = os.environ['OS_TENANT_NAME']
    d['region_name'] = os.environ.get('OS_REGION_NAME')
    return d

class ApplyResources(object):
    def __init__(self):
        self.nova_client = None
        self._images = {}
        self._flavors = {}

    def read_resources(self, path):
        fp = file(path)
        return yaml.load(fp)['resources']

    def read_mappings(self, path):
        fp = file(path)
        return yaml.load(fp)

    def get_nova_client(self):
        if not self.nova_client:
            self.nova_client = novaclient.Client("1.1", **get_nova_creds_from_env())
        return self.nova_client

    def get_existing_servers(self, project_tag=None, attr_name='name'):
        """
        This method accepts an option project tag
        """
        # NOTE we should check for servers only in a certain state
        nova_client = self.get_nova_client()
        servers = nova_client.servers.list()
        if project_tag:
            servers = [elem for elem in servers if elem.name.endswith('_' + project_tag) ]
        return [getattr(s, attr_name) for s in servers]


    def generate_desired_servers(self, resources, mappings={}, project_tag=None):
        """
        Convert from a hash of servers resources to the
        hash of all server names that should be created
        """
        suffix = (project_tag and ('_' + project_tag)) or ''

        servers_to_create = []
        for k,v in resources.iteritems():
            for i in range(int(v['number'])):
                # NOTE ideally, this would not contain the caridinatlity
                # b/c it is not needed by this hash
                server = {'name': "%s%d%s" % (k, i+1, suffix)}
                for k_,v_ in v.iteritems():
                    if k_ == 'number':
                        continue
                    server[k_] = mappings.get(k_, {}).get(v_, v_)
                servers_to_create.append(server)
        return servers_to_create

    def servers_to_create(self, resource_file, mappings_file=None, project_tag=None):
        resources = self.read_resources(resource_file)
        mappings = mappings_file and self.read_mappings(mappings_file) or {}
        existing_servers = self.get_existing_servers(project_tag=project_tag)
        desired_servers = self.generate_desired_servers(resources, mappings, project_tag)
        return [elem for elem in desired_servers if elem['name'] not in existing_servers ]

    def create_servers(self, servers, userdata, key_name=None):

        ids = set()
        floating_ip_servers = set()
        for s in servers:
            userdata_file = file(userdata)
            server_id = self.create_server(userdata_file, key_name, **s)
            ids.add(server_id)

            if s.get('assign_floating_ip'):
                floating_ip_servers.add(server_id)

        nova_client = self.get_nova_client()

        done = set()
        while ids:
            time.sleep(5)
            for id in ids:
                instance = nova_client.servers.get(id)
                print "%s (%s): %s" % (instance.name, id, instance.status)
                if instance.status != 'BUILD':
                    done.add(id)
            ids = ids.difference(done)

        for server_id in floating_ip_servers:
            ip = nova_client.floating_ips.create()
            instance = nova_client.servers.get(server_id)
            print "Assigning %s to %s (%s)" % (ip.ip, instance.name, id)
            instance.add_floating_ip(ip.ip)


    def create_server(self,
                      userdata_file,
                      key_name,
                      name,
                      flavor,
                      image,
                      config_drive=False,
                      networks=None,
                      **keys):
        print "Creating server %s"%(name)
        nova_client = self.get_nova_client()
        self._images[image] = self._images.get(image, nova_client.images.get(image))
        self._flavors[flavor] = self._flavors.get(flavor, nova_client.flavors.get(flavor))
        net_list = networks and ([{'net-id': n} for n in networks])
        instance = nova_client.servers.create(
          name=name,
          image=self._images[image],
          flavor=self._flavors[flavor],
          nics=net_list,
          userdata=userdata_file,
          key_name=key_name,
          config_drive=config_drive,
        )

        return instance.id

    def delete_servers(self, project_tag):
        nova_client = self.get_nova_client()
        servers = self.get_existing_servers(project_tag=project_tag, attr_name='id')
        ip_to_server_map = {ip.instance_id: ip for ip in nova_client.floating_ips.list()}
        ips_to_delete = set()
        for uuid in servers:
            print "Deleting uuid: %s"%(uuid)
            server = nova_client.servers.get(uuid)
            if uuid in ip_to_server_map:
                ip = ip_to_server_map[uuid]
                server.remove_floating_ip(ip.ip)
                ips_to_delete.add(ip)
            server.delete()

        for ip in ips_to_delete:
            print "Deleting floating ip: %s" % (ip.ip,)
            ip.delete()

    def ssh_config(self, servers):
        out = ''
        bastions = filter(lambda s:s.get('assign_floating_ip', False), servers)
        if bastions:
            bastion = utils.get_ip_of_node(self.get_nova_client(), bastions[0]['name'])
        else:
            bastion = None

        out += 'StrictHostKeyChecking no\n'
        out += 'UserKnownHostsFile /dev/null\n'
        out += '\n'
        for s in servers:
            out += 'Host %s\n' % (s['name'],)
            ip = utils.get_ip_of_node(apply_resources.get_nova_client(),  s['name'])
            out += '    HostName %s\n' % (ip,)
            if not s.get('assign_floating_ip', False) and bastion:
                out += '    ProxyCommand ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null %%r@%s nc %%h %%p\n' % (bastion,)
            out += '\n'
        return out

if __name__ == '__main__':
    argparser = argparse.ArgumentParser()
    subparsers = argparser.add_subparsers(dest='action', help='Action to perform')

    apply_parser  = subparsers.add_parser('apply', help='Apply a resource file')
    apply_parser.add_argument('resource_file_path', help='Path to resource file')
    apply_parser.add_argument('userdata', help='Path of userdata to apply to all nodes')
    apply_parser.add_argument('--mappings', help='Path to mappings file')
    apply_parser.add_argument('--project_tag', help='Project tag')
    apply_parser.add_argument('--key_name', help='Name of key pair')

    delete_parser = subparsers.add_parser('delete', help='Delete a project')
    delete_parser.add_argument('project_tag', help='Id of project to delete')

    list_parser  = subparsers.add_parser('list', help='List servers described in resource file')
    list_parser.add_argument('resource_file_path', help='Path to resource file')
    list_parser.add_argument('--project_tag', help='Project tag')

    ssh_config_parser  = subparsers.add_parser('ssh_config', help='Generate ssh config to connect to servers')
    ssh_config_parser.add_argument('resource_file_path', help='Path to resource file')
    ssh_config_parser.add_argument('--mappings', help='Path to mappings file')
    ssh_config_parser.add_argument('--project_tag', help='Project tag')

    args = argparser.parse_args()
    if args.action == 'apply':
        apply_resources = ApplyResources()
        servers = apply_resources.servers_to_create(args.resource_file_path,
                                                    args.mappings,
                                                    project_tag=args.project_tag)
        apply_resources.create_servers(servers, args.userdata, key_name=args.key_name)
    elif args.action == 'delete':
        if not args.project_tag:
            argparser.error("Must set project tag when action is delete")
        ApplyResources().delete_servers(project_tag=args.project_tag)
    elif args.action == 'list':
        apply_resources = ApplyResources()
        resources = apply_resources.read_resources(args.resource_file_path)
        desired_servers = apply_resources.generate_desired_servers(resources, project_tag=args.project_tag)
        print '\n'.join([s['name'] for s in desired_servers])
    elif args.action == 'ssh_config':
        apply_resources = ApplyResources()
        resources = apply_resources.read_resources(args.resource_file_path)
        mappings = args.mappings and apply_resources.read_mappings(args.mappings) or {}
        servers = apply_resources.generate_desired_servers(resources, mappings, args.project_tag)
        print apply_resources.ssh_config(servers)
