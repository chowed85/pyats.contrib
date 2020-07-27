import os
import re
import sys
import time
import logging
import argparse
import ipaddress
from itertools import product
from collections import OrderedDict
from yaml import (YAMLError, safe_load)
from concurrent.futures import ThreadPoolExecutor


from genie.conf import Genie
from genie.testbed import load
from pyats.async_ import pcall
from genie.conf.base import Testbed, Device, Interface, Link
from genie.metaparser.util.exceptions import SchemaEmptyParserError

from .libs import testbed_manager
from .creator import TestbedCreator

log = logging.getLogger(__name__)

SUPPORTED_OS = ['nxos', 'iosxr', 'iosxe', 'ios']


class Topology(TestbedCreator):

    """ Topology class (TestbedCreator)

    Takes a yaml file given by argument testbed_file and attempts to connect
    to each device in the testbed and discover the devices connection using
    cdp and lldp and writes it to a new yaml file

    Args:
        testbed-file('str'): Mandatory argument - testbed-file
        config-discovery ('bool): if enabled the script will enable cdp and lldp on devices
                            and disable it afterwards default is false
        add-unconnected-interfaces('bool'): if enabled, script will add all interfaces to
                topology section of yaml file instead of just interfaces
                with active connections default is false
        exclude-network ('str'): list networks that won't be recorded by creator
                if found as part of a connection, default is that no ips
                will be excluded
                Example: <ipv4> <ipv4>
        exclude-interfaces ('str'):list interfaces that won't be recorded by creator
                if found as part of a connection, default is that no interfaces
                will be excluded
        only-links: Only find connections between already defined devices
                    in the testbed and will not discover new devices
                    default behavior is that device discovery will be done
        alias: takes argument in format device:alias device2:alias2 and
                and indicates which alias should be used to connect to the
                device first, default behavior has no prefered alias
        ssh_only: if True the script will only attempt to use ssh connections
                to connect to devices, default behavior is to use all connections
        timeout('int'): How long before connection and verification attempts time out.
                        default value is 10 seconds

    CLI Argument                                   |  Class Argument
    --------------------------------------------------------------------------------------------
    --testbed-file=value                           |  testbed_file=value
    --config-discovery                             |  config_discovery
    --add-unconnected-interfaces                   |  add_unconnected_interfaces=True
    --exclude-network='<ipv4> <ipv4>'              |  exclude_network='<ipv4> <ipv4>'  
    --exclude-interfaces='<interface> <interface>' |  exclude_interfaces='<interface> <interface>'
    --only-links                                   |  only_links=True
    --alias='<device>:<alias> <device>:<alias>'    |  alias='<device>:<alias> <device>:<alias>'
    --ssh_only                                     |  ssh_only=True
    --timeout=value                                |  timeout=value
    """

    def _init_arguments(self):
        """ Specifies the arguments for the creator.
        Returns:
            dict: Arguments for the creator.
        """
        self.alias_dict = {}
        return {
            'required': ['testbed_file'],
            'optional': {
                'config_discovery': False,
                'add_unconnected_interfaces': False,
                'exclude_network': '',
                'exclude_interfaces':'',
                'only_links': False,
                'timeout': 10,
                'alias': '',
                'ssh_only': False
            }
        }

    def _generate(self):
        """The _generate is called by the testbed creator - It starts here
        Takes testbed information and writes the topology information into
        a yaml file
        
        Returns:
            dict: The intermediate dictionary format of the testbed data.
        """

        # Load testbed file
        testbed = load(self._testbed_file)

        # Re-open the testbed file as yaml so we can read the 
        # connection password - so we can re-create the yaml
        # with these credential
        with open(self._testbed_file, 'r') as stream:
            try:
                testbed_yaml = safe_load(stream)
            except YAMLError as exc:
                log.error('error opening yaml file: {}'.format(exc))
        # Standardizing exclude networks
        exclude_networks = []
        for network in self._exclude_network.split():
            try:
                exclude_networks.append(ipaddress.ip_network(network))
            except Exception:
                raise Exception('IP range given {ip} is not valid'.format(ip=network))

        # take aliases entered by user and format it into dictionary
        for alias_mapping in self._alias.split():
           spli = alias_mapping.split(':')
           if len(spli) != 2:
               raise Exception('{} is not valid entry'.format(alias_mapping))
           self.alias_dict[spli[0]] = spli[1]

        dev_man = testbed_manager.TestbedManager(testbed, config=self._config_discovery,
                                                 ssh_only=self._ssh_only,
                                                 alias_dict=self.alias_dict,
                                                 timeout=self._timeout,
                                                 supported_os=SUPPORTED_OS)

        # Get the credential for the device from the yaml - so can recreate the
        # yaml with those
        credential_dict, proxy_set = dev_man.get_credentials_and_proxies(testbed_yaml)
        device_list = {}

        while len(testbed.devices) > len(dev_man.visited_devices):
            # connect to unvisited devices
            dev_man.connect_all_devices(len(testbed.devices))

            # Configure these connected devices
            if dev_man.config:
                dev_man.configure_testbed_cdp_protocol()
                dev_man.configure_testbed_lldp_protocol()

            # Get the cdp/lldp operation data and massage it into our structure format
            result = dev_man.get_neigbor_data()
            connections = self.process_neighbor_data(testbed, device_list,
                                                     exclude_networks, result)
            log.info('Connections found in current set of devices: {}'.format(connections))
            
            # Create new devices to add to testbed
            self._write_devices_into_testbed(device_list, proxy_set,
                                             credential_dict, testbed)

            # add the connections that were found to the topology
            self._write_connections_to_testbed(connections, testbed)
            if self._only_links:
                break
            log.info('Looping to check newly discovered devices')

        log.info('Finished writing connections')
        # get IP address for interfaces
        for device in testbed.devices.values():
            dev_man.get_interfaces_ipV4_address(device)

        # unconfigure cdp and lldp on devices that were configured by script
        pcall(dev_man.unconfigure_neighbor_discovery_protocols,
              device= testbed.devices.values()
        )

        # add the new information into testbed_yaml
        log.info('Creating dictionary based on testbed')
        final_yaml = self.create_yaml_dict(testbed, testbed_yaml, credential_dict)

        # return final topology
        log.info('topology discovered is: {}'.format(final_yaml['topology']))
        return final_yaml

    def process_neighbor_data(self, testbed, device_list, exclude_networks, result):
        '''
        Args:
            testbed: testbed of devices that will be visited
            device_list: list of device with information about how to
                            connect and their existing interfaces
            exclude_networks : range of ip addresses whose connections won't be logged in the yaml
            result:

        Returns:
            {device:{interface with connection:{'dest_host': destination device,
                                                'dest_port': destination device port}}}
        '''
        conn_dict = {}

        # process the connection data retrieved from getting cdp and lldp neighbors
        # and write it into a dictionary of format
        # {device:{interface with connection:{'dest_host': destination device,
        #                                     'dest_port': destination device port}}}
        for entry in result:
            for device in entry:
                conn_dict[device] = self.get_device_connections(entry[device],
                                                                device, 
                                                                device_list, 
                                                                exclude_networks, 
                                                                testbed)

        return conn_dict

    def get_device_connections(self, data, device_name, device_list, exclude_networks , testbed):
        '''Take a device from a testbed and find all the adjacent devices.
        First it processes the devices cdp information and writes it into the dict
        then it processes the lldp information and adds new data to the dict

        Args:
            data: dict containing devices cdp and lldp information
            device_name: the device whose connections are being processed
            device_list: list of device with information about how to
                            connect and their existing interfaces
            exclude_networks : range of ip addresses whose connections won't be logged in the yaml
            testbed: testbed of devices, used to check if found device is already in testbed or not
            
        Returns:
            Dictionary containing the connection info found on currently accesible
            devices
        '''
        device_connections = {}
        #interface_filter = re.compile(r'.+?(?=\d)')

        # parse cdp information
        result = data.get('cdp', [])
        log.info('cdp neighbor information: {}'.format(result))
        if result:
            self._process_cdp_information(result, device_name, device_list,
                                          exclude_networks , testbed, device_connections)

        # parse lldp information
        result = data.get('lldp', [])
        log.info('lldp neighbor information: {}'.format(result))
        if result and result['total_entries'] != 0:
            self._process_lldp_information(result, device_name, device_list,
                                           exclude_networks , testbed, device_connections)

        # add interfaces used in the connections of the device
        # TODO MAKE THIS UNECESSARY
        # for interface in device_connections:
        #     if interface not in testbed.devices[device_name].interfaces:
        #         type_name = interface_filter.match(interface)
        #         interface_a = Interface(interface,
        #                                 type = type_name[0].lower())
        #         interface_a.device = testbed.devices[device_name]
        # return device_connections

    def _process_cdp_information(self, result, device_name, device_list, exclude_networks ,
                                 testbed, device_connections):
        '''Process the cdp parser information and enters it into the
        device_connections and the device_list

        Args:
            Result: The parser result for show cdp neighbors
            device_name: the device who's parser information is being examined
            device_list: list of device with information about how to
                            connect and their existing interfaces
            exclude_networks : range of ip addresses whose connections won't be logged in the yaml
            testbed: testbed of devices, used to check if found device is already in testbed or not
            device_connections: Dictionary of connections to write info into
        '''
        # filter to strip out the domain name from the system name
        domain_filter = re.compile(r'^.*?(?P<hostname>[-\w]+)\s?')

        for index in result['index']:

            connection = result['index'][index]

            # filter the host name from the domain name
            dest_host = connection.get('system_name')
            if not dest_host:
                dest_host = connection.get('device_id')
            filtered_name = domain_filter.match(dest_host)
            if filtered_name:
                dest_host = filtered_name.groupdict()['hostname']

            # If only-links is enabled and the destination host is not in
            # the testbed, skip the connection
            if self._only_links and dest_host not in testbed.devices:
                log.info('Device {} does not exists in {}, skipping'.format(
                    dest_host, testbed.name))
                continue

            dest_port = connection['port_id']
            interface = connection['local_interface']

            # if the name of either interface in the connection is listed in the exclude_interfaces argument,
            # skip the connection and begin checking the next connection
            if interface in self._exclude_interfaces:
                log.info('connection interface {} is found in '
                        'exclude interface list, skipping connection'.format(
                            interface))
                continue
            if dest_port in self._exclude_interfaces:
                log.info('destination interface {} is found in '
                        'exclude interface list, skipping connection'.format(
                            dest_port))
                continue

            # get the managment and interface addresses for the neighboring device
            mgmt_address = connection.get('management_addresses')
            int_address = connection.get('interface_addresses')
            int_set = {ip for ip in int_address}
            mgmt_set = {ip for ip in mgmt_address}

            # if the ip addresses for the connection are in the range given
            # by the cli, do not log the connection and proceed to next entry
            stop = False
            for ip, net in product(int_set, exclude_networks ):
                if ipaddress.IPv4Address(ip) in net:
                    log.info('IP {ip} found in'
                            'ignored network {net}'.format(ip=ip, net=net))
                    stop = True
                    break
            for ip, net in product(mgmt_set, exclude_networks ):
                if ipaddress.IPv4Address(ip) in net:
                    log.info('IP {ip} found in'
                            'ignored network {net}'.format(ip=ip, net=net))
                    stop = True
                    break
            if stop:
                continue

            # get the name of the operating system of the destination device
            # and filter the host name from the domain name
            os = self.get_os(connection['software_version'],
                             connection['platform'])
            log.info('dest host = {}, dest port = {}, interface= {}, device = {}'.format(dest_host,dest_port, interface, device_name))

            # Add the connection information to the device_connections and destination device information to the device_list
            self.add_to_device_list(device_list, dest_host, dest_port, int_set, mgmt_set,
                                    os, device_name, interface)
            self.add_to_device_connections(device_connections, dest_host, dest_port, interface, device_name)

    def _process_lldp_information(self, result, device_name, device_list, exclude_networks , testbed, device_connections):
        '''Process the lldp parser information and enters it into the
        device_connections and the device_list

        Args:
            Result: The parser result for show lldp neighbors
            device_name: the device who's parser information is being examined
            device_list: list of device with information about how to
                            connect and their existing interfaces
            exclude_networks : range of ip addresses whose connections won't be logged in the yaml
            testbed: testbed of devices, used to check if found device is already in testbed or not
            device_connections: Dictionary of connections to write info into
        '''
        # filter to strip out the domain name from the system name
        # Ex. n77-1.cisco.com becomes n77-1
        domain_filter = re.compile(r'^.*?(?P<hostname>[-\w]+)\s?')

        for interface, connection in result['interfaces'].items():
            port_list = connection['port_id']
            for dest_port in port_list:

                # filter the host name from the domain name
                dest_host = list(port_list[dest_port]['neighbors'].keys())[0]
                filtered_name = domain_filter.match(dest_host)
                if filtered_name:
                    dest_host = filtered_name.groupdict()['hostname']

                # If only-links is enabled and the destination host is not in
                # the testbed, skip the connection
                if self._only_links and dest_host not in testbed.devices:
                    log.info('{} is not in initial testbed, '
                             'skipping connection'.format(dest_host))
                    continue

                # if the name of either interface in the connection is listed in the exclude_interfaces argument,
                # skip the connection and begin checking the next connection
                if interface in self._exclude_interfaces:
                    log.info('connection interface {} is found in '
                            'ignore interface list,'
                            ' skipping connection'.format(interface))
                    continue
                if dest_port in self._exclude_interfaces:
                    log.info('destination interface {} is found in '
                            'ignore interface list,'
                            ' skipping connection'.format(dest_port))
                    continue

                # get the managment addresses for the neighboring device
                neighbor = port_list[dest_port]['neighbors'][dest_host]
                ip_address = neighbor.get('management_address')
                if ip_address is None:
                    ip_address = neighbor.get('management_address_v4')

                # if the ip addresses for the connection are in the range given
                # by the cli, do not log the connection and move on
                if ip_address is not None and exclude_networks :
                    stop = False
                    for net in exclude_networks :
                        if ipaddress.IPv4Address(ip_address) in net:
                            log.info('IP {ip} found in ignored '
                                        'network {net}'.format(ip=ip_address,
                                                            net=net))
                            stop = True
                            break
                    if stop:
                        continue
                
                # get the name of the operating system of the destination device
                os = self.get_os(neighbor['system_description'], '')

                # Add the connection information to the device_connections and destination device information to the device_list
                self.add_to_device_list(device_list, dest_host, dest_port,
                                        set(), {ip_address}, os, device_name, interface)
                self.add_to_device_connections(device_connections, dest_host, dest_port, interface, device_name)

    def add_to_device_list(self, device_list, dest_host,
                           dest_port, int_address, mgmt_address, os, discover_name, interface):
        '''TEST: add host device interface info as well 
        Add the information needed to create the device in the
        testbed later to the specified list
        Args:
            device_list: list of device with information about how to
                            connect and their existing interfaces
            dest_host: device being added to the list
            dest_port: interface of device to be added
            int_address: set of ip addresses found under int ip header for device
            mgmt_address: set of ip addresses found under mgmt ip header for device
            os: the os of the device
            discover_name: the name of the device that discovered dest_host
        '''
        # if the interface addresses is the same as the mgmt adresses
        # assume the adress is a managment IP and remove the address
        # from the interface address set
        int_address.difference_update(mgmt_address)
        
        # if the device is not yet listed, write all traits intp the device list
        if dest_host not in device_list:
            device_list[dest_host] = {'ports': {dest_port},
                                    'ip':mgmt_address,
                                    'os': os,
                                    'finder': (discover_name, int_address),
                                    'new_device': True}
        
        # if the device already exists in the device list, add the new interfaces
        # and ip addresses to the list
        else:
            device_list[dest_host]['ports'].add(dest_port)
            if not device_list[dest_host]['new_device']:
                return
            if device_list[dest_host]['os'] is None:
                device_list[dest_host]['os'] = os
            device_list[dest_host]['ip'] = device_list[dest_host]['ip'].union(mgmt_address)
        
        # add new interfaces for discovering device
        if discover_name not in device_list:
            device_list[discover_name] = {'ports' : {interface},
                                          'new_device': False}
        else:
            device_list[dest_host]['ports'].add(interface)

    def add_to_device_connections(self, device_connections,
                               dest_host, dest_port,interface, dev):
        '''Adds the information about a connection to be added to the topology
        recording what device interface combo is connected to the given
        interface and ip address involved in the connection
        
        Args:
            device_connections: Dictionary of connections to write info into
            dest_host: device at other end of connection
            dest_port: interface used by dest_host in connection
            interface: interface of device used in connection
            dev: the device involved in the connection
        '''
        new_entry = {'dest_host': dest_host,
                    'dest_port': dest_port}

        # if interface is not yet in the connection dict,
        # add it to the dictionary
        if interface not in device_connections:
            device_connections[interface] = [new_entry]
            log.info('Connection device {} interface {} to'
                    ' device {} interface {} found'.format(dev,
                                                        interface,
                                                        dest_host,
                                                        dest_port))
        
        # if the interface is already in the connection dict,
        # verify that the connection is unique before adding to dict
        else:
            for entry in device_connections[interface]:

                # check that the connection being added is unique
                if (entry['dest_host'] == dest_host
                        and entry['dest_port'] == dest_port):
                    break
            else:
                log.info('Connection device {} interface {} to'
                         ' device {} interface {} found'.format(dev,
                                                                interface,
                                                                dest_host,
                                                                dest_port))
                device_connections[interface].append(new_entry)

    def get_os(self, system_string, platform_name):
        '''Get the os from the system_description output from the show
        cdp and show lldp neighbor parsers

        Args:
            system_string: posible location for os name
            platform_name: possible location for os name
        Return:
            returns os or None
        '''
        if 'IOS' in system_string or 'IOS' in platform_name:
            if 'XE' in system_string or 'XE' in platform_name:
                return 'iosxe'
            elif 'XR' in system_string or 'XR' in platform_name:
                return 'iosxr'
            else:
                return 'ios'
        if 'NX-OS' in system_string or 'NX-OS' in platform_name:
            return 'nxos'

    

    def _write_devices_into_testbed(self, device_list, proxy_set, credential_dict, testbed):
        ''' Writes any new devices found in the device list into the testbed
        and adds any missing interfaces into devices that are missing it

        Args:
            device_list: list of devices and attached interfaces to add to testbed
            proxy_set: list of proxies used by other devices in testbed
            credential_dict: Dictionary of credentials used by other devices in testbed
            testbed: testbed to add devices too

        Returns:
            Dictionary of new device objects to add to testbed
        '''
        # filter to strip out the numbers from an interface to create a type name
        # example: ethernet0/3 becomes ethernet
        interface_filter = re.compile(r'.+?(?=\d)')

        for device_name in device_list:
            # if the device is not in the testbed
            if device_name not in testbed.devices:
                log.info('New device {} found and'
                         ' being added to testbed'.format(device_name))
                new_dev = self.create_new_device(testbed, device_list[device_name], proxy_set, device_name)
                testbed.add_device(new_dev)
                device_list[device_name]['new_device'] = False
                continue

            # if the device is already in the testbed, check if the interface exists or not
            if not self._add_unconnected_interfaces:
                for interface in device_list[device_name]['ports']:

                    #if interface does not exist add it to the testbed
                    if interface not in testbed.devices[device_name].interfaces:
                        type_name = interface_filter.match(interface)
                        interface_a = Interface(interface,
                                                type=type_name[0].lower())
                        interface_a.device = testbed.devices[device_name]
                continue

            # get all interfaces and add them to testbed
            interface_list = testbed.devices[device_name].parse('show interfaces description')
            for interface in interface_list['interfaces']:
                if interface not in testbed.devices[device_name].interfaces:
                    type_name = interface_filter.match(interface)
                    interface_a = Interface(interface,
                                            type=type_name[0].lower())
                    interface_a.device = testbed.devices[device_name]

    def create_new_device(self, testbed, device_data, proxy_set, device_name):
        '''Create a new device object based on given data to add to testbed

        Args:
            testbed: testbed that new device will be added to
            device_data: information about device to be created
            proxy_set: set of proxies used by other devices in testbed
            device_name: name of device that is being created

        Returns:
            new device object to be added to testbed
        '''
        # filter to strip out the numbers from an interface to create a type name
        # example: ethernet0/3 becomes ethernet
        interface_filter = re.compile(r'.+?(?=\d)')

        connections = {}
        # get credentials of finder device to use as new device credentials
        finder = device_data['finder']
        finder_dev = testbed.devices[finder[0]]
        credentials = finder_dev.credentials

        # create connections for the managment addresses in the device list
        for ip in device_data['ip']:
            for proxy in proxy_set:
                # create connection using possible proxies
                connections['ssh' + proxy] = {
                    'protocol': 'ssh',
                    'ip': ip,
                    'proxy': proxy
                    }

        # if there is an interface ip, create a proxy connection
        # using the discovery device
        if device_data['finder'][1]:
            for ip in device_data['finder'][1]:
                finder_proxy = self.write_proxy_chain(
                                finder[0], testbed, credentials, ip)
                connections['finder_proxy'] = {
                        'protocol': 'ssh',
                        'ip': ip,
                        'proxy': finder_proxy
                        }
                    
        # create the new device
        dev_obj = Device(device_name,
                    os=device_data['os'],
                    credentials=credentials,
                    type='device',
                    connections=connections,
                    custom={'abstraction':
                                {'order':['os'],
                                'os': device_data['os']}
                            })

        # create and add the interfaces for the new device
        for interface in device_data['ports']:
            type_name = interface_filter.match(interface)
            interface_a = Interface(interface,
                                    type=type_name[0].lower())
            interface_a.device = dev_obj
        return dev_obj

    def write_proxy_chain(self, finder_name, testbed, credentials, ip):
        '''creates a set of proxies for ssh connections, creating a set of
        commands if there are mutiple proxies involved

        Args:
            finder_name = name of device being used as proxy
            testbed = testbed where device is found
            credentials = credentials of finder device
            ip = interface ip used in connection
        
        Returns:
            Either a simple proxy or a list of proxy commands to use in
            connecting to targe device
        '''
        # get finder_device object and its default user name
        finder_device = testbed.devices[finder_name]
        user = credentials['default']['username']

        # Search for an ssh connection to use and see if it has 
        # existing proxy information
        for conn in finder_device.connections:
            if conn == 'defaults':
                continue
            connection_detail = finder_device.connections[conn]
            if connection_detail.protocol =='ssh' and 'proxy' in connection_detail:
                new_proxy = connection_detail.proxy
                conn_ip = connection_detail.ip
                break
        else:
            new_proxy = None

        # if there is no proxy found, create a simple one proxy connection
        if new_proxy is None:
            return finder_name
        
        # if the proxy information is a list of proxy commands, append necessary extra proxy commands to list
        if isinstance(new_proxy, list):
            new_proxy[-1]['command'] = 'ssh {user}@{ip}'.format(user=user, ip=conn_ip)
            new_proxy.append({'device': finder_name, 'command': 'ssh {user}@{ip}'.format(user=user, ip=ip)})
            return new_proxy
        
        # if the proxy information found is a simple proxy connection, create a set of proxy commands to use
        if isinstance(new_proxy,str):
            proxy_steps = [{'device':new_proxy,'command':'ssh {}'.format(conn_ip)},
                           {'device':finder_name, 'command': 'ssh {user}@{ip}'.format(user=user, ip=ip)}]
            return proxy_steps        

    def _write_connections_to_testbed(self, connection_dict, testbed):
        '''Writes the connections found in the connection_dict into the testbed

        Args:
            connection_dict: Dictionary with connections found earlier
            testbed: testbed to write connections into
        '''
        log.info('Adding connections to testbed')
        for device in connection_dict:
            log.info('Writing connections found in {}'.format(device))
            for interface_name in connection_dict[device]:

                # get the interface found in the connection on the device searched
                interface = testbed.devices[device].interfaces[interface_name]

                # if the interface is not already part of a link get a list of
                # all interfaces involved in the link and create a new link
                # object with the associated interfaces
                if interface.link is None:
                    int_list = [interface]
                    for entry in connection_dict[device][interface_name]:
                        dev = entry['dest_host']
                        dest_int = entry['dest_port']
                        if testbed.devices[dev].interfaces[dest_int] not in int_list:
                            int_list.append(testbed.devices[dev].interfaces[dest_int])
                    link = Link('Link_{num}'.format(num=len(testbed.links)),
                                interfaces=int_list)

                # if the interface is already part of the link go over the
                # other interfaces found in the connection_dict and add them to the link
                # if they are not there already
                else:
                    link = interface.link
                    for entry in connection_dict[device][interface_name]:
                        dev = entry['dest_host']
                        dest_int = entry['dest_port']
                        if testbed.devices[dev].interfaces[dest_int] not in link.interfaces:
                            link.connect_interface(testbed.devices[dev].interfaces[dest_int])

    def create_yaml_dict(self, testbed, testbed_yaml, credential_dict):
        '''Integrate the new information added to the testbed 
        into the testbed_yaml file

        Args:
            testbed: testbed whose devices and connections are added
            testbed_yaml: exisiting yaml file that will have the new data added to it
            credential_dict: dictionary of device credentials

        '''
        log.info('Creating dictionary based on testbed')
        yaml_dict = {'topology': {}}

        for device in testbed.devices.values():
            # write new devices into dict
            if device.name not in testbed_yaml['devices']:
                log.info('Adding device info for {}'.format(device.name))
                testbed_yaml['devices'][device.name] = {'type': device.type,
                                                    'os': device.os,
                                                    'credentials': credential_dict,
                                                    'connections': {}
                                                    }
                conn_dict = testbed_yaml['devices'][device.name]['connections']
                for connect in device.connections:
                    ip = device.connections[connect].get('ip')
                    protocol = device.connections[connect].get('protocol')
                    proxy = device.connections[connect].get('proxy')
                    conn_dict[connect] = {'protocol':protocol,
                                            'ip': ip,
                                          'proxy': proxy
                                         }
                conn_dict['defaults'] = {'via':connect}

            # write in the interfaces and link from devices into testbed
            interface_dict = {'interfaces': {}}
            log.info('Adding interface info for {}'.format(device.name))
            for interface in device.interfaces.values():
                interface_dict['interfaces'][interface.name] = {'type': interface.type}
                if interface.link is not None:
                    interface_dict['interfaces'][interface.name]['link'] = interface.link.name
                if interface.ipv4 is not None:
                    interface_dict['interfaces'][interface.name]['ipv4'] = str(interface.ipv4)

            # add interface information into the topology part of yaml_dict
            yaml_dict['topology'][device.name] = interface_dict
        log.info('topology discovered is: {}'.format(yaml_dict['topology']))

        # combine and add the new information to existing yaml file
        log.info('combining existing testbed with new topology')

        # if yaml file has no topology info, add yaml_dict topology
        # directly to file
        if testbed_yaml.get('topology') is None:
            testbed_yaml['topology'] = yaml_dict['topology']
            return testbed_yaml

        #if testbed has exsiting topology only add new or changed information
        for device in yaml_dict['topology']:
            if device not in testbed_yaml['topology']:
                testbed_yaml['topology'][device] = yaml_dict['topology'][device]
            else:
                testbed_yaml['topology'][device]['interfaces'].update(yaml_dict['topology'][device]['interfaces'])
        return testbed_yaml

