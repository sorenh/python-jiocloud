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
import mock
import os
import StringIO
import unittest
from contextlib import nested
from jiocloud.apply_resources import ApplyResources

class TestApplyResources(unittest.TestCase):
    server_data = [('foo1_abc123', '93138146-2275-4e18-b41e-3957aa13e73a'),
                   ('foo2_abc124', '26af0276-83e1-4b68-870e-ff3250be8e8f'),
                   ('foo4_bc124', '677388b7-b5ac-418b-b671-6b930dc8003a'),
                   ('bar2', '381877b2-12c5-4831-95ed-1d7518bb7e8c'),
                   ('baz', '59e5dd8d-2063-4943-98de-df206e462849')]

    def setUp(self):
        super(TestApplyResources, self).setUp()
        os.environ['OS_USERNAME'] = 'os_username'
        os.environ['OS_PASSWORD'] = 'os_pasword'
        os.environ['OS_AUTH_URL'] = 'http://example.com/'
        os.environ['OS_TENANT_NAME'] = 'tenant_name'
        os.environ['OS_REGION_NAME'] = 'region_name'

    def fake_server_data(self, nova_client):
        def fake_server(name, uuid):
            s = mock.Mock()
            s.configure_mock(name=name, id=uuid)
            return s
        server_list = [fake_server(*s) for s in self.server_data]
        nova_client.servers.list.return_value = server_list

    def test_get_existing_servers(self):
        apply_resources = ApplyResources()
        with mock.patch.object(apply_resources, 'get_nova_client') as get_nova_client:
            nova_client = get_nova_client.return_value
            self.fake_server_data(nova_client)
            self.assertEquals(apply_resources.get_existing_servers(), [s[0] for s in self.server_data])
            self.assertEquals(apply_resources.get_existing_servers(project_tag='abc123'),
                              ['foo1_abc123'])
            self.assertEquals(apply_resources.get_existing_servers(project_tag='abc124'),
                              ['foo2_abc124'])
            self.assertEquals(apply_resources.get_existing_servers(project_tag='bc124'),
                              ['foo4_bc124'])
            self.assertEquals(apply_resources.get_existing_servers(project_tag='bc124', attr_name='id'),
                              ['677388b7-b5ac-418b-b671-6b930dc8003a'])

    def test_generate_desired_servers(self):
        apply_resources = ApplyResources()
        self.assertEquals(apply_resources.generate_desired_servers({'foo': {'number': 5 }}, project_tag='foo'),
                          [{'name': 'foo1_foo'},
                           {'name': 'foo2_foo'},
                           {'name': 'foo3_foo'},
                           {'name': 'foo4_foo'},
                           {'name': 'foo5_foo'},
                          ])
        self.assertEquals(apply_resources.generate_desired_servers({'foo': {'number': 5,
                                                                            'network': 'public'},
                                                                    'bar': {'number': 2,
                                                                            'network': 'private',
                                                                            'other': 'something'}},
                                                                    mappings={'network': {'private': 'mappedprivate'}},
                                                                    project_tag='foo'),
                          [{'name': 'foo1_foo', 'network': 'public'},
                           {'name': 'foo2_foo', 'network': 'public'},
                           {'name': 'foo3_foo', 'network': 'public'},
                           {'name': 'foo4_foo', 'network': 'public'},
                           {'name': 'foo5_foo', 'network': 'public'},
                           {'name': 'bar1_foo', 'network': 'mappedprivate', 'other': 'something'},
                           {'name': 'bar2_foo', 'network': 'mappedprivate', 'other': 'something'},
                           ])
        self.assertEquals(apply_resources.generate_desired_servers({'foo': {'number': 0},
                                                                    'bar': {'number': 2}}),
                          [{'name': 'bar1'},
                           {'name': 'bar2'}])
        self.assertEquals(apply_resources.generate_desired_servers({'foo': {'number': 0},
                                                                    'bar': {'number': 2}},
                                                                   number_overrides={'bar': 4}),
                          [{'name': 'bar1'},
                           {'name': 'bar2'},
                           {'name': 'bar3'},
                           {'name': 'bar4'}])

    def test_servers_to_create(self):
        apply_resources = ApplyResources()
        with mock.patch.multiple(apply_resources,
                                 get_nova_client=mock.DEFAULT,
                                 read_resources=mock.DEFAULT) as mocks:
            get_nova_client = mocks['get_nova_client']
            read_resources = mocks['read_resources']
            read_resources.return_value = {'foo': {'number': 1},
                                           'bar': {'number': 2}}

            nova_client = get_nova_client.return_value
            self.fake_server_data(nova_client)

            self.assertEquals(apply_resources.servers_to_create('fake_path'),
                              [{'name': 'foo1'},
                               {'name': 'bar1'}])

    def test_create_servers(self):
        apply_resources = ApplyResources()
        with nested(
               mock.patch('__builtin__.file'),
               mock.patch('time.sleep'),
               mock.patch.object(apply_resources, 'create_server'),
               mock.patch.object(apply_resources, 'get_nova_client')
            ) as (file_mock, sleep, create_server, get_nova_client):
            ids = [10,11,12]
            status = {10: ['ACTIVE', 'BUILD', 'BUILD'], 11: ['ACTIVE', 'BUILD'], 12: ['ACTIVE', 'BUILD']}

            servers = {}
            def fake_create_server(*args, **kwargs):
                server_id = ids.pop()
                servers[kwargs['name']] = server_id
                return server_id

            create_server.side_effect = fake_create_server
            self.add_floating_ip_called = False

            def server_get(id):
                mm = mock.MagicMock()
                if status[id]:
                    mm.status = status[id].pop()
                    mm.add_floating_ip.side_effect = Exception
                else:
                    def add_floating_ip(ip):
                        self.assertEquals(ip, '1.2.3.4')
                        self.add_floating_ip_called = True

                    mm.add_floating_ip.side_effect = add_floating_ip
                return mm

            get_nova_client.return_value.servers.get.side_effect = server_get
            get_nova_client.return_value.floating_ips.create.return_value.ip = '1.2.3.4'

            file_mock.side_effect = lambda f: StringIO.StringIO('test user data')

            apply_resources.create_servers([{'name': 'foo1', 'networks':  ['someid']},
                                            {'name': 'foo2', 'networks':  ['someid']},
                                            {'name': 'foo3', 'assign_floating_ip': True}
                                            ], 'somefile', 'somekey',0)

            create_server.assert_any_call(mock.ANY, 'somekey', name='foo1', networks=['someid'])
            create_server.assert_any_call(mock.ANY, 'somekey', name='foo2', networks=['someid'])
            create_server.assert_any_call(mock.ANY, 'somekey', name='foo3', assign_floating_ip=True)

            for call in create_server.call_args_list:
                self.assertEquals(call[0][0].read(), 'test user data')

            for s in status.values():
                self.assertEquals(s, [], 'create_servers stopped polling before server left BUILD state')
            self.assertTrue(self.add_floating_ip_called)
