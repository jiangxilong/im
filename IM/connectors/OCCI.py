# IM - Infrastructure Manager
# Copyright (C) 2011 - GRyCAP - Universitat Politecnica de Valencia
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import time
from ssl import SSLError
import json
import os
import re
import base64
import string
import httplib
import tempfile
from IM.uriparse import uriparse
from IM.VirtualMachine import VirtualMachine
from CloudConnector import CloudConnector
from radl.radl import Feature
from netaddr import IPNetwork, IPAddress
from IM.config import Config


class OCCICloudConnector(CloudConnector):
    """
    Cloud Launcher to the OCCI platform (FedCloud)
    """

    type = "OCCI"
    """str with the name of the provider."""
    INSTANCE_TYPE = 'small'
    """str with the name of the default instance type to launch."""
    DEFAULT_USER = 'cloudadm'
    """ default user to SSH access the VM """

    VM_STATE_MAP = {
        'waiting': VirtualMachine.PENDING,
        'active': VirtualMachine.RUNNING,
        'inactive': VirtualMachine.OFF,
        'error': VirtualMachine.FAILED,
        'suspended': VirtualMachine.STOPPED
    }
    """Dictionary with a map with the OCCI VM states to the IM states."""

    def get_https_connection(self, auth, server, port):
        """
        Get a HTTPS connection with the specified server.
        It uses a proxy file if it has been specified in the auth credentials
        """
        # disable SSL certificate validation
        import ssl
        if hasattr(ssl, '_create_unverified_context'):
            ssl._create_default_https_context = ssl._create_unverified_context

        if auth and 'proxy' in auth:
            proxy = auth['proxy']

            (fproxy, proxy_filename) = tempfile.mkstemp()
            os.write(fproxy, proxy)
            os.close(fproxy)

            return httplib.HTTPSConnection(server, port, cert_file=proxy_filename)
        else:
            return httplib.HTTPSConnection(server, port)

    def get_http_connection(self, auth_data):
        """
        Get the HTTP connection to contact the OCCI server
        """
        auths = auth_data.getAuthInfo(self.type, self.cloud.server)
        if not auths:
            raise Exception("No correct auth data has been specified to OCCI.")
        else:
            auth = auths[0]

        if self.cloud.protocol == 'https':
            conn = self.get_https_connection(
                auth, self.cloud.server, self.cloud.port)
        else:
            conn = httplib.HTTPConnection(self.cloud.server, self.cloud.port)

        return conn

    @staticmethod
    def delete_proxy(conn):
        """
        Delete the proxy file created to contact with the HTTPS server.
        (Created in the get_https_connection function)
        """
        if isinstance(conn, httplib.HTTPSConnection) and conn.cert_file and os.path.isfile(conn.cert_file):
            os.unlink(conn.cert_file)

    def get_auth_header(self, auth_data):
        """
        Generate the auth header needed to contact with the OCCI server.
        I supports Keystone tokens and basic auth.
        """
        auths = auth_data.getAuthInfo(self.type, self.cloud.server)
        if not auths:
            self.logger.error(
                "No correct auth data has been specified to OCCI.")
            return None
        else:
            auth = auths[0]

        auth_header = None
        keystone_uri = KeyStoneAuth.get_keystone_uri(self, auth_data)

        if keystone_uri:
            # TODO: Check validity of token
            keystone_token = KeyStoneAuth.get_keystone_token(
                self, keystone_uri, auth)
            auth_header = {'X-Auth-Token': keystone_token}
        else:
            if 'username' in auth and 'password' in auth:
                passwd = auth['password']
                user = auth['username']
                auth_header = {'Authorization': 'Basic ' +
                               string.strip(base64.encodestring(user + ':' + passwd))}

        return auth_header

    def concreteSystem(self, radl_system, auth_data):
        image_urls = radl_system.getValue("disk.0.image.url")
        if not image_urls:
            return [radl_system.clone()]
        else:
            if not isinstance(image_urls, list):
                image_urls = [image_urls]

            res = []
            for str_url in image_urls:
                url = uriparse(str_url)
                protocol = url[0]
                cloud_url = self.cloud.protocol + "://" + self.cloud.server + ":" + str(self.cloud.port)
                if (protocol in ['https', 'http'] and url[2] and url[0] + "://" + url[1] == cloud_url):
                    res_system = radl_system.clone()

                    res_system.getFeature("cpu.count").operator = "="
                    res_system.getFeature("memory.size").operator = "="

                    res_system.addFeature(
                        Feature("disk.0.image.url", "=", str_url), conflict="other", missing="other")

                    res_system.addFeature(
                        Feature("provider.type", "=", self.type), conflict="other", missing="other")
                    res_system.addFeature(Feature(
                        "provider.host", "=", self.cloud.server), conflict="other", missing="other")
                    res_system.addFeature(Feature(
                        "provider.port", "=", self.cloud.port), conflict="other", missing="other")

                    username = res_system.getValue(
                        'disk.0.os.credentials.username')
                    if not username:
                        res_system.setValue(
                            'disk.0.os.credentials.username', self.DEFAULT_USER)

                    res.append(res_system)

            return res

    def get_attached_volumes_from_info(self, occi_res):
        """
        Get the attached volumes in VM from the OCCI information returned by the server
        """
        # Link:
        # </storage/0>;rel="http://schemas.ogf.org/occi/infrastructure#storage";self="/link/storagelink/compute_10_disk_0";category="http://schemas.ogf.org/occi/infrastructure#storagelink
        # http://opennebula.org/occi/infrastructure#storagelink";occi.core.id="compute_10_disk_0";occi.core.title="ttylinux
        # -
        # kvm_file0";occi.core.target="/storage/0";occi.core.source="/compute/10";occi.storagelink.deviceid="/dev/hda";occi.storagelink.state="active"
        lines = occi_res.split("\n")
        res = []
        for l in lines:
            if l.find('Link:') != -1 and l.find('/storage/') != -1:
                num_link = None
                num_storage = None
                device = None
                parts = l.split(';')
                for part in parts:
                    kv = part.split('=')
                    if kv[0].strip() == "self":
                        num_link = kv[1].strip('"')
                    elif kv[0].strip() == "occi.storagelink.deviceid":
                        device = kv[1].strip('"')
                    elif kv[0].strip() == "occi.core.target":
                        num_storage = kv[1].strip('"')
                if num_link and num_storage:
                    res.append((num_link, num_storage, device))
        return res

    def get_net_info(self, occi_res):
        """
        Get the net related information about a VM from the OCCI information returned by the server
        """
        # Link:
        # </network/1>;rel="http://schemas.ogf.org/occi/infrastructure#network";self="/link/networkinterface/compute_10_nic_0";category="http://schemas.ogf.org/occi/infrastructure#networkinterface
        # http://schemas.ogf.org/occi/infrastructure/networkinterface#ipnetworkinterface
        # http://opennebula.org/occi/infrastructure#networkinterface";occi.core.id="compute_10_nic_0";occi.core.title="private";occi.core.target="/network/1";occi.core.source="/compute/10";occi.networkinterface.interface="eth0";occi.networkinterface.mac="10:00:00:00:00:05";occi.networkinterface.state="active";occi.networkinterface.address="10.100.1.5";org.opennebula.networkinterface.bridge="br1"
        lines = occi_res.split("\n")
        res = []
        link_to_public = False
        for l in lines:
            if l.find('Link:') != -1 and l.find('/network/public') != -1:
                link_to_public = True
            if l.find('Link:') != -1 and l.find('/network/') != -1:
                num_interface = None
                ip_address = None
                parts = l.split(';')
                for part in parts:
                    kv = part.split('=')
                    if kv[0].strip() == "occi.networkinterface.address":
                        ip_address = kv[1].strip('"')
                        is_private = any([IPAddress(ip_address) in IPNetwork(
                            mask) for mask in Config.PRIVATE_NET_MASKS])
                    elif kv[0].strip() == "occi.networkinterface.interface":
                        net_interface = kv[1].strip('"')
                        num_interface = re.findall('\d+', net_interface)[0]
                if num_interface and ip_address:
                    res.append((num_interface, ip_address, not is_private))
        return link_to_public, res

    def setIPs(self, vm, occi_res, auth_data):
        """
        Set to the VM info the IPs obtained from the OCCI info
        """
        public_ips = []
        private_ips = []

        link_to_public, addresses = self.get_net_info(occi_res)
        for _, ip_address, is_public in addresses:
            if is_public:
                public_ips.append(ip_address)
            else:
                private_ips.append(ip_address)

        if (vm.state == VirtualMachine.RUNNING and not link_to_public and
                not public_ips and vm.requested_radl.hasPublicNet(vm.info.systems[0].name)):
            self.logger.debug("The VM does not have public IP trying to add one.")
            success, _ = self.add_public_ip(vm, auth_data)
            if not success:
                # in some sites the network is called floating instead of public
                success, _ = self.add_public_ip(vm, auth_data, network_name="floating")

            if success:
                self.logger.debug("Public IP successfully added.")

        vm.setIps(public_ips, private_ips)

    def get_property_from_category(self, occi_res, category, prop_name):
        """
        Get a property of an OCCI category returned by an OCCI server
        """
        lines = occi_res.split("\n")
        for l in lines:
            if l.find('Category: ' + category + ';') != -1:
                for elem in l.split(';'):
                    kv = elem.split('=')
                    if len(kv) == 2:
                        key = kv[0].strip()
                        value = kv[1].strip('"')
                        if key == prop_name:
                            return value
        return None

    def add_public_ip(self, vm, auth_data, network_name="public"):
        occi_info = self.query_occi(auth_data)
        url = self.get_property_from_category(occi_info, "networkinterface", "location")
        if not url:
            self.logger.error("No location for networkinterface category.")
            return (False, "No location for networkinterface category.")

        auth_header = self.get_auth_header(auth_data)
        conn = None
        try:
            conn = self.get_http_connection(auth_data)
            conn.putrequest('POST', url)
            if auth_header:
                conn.putheader(auth_header.keys()[0], auth_header.values()[0])
            conn.putheader('Accept', 'text/plain')
            conn.putheader('Content-Type', 'text/plain,text/occi')
            conn.putheader('Connection', 'close')

            net_id = "imnet." + str(int(time.time() * 100))

            body = ('Category: networkinterface;scheme="http://schemas.ogf.org/occi/infrastructure#";class="kind";'
                    'location="%s/link/networkinterface/";title="networkinterface link"\n' % self.cloud.path)
            body += 'X-OCCI-Attribute: occi.core.id="%s"\n' % net_id
            body += 'X-OCCI-Attribute: occi.core.target="%s/network/%s"\n' % (self.cloud.path, network_name)
            body += 'X-OCCI-Attribute: occi.core.source="%s/compute/%s"' % (self.cloud.path, vm.id)
            conn.putheader('Content-Length', len(body))
            conn.endheaders(body)

            resp = conn.getresponse()
            output = str(resp.read())
            if resp.status != 201 and resp.status != 200:
                self.logger.warn("Error adding public IP the VM: " + resp.reason + "\n" + output)
                return (False, "Error adding public IP the VM: " + resp.reason + "\n" + output)
            else:
                return (True, vm.id)
        except Exception:
            self.logger.exception("Error connecting with OCCI server")
            return (False, "Error connecting with OCCI server")
        finally:
            self.delete_proxy(conn)

    def get_occi_attribute_value(self, occi_res, attr_name):
        """
        Get the value of an OCCI attribute returned by an OCCI server
        """
        lines = occi_res.split("\n")
        for l in lines:
            if l.find('X-OCCI-Attribute: ' + attr_name + '=') != -1:
                return l.split('=')[1].strip('"')
        return None

    def updateVMInfo(self, vm, auth_data):
        auth = self.get_auth_header(auth_data)
        headers = {'Accept': 'text/plain', 'Connection': 'close'}
        if auth:
            headers.update(auth)
        conn = None
        try:
            conn = self.get_http_connection(auth_data)
            conn.request('GET', self.cloud.path +
                         "/compute/" + vm.id, headers=headers)
            resp = conn.getresponse()

            output = resp.read()
            if resp.status == 404 or resp.status == 204:
                vm.state = VirtualMachine.OFF
                return (True, vm)
            elif resp.status != 200:
                return (False, resp.reason + "\n" + output)
            else:
                old_state = vm.state
                occi_state = self.get_occi_attribute_value(output, 'occi.compute.state')

                occi_name = self.get_occi_attribute_value(output, 'occi.core.title')
                if occi_name:
                    vm.info.systems[0].setValue('instance_name', occi_name)

                # I have to do that because OCCI returns 'inactive' when a VM is starting
                # to distinguish from the OFF state
                if old_state == VirtualMachine.PENDING and occi_state == 'inactive':
                    vm.state = VirtualMachine.PENDING
                else:
                    vm.state = self.VM_STATE_MAP.get(occi_state, VirtualMachine.UNKNOWN)

                cores = self.get_occi_attribute_value(output, 'occi.compute.cores')
                if cores:
                    vm.info.systems[0].setValue("cpu.count", int(cores))
                memory = self.get_occi_attribute_value(output, 'occi.compute.memory')
                if memory:
                    vm.info.systems[0].setValue("memory.size", float(memory), 'G')

                console_vnc = self.get_occi_attribute_value(output, 'org.openstack.compute.console.vnc')
                if console_vnc:
                    vm.info.systems[0].setValue("console_vnc", console_vnc)

                # Update the network data
                self.setIPs(vm, output, auth_data)

                # Update disks data
                self.set_disk_info(vm, output, auth_data)
                return (True, vm)

        except Exception, ex:
            self.logger.exception("Error connecting with OCCI server")
            return (False, "Error connecting with OCCI server: " + str(ex))
        finally:
            self.delete_proxy(conn)

    def set_disk_info(self, vm, occi_res, auth_data):
        """
        Update the disks info with the actual device assigned by OCCI
        """
        system = vm.info.systems[0]

        for _, num_storage, device in self.get_attached_volumes_from_info(occi_res):
            cont = 1
            while system.getValue("disk." + str(cont) + ".size") and device:
                if os.path.basename(num_storage) == system.getValue("disk." + str(cont) + ".provider_id"):
                    system.setValue("disk." + str(cont) + ".device", device)
                cont += 1

    def gen_cloud_config(self, public_key, user=None, cloud_config_str=None):
        """
        Generate the cloud-config file to be used in the user_data of the OCCI VM
        """
        if not user:
            user = self.DEFAULT_USER
        config = """#cloud-config
users:
  - name: %s
    sudo: ALL=(ALL) NOPASSWD:ALL
    lock-passwd: true
    ssh-import-id: %s
    ssh-authorized-keys:
      - %s
""" % (user, user, public_key)
        if cloud_config_str:
            config += "\n%s\n\n" % cloud_config_str.replace("\\n", "\n")
        return config

    def query_occi(self, auth_data):
        """
        Get the info contacting with the OCCI server
        """
        auth = self.get_auth_header(auth_data)
        headers = {'Accept': 'text/plain', 'Connection': 'close'}
        if auth:
            headers.update(auth)
        conn = None
        try:
            conn = self.get_http_connection(auth_data)
            conn.request('GET', self.cloud.path + "/-/", headers=headers)
            resp = conn.getresponse()

            output = resp.read()
            # self.logger.debug(output)

            if resp.status != 200:
                self.logger.error("Error querying the OCCI server")
                return ""
            else:
                return output
        except:
            self.logger.exception("Error querying the OCCI server")
            return ""
        finally:
            self.delete_proxy(conn)

    def get_scheme(self, occi_info, category, ctype):
        """
        Get the scheme of an OCCI category contacting with the OCCI server
        """
        lines = occi_info.split("\n")
        for l in lines:
            if l.find('Category: ' + category) != -1 and l.find(ctype) != -1:
                parts = l.split(';')
                for p in parts:
                    kv = p.split("=")
                    if kv[0].strip() == "scheme":
                        return kv[1].replace('"', '').replace("'", '')

        self.logger.error("Error getting scheme for category: " + category)
        return ""

    def get_instance_type_uri(self, occi_info, instance_type):
        """
        Get the whole URI of an OCCI instance from the OCCI info
        """
        if instance_type.startswith('http'):
            # If the user set the whole uri, do not search
            return instance_type
        else:
            return self.get_scheme(occi_info, instance_type, 'resource_tpl') + instance_type

    def get_os_tpl_scheme(self, occi_info, os_tpl):
        """
        Get the whole URI of an OCCI os template from the OCCI info
        """
        return self.get_scheme(occi_info, os_tpl, 'os_tpl')

    def get_cloud_init_data(self, radl):
        """
        Get the cloud init data specified by the user in the RADL
        """
        configure_name = None
        if radl.contextualize.items:
            system_name = radl.systems[0].name

            for item in radl.contextualize.items.values():
                if item.system == system_name and item.get_ctxt_tool() == "cloud_init":
                    configure_name = item.configure

        if configure_name:
            return radl.get_configure_by_name(configure_name).recipes
        else:
            return None

    def create_volumes(self, system, auth_data):
        """
        Attach the required volumes (in the RADL) to the launched instance

        Arguments:
           - instance(:py:class:`boto.ec2.instance`): object to connect to EC2 instance.
           - vm(:py:class:`IM.VirtualMachine`): VM information.
        """
        volumes = []
        cont = 1
        while system.getValue("disk." + str(cont) + ".size"):
            disk_size = system.getFeature(
                "disk." + str(cont) + ".size").getValue('G')
            disk_device = system.getValue("disk." + str(cont) + ".device")
            if disk_device:
                # get the last letter and use vd
                disk_device = "vd" + disk_device[-1]
                system.setValue("disk." + str(cont) + ".device", disk_device)
            self.logger.debug("Creating a %d GB volume for the disk %d" % (int(disk_size), cont))
            storage_name = "im-disk-" + str(int(time.time() * 100))
            success, volume_id = self.create_volume(int(disk_size), storage_name, auth_data)
            if success:
                self.logger.debug("Volume id %s sucessfully created." % volume_id)
                volumes.append((disk_device, volume_id))
                system.setValue("disk." + str(cont) + ".provider_id", volume_id)
                # TODO: get the actual device_id from OCCI

                # let's wait the storage to be ready "online"
                wait_ok = self.wait_volume_state(volume_id, auth_data)
                if not wait_ok:
                    self.logger.error("Error waiting volume %s. Deleting it." % volume_id)
                    self.delete_volume(volume_id, auth_data)
            else:
                self.logger.error("Error creating volume: %s" % volume_id)

            cont += 1

        return volumes

    def wait_volume_state(self, volume_id, auth_data, wait_state="online", timeout=180, delay=5):
        """
        Wait a storage to be in the specified state (by default "online")
        """
        wait = 0
        online = False
        while not online and wait < timeout:
            success, storage_info = self.get_volume_info(volume_id, auth_data)
            state = self.get_occi_attribute_value(storage_info, 'occi.storage.state')
            self.logger.debug("Waiting volume %s to be %s. Current state: %s" % (volume_id, wait_state, state))
            if success and state == wait_state:
                online = True
            elif not success:
                self.logger.error("Error waiting volume %s to be ready: %s" % (volume_id, state))
                return False
            if not state == wait_state:
                time.sleep(delay)
                wait += delay

        return online

    def get_volume_info(self, storage_id, auth_data):
        """
        Get the OCCI info about the storage
        """
        auth = self.get_auth_header(auth_data)
        headers = {'Accept': 'text/plain', 'Connection': 'close'}
        if auth:
            headers.update(auth)
        conn = None
        try:
            conn = self.get_http_connection(auth_data)
            conn.request('GET', self.cloud.path +
                         "/storage/" + storage_id, headers=headers)
            resp = conn.getresponse()

            output = resp.read()
            if resp.status == 404 or resp.status == 204:
                return (False, "Volume not found.")
            elif resp.status != 200:
                return (False, resp.reason + "\n" + output)
            else:
                return (True, output)
        except Exception, ex:
            self.logger.exception("Error getting volume info")
            return False, str(ex)
        finally:
            self.delete_proxy(conn)

    def create_volume(self, size, name, auth_data):
        """
        Creates a volume of the specified data (in GB)

        returns the OCCI ID of the storage object
        """
        conn = None
        try:
            auth_header = self.get_auth_header(auth_data)

            conn = self.get_http_connection(auth_data)

            conn.putrequest('POST', self.cloud.path + "/storage/")
            if auth_header:
                conn.putheader(auth_header.keys()[0], auth_header.values()[0])
            conn.putheader('Accept', 'text/plain')
            conn.putheader('Content-Type', 'text/plain')
            conn.putheader('Connection', 'close')

            body = 'Category: storage; scheme="http://schemas.ogf.org/occi/infrastructure#"; class="kind"\n'
            body += 'X-OCCI-Attribute: occi.core.title="%s"\n' % name
            body += 'X-OCCI-Attribute: occi.storage.size=%d\n' % int(size)

            conn.putheader('Content-Length', len(body))
            conn.endheaders(body)

            resp = conn.getresponse()

            output = resp.read()

            if resp.status != 201 and resp.status != 200:
                return False, resp.reason + "\n" + output
            else:
                if 'location' in resp.msg.dict:
                    occi_id = os.path.basename(resp.msg.dict['location'])
                else:
                    occi_id = os.path.basename(output)
                return True, occi_id
        except Exception, ex:
            self.logger.exception("Error creating volume")
            return False, str(ex)
        finally:
            self.delete_proxy(conn)

    def delete_volume(self, storage_id, auth_data, timeout=180, delay=5):
        """
        Delete a volume
        """
        if storage_id.startswith("http"):
            storage_id = uriparse(storage_id)[2]
        else:
            if not storage_id.startswith("/storage"):
                storage_id = "/storage/%s" % storage_id
            storage_id = self.cloud.path + storage_id
        wait = 0
        while wait < timeout:
            auth = self.get_auth_header(auth_data)
            headers = {'Accept': 'text/plain', 'Connection': 'close'}
            if auth:
                headers.update(auth)

            self.logger.debug("Delete storage: %s" % storage_id)
            conn = None
            try:
                conn = self.get_http_connection(auth_data)
                conn.request('DELETE', storage_id, headers=headers)
                resp = conn.getresponse()
                output = str(resp.read())
                if resp.status == 404:
                    self.logger.debug("It does not exist.")
                    return (True, "")
                elif resp.status == 409:
                    self.logger.debug("Error deleting the Volume. It seems that it is still "
                                      "attached to a VM: %s" % output)
                    time.sleep(delay)
                    wait += delay
                elif resp.status != 200 and resp.status != 204:
                    self.logger.error("Error deleting the Volume: " + resp.reason + "\n" + output)
                    return (False, "Error deleting the Volume: " + resp.reason + "\n" + output)
                else:
                    self.logger.debug("Successfully deleted")
                    return (True, "")
            except Exception:
                self.logger.exception("Error connecting with OCCI server")
                return (False, "Error connecting with OCCI server")
            finally:
                self.delete_proxy(conn)

        return (False, "Error deleting the Volume: Timeout.")

    def launch(self, inf, radl, requested_radl, num_vm, auth_data):
        system = radl.systems[0]
        auth_header = self.get_auth_header(auth_data)

        cpu = system.getValue('cpu.count')
        memory = None
        if system.getFeature('memory.size'):
            memory = system.getFeature('memory.size').getValue('G')
        name = system.getValue("instance_name")
        if not name:
            name = system.getValue("disk.0.image.name")
        if not name:
            name = "im_userimage"
        arch = system.getValue('cpu.arch')

        if arch.find('64'):
            arch = 'x64'
        else:
            arch = 'x86'

        res = []
        i = 0

        public_key = system.getValue('disk.0.os.credentials.public_key')
        password = system.getValue('disk.0.os.credentials.password')

        if public_key:
            if password:
                system.delValue('disk.0.os.credentials.password')
            password = None
        else:
            if not password:
                # We must generate them
                (public_key, private_key) = self.keygen()
                system.setValue('disk.0.os.credentials.private_key', private_key)

        user = system.getValue('disk.0.os.credentials.username')
        if not user:
            user = self.DEFAULT_USER
            system.setValue('disk.0.os.credentials.username', user)

        user_data = ""
        if public_key:
            # Add user cloud init data
            cloud_config_str = self.get_cloud_init_data(radl)
            cloud_config = self.gen_cloud_config(public_key, user, cloud_config_str)
            user_data = base64.b64encode(cloud_config).replace("\n", "")
            self.logger.debug("Cloud init: " + cloud_config)

        # Get the info about the OCCI server (GET /-/)
        occi_info = self.query_occi(auth_data)

        # Parse the info to get the os_tpl scheme
        url = uriparse(system.getValue("disk.0.image.url"))
        # Get the Image ID from the last part of the path
        os_tpl = os.path.basename(url[2])
        os_tpl_scheme = self.get_os_tpl_scheme(occi_info, os_tpl)
        if not os_tpl_scheme:
            raise Exception(
                "Error getting os_tpl scheme. Check that the image specified is supported in the OCCI server.")

        # Parse the info to get the instance_type (resource_tpl) scheme
        instance_type_uri = None
        if system.getValue('instance_type'):
            instance_type = self.get_instance_type_uri(
                occi_info, system.getValue('instance_type'))
            instance_type_uri = uriparse(instance_type)
            if not instance_type_uri[5]:
                raise Exception("Error getting Instance type URI. Check that the instance_type specified is "
                                "supported in the OCCI server.")
            else:
                instance_name = instance_type_uri[5]
                instance_scheme = instance_type_uri[
                    0] + "://" + instance_type_uri[1] + instance_type_uri[2] + "#"

        while i < num_vm:
            volumes = []
            conn = None
            try:
                # First create the volumes
                volumes = self.create_volumes(system, auth_data)

                conn = self.get_http_connection(auth_data)
                conn.putrequest('POST', self.cloud.path + "/compute/")
                if auth_header:
                    conn.putheader(auth_header.keys()[
                                   0], auth_header.values()[0])
                conn.putheader('Accept', 'text/plain')
                conn.putheader('Content-Type', 'text/plain')
                conn.putheader('Connection', 'close')

                body = 'Category: compute; scheme="http://schemas.ogf.org/occi/infrastructure#"; class="kind"\n'
                body += 'Category: ' + os_tpl + '; scheme="' + \
                    os_tpl_scheme + '"; class="mixin"\n'
                body += 'Category: user_data; scheme="http://schemas.openstack.org/compute/instance#"; class="mixin"\n'
                # body += 'Category: public_key;
                # scheme="http://schemas.openstack.org/instance/credentials#";
                # class="mixin"\n'

                if instance_type_uri:
                    body += 'Category: ' + instance_name + '; scheme="' + \
                        instance_scheme + '"; class="mixin"\n'
                else:
                    # Try to use this OCCI attributes (not supported by
                    # openstack)
                    if cpu:
                        body += 'X-OCCI-Attribute: occi.compute.cores=' + \
                            str(cpu) + '\n'
                    # body += 'X-OCCI-Attribute: occi.compute.architecture=' + arch +'\n'
                    if memory:
                        body += 'X-OCCI-Attribute: occi.compute.memory=' + \
                            str(memory) + '\n'

                compute_id = "im." + str(int(time.time() * 100))
                body += 'X-OCCI-Attribute: occi.core.id="' + compute_id + '"\n'
                body += 'X-OCCI-Attribute: occi.core.title="' + name + '"\n'

                # Set the hostname defined in the RADL
                # Create the VM to get the nodename
                vm = VirtualMachine(inf, None, self.cloud, radl, requested_radl, self)
                (nodename, nodedom) = vm.getRequestedName(default_hostname=Config.DEFAULT_VM_NAME,
                                                          default_domain=Config.DEFAULT_DOMAIN)

                body += 'X-OCCI-Attribute: occi.compute.hostname="' + nodename + '"\n'
                # See: https://wiki.egi.eu/wiki/HOWTO10
                # body += 'X-OCCI-Attribute: org.openstack.credentials.publickey.name="my_key"'
                # body += 'X-OCCI-Attribute: org.openstack.credentials.publickey.data="ssh-rsa BAA...zxe ==user@host"'
                if user_data:
                    body += 'X-OCCI-Attribute: org.openstack.compute.user_data="' + user_data + '"\n'

                # Add volume links
                for device, volume_id in volumes:
                    body += ('Link: <%s/storage/%s>;rel="http://schemas.ogf.org/occi/infrastructure#storage";'
                             'category="http://schemas.ogf.org/occi/infrastructure#storagelink";'
                             'occi.core.target="%s/storage/%s";occi.core.source="%s/compute/%s"'
                             '' % (self.cloud.path, volume_id,
                                   self.cloud.path, volume_id,
                                   self.cloud.path, compute_id))
                    if device:
                        body += ';occi.storagelink.deviceid="/dev/%s"\n' % device
                    body += '\n'

                self.logger.debug(body)

                conn.putheader('Content-Length', len(body))
                conn.endheaders(body)

                resp = conn.getresponse()

                # With this format: X-OCCI-Location:
                # http://fc-one.i3m.upv.es:11080/compute/8
                output = resp.read()

                # some servers return 201 and other 200
                if resp.status != 201 and resp.status != 200:
                    res.append((False, resp.reason + "\n" + output))
                    for _, volume_id in volumes:
                        self.delete_volume(volume_id, auth_data)
                else:
                    if 'location' in resp.msg.dict:
                        occi_vm_id = os.path.basename(
                            resp.msg.dict['location'])
                    else:
                        occi_vm_id = os.path.basename(output)
                    if occi_vm_id:
                        vm.id = occi_vm_id
                        vm.info.systems[0].setValue('instance_id', str(occi_vm_id))
                        res.append((True, vm))
                    else:
                        res.append((False, 'Unknown Error launching the VM.'))

            except Exception, ex:
                self.logger.exception("Error connecting with OCCI server")
                res.append((False, "ERROR: " + str(ex)))
                for _, volume_id in volumes:
                    self.delete_volume(volume_id, auth_data)
            finally:
                self.delete_proxy(conn)

            i += 1

        return res

    def get_volume_ids_from_radl(self, system):
        volumes = []
        cont = 1
        while system.getValue("disk." + str(cont) + ".size") and system.getValue("disk." + str(cont) + ".device"):
            provider_id = system.getValue("disk." + str(cont) + ".provider_id")
            if provider_id:
                volumes.append(provider_id)
            cont += 1

        return volumes

    def get_attached_volumes(self, vm, auth_data):
        auth = self.get_auth_header(auth_data)
        headers = {'Accept': 'text/plain', 'Connection': 'close'}
        if auth:
            headers.update(auth)
        conn = None
        try:
            conn = self.get_http_connection(auth_data)
            conn.request('GET', self.cloud.path +
                         "/compute/" + vm.id, headers=headers)
            resp = conn.getresponse()

            output = resp.read()
            if resp.status == 404 or resp.status == 204:
                return (True, "")
            elif resp.status != 200:
                return (False, resp.reason + "\n" + output)
            else:
                occi_volumes = self.get_attached_volumes_from_info(output)
                deleted_vols = []
                for link, num_storage, device in occi_volumes:
                    if not device.endswith("vda") and not device.endswith("hda"):
                        deleted_vols.append((link, num_storage, device))
                return (True, deleted_vols)
        except Exception, ex:
            self.logger.exception("Error deleting volumes")
            return (False, "Error deleting volumes " + str(ex))
        finally:
            self.delete_proxy(conn)

    def finalize(self, vm, auth_data):
        # First try to get the volumes
        get_vols_ok, volumes = self.get_attached_volumes(vm, auth_data)
        if not get_vols_ok:
            self.logger.error("Error getting attached volumes: %s" % volumes)

        auth = self.get_auth_header(auth_data)
        headers = {'Accept': 'text/plain', 'Connection': 'close'}
        if auth:
            headers.update(auth)
        conn = None
        try:
            conn = self.get_http_connection(auth_data)
            conn.request('DELETE', self.cloud.path +
                         "/compute/" + vm.id, headers=headers)
            resp = conn.getresponse()
            output = str(resp.read())
            if resp.status != 200 and resp.status != 404 and resp.status != 204:
                return (False, "Error removing the VM: " + resp.reason + "\n" + output)
        except Exception:
            self.logger.exception("Error connecting with OCCI server")
            return (False, "Error connecting with OCCI server")
        finally:
            self.delete_proxy(conn)

        # now delete the volumes
        if get_vols_ok:
            for _, storage_id, _ in volumes:
                self.delete_volume(storage_id, auth_data)

        # sometime we have created a volume that is not correctly attached to the vm
        # check the RADL of the VM to get them
        radl_volumes = self.get_volume_ids_from_radl(vm.info.systems[0])
        for num_storage in radl_volumes:
            self.delete_volume(num_storage, auth_data)

        return (True, vm.id)

    def stop(self, vm, auth_data):
        auth_header = self.get_auth_header(auth_data)
        conn = None
        try:
            conn = self.get_http_connection(auth_data)
            conn.putrequest('POST', self.cloud.path +
                            "/compute/" + vm.id + "?action=suspend")
            if auth_header:
                conn.putheader(auth_header.keys()[0], auth_header.values()[0])
            conn.putheader('Accept', 'text/plain')
            conn.putheader('Content-Type', 'text/plain,text/occi')
            conn.putheader('Connection', 'close')

            body = ('Category: suspend;scheme="http://schemas.ogf.org/occi/infrastructure/compute/action#"'
                    ';class="action";\n')
            conn.putheader('Content-Length', len(body))
            conn.endheaders(body)

            resp = conn.getresponse()
            output = str(resp.read())
            if resp.status != 200:
                return (False, "Error stopping the VM: " + resp.reason + "\n" + output)
            else:
                return (True, vm.id)
        except Exception:
            self.logger.exception("Error connecting with OCCI server")
            return (False, "Error connecting with OCCI server")
        finally:
            self.delete_proxy(conn)

    def start(self, vm, auth_data):
        auth_header = self.get_auth_header(auth_data)
        conn = None
        try:
            conn = self.get_http_connection(auth_data)
            conn.putrequest('POST', self.cloud.path +
                            "/compute/" + vm.id + "?action=start")
            if auth_header:
                conn.putheader(auth_header.keys()[0], auth_header.values()[0])
            conn.putheader('Accept', 'text/plain')
            conn.putheader('Content-Type', 'text/plain,text/occi')
            conn.putheader('Connection', 'close')

            body = ('Category: start;scheme="http://schemas.ogf.org/occi/infrastructure/compute/action#"'
                    ';class="action";\n')
            conn.putheader('Content-Length', len(body))
            conn.endheaders(body)

            resp = conn.getresponse()
            output = str(resp.read())
            if resp.status != 200:
                return (False, "Error starting the VM: " + resp.reason + "\n" + output)
            else:
                return (True, vm.id)
        except Exception:
            self.logger.exception("Error connecting with OCCI server")
            return (False, "Error connecting with OCCI server")
        finally:
            self.delete_proxy(conn)

    def alterVM(self, vm, radl, auth_data):
        """
        In the OCCI case it only enable to attach new disks
        """
        if not radl.systems:
            return (True, "")

        try:
            orig_system = vm.info.systems[0]

            cont = 1
            while orig_system.getValue("disk." + str(cont) + ".size"):
                cont += 1

            system = radl.systems[0]
            while system.getValue("disk." + str(cont) + ".size"):
                disk_size = system.getFeature("disk." + str(cont) + ".size").getValue('G')
                disk_device = system.getValue("disk." + str(cont) + ".device")
                mount_path = system.getValue("disk." + str(cont) + ".mount_path")
                if disk_device:
                    # get the last letter and use vd
                    disk_device = "vd" + disk_device[-1]
                    system.setValue("disk." + str(cont) + ".device", disk_device)
                self.logger.debug("Creating a %d GB volume for the disk %d" % (int(disk_size), cont))
                success, volume_id = self.create_volume(int(disk_size), "im-disk-%d" % cont, auth_data)

                if success:
                    self.logger.debug("Volume id %s successfuly created." % volume_id)
                    # let's wait the storage to be ready "online"
                    wait_ok = self.wait_volume_state(volume_id, auth_data)
                    if not wait_ok:
                        self.logger.debug("Error waiting volume %s. Deleting it." % volume_id)
                        self.delete_volume(volume_id, auth_data)
                        return (False, "Error waiting volume %s. Deleting it." % volume_id)
                else:
                    self.logger.error("Error creating volume: %s" % volume_id)

                if wait_ok:
                    self.logger.debug("Attaching to the instance")
                    attached = self.attach_volume(vm, volume_id, disk_device, mount_path, auth_data)
                    if attached:
                        orig_system.setValue("disk." + str(cont) + ".size", disk_size, "G")
                        orig_system.setValue("disk." + str(cont) + ".provider_id", volume_id)
                        if disk_device:
                            orig_system.setValue("disk." + str(cont) + ".device", disk_device)
                        if mount_path:
                            orig_system.setValue("disk." + str(cont) + ".mount_path", mount_path)
                    else:
                        self.logger.error("Error attaching a %d GB volume for the disk %d."
                                          " Deleting it." % (int(disk_size), cont))
                        self.delete_volume(volume_id, auth_data)
                        return (False, "Error attaching the new volume")
                else:
                    return (False, "Error creating the new volume: " + volume_id)
                cont += 1
        except Exception, ex:
            self.logger.exception("Error connecting with OCCI server")
            return (False, "Error connecting with OCCI server: " + str(ex))

        return (True, "")

    def attach_volume(self, vm, volume_id, device, mount_path, auth_data):
        """
        Attach a volume to a running VM
        """
        occi_info = self.query_occi(auth_data)
        url = self.get_property_from_category(occi_info, "storagelink", "location")
        if not url:
            self.logger.error("No location for storagelink category.")
            return (False, "No location for storagelink category.")

        auth_header = self.get_auth_header(auth_data)
        conn = None
        try:
            conn = self.get_http_connection(auth_data)
            conn.putrequest('POST', url)
            if auth_header:
                conn.putheader(auth_header.keys()[0], auth_header.values()[0])
            conn.putheader('Accept', 'text/plain')
            conn.putheader('Content-Type', 'text/plain,text/occi')
            conn.putheader('Connection', 'close')

            disk_id = "imdisk." + str(int(time.time() * 100))

            body = ('Category: storagelink;scheme="http://schemas.ogf.org/occi/infrastructure#";class="kind";'
                    'location="%s/link/storagelink/";title="storagelink"\n' % self.cloud.path)
            body += 'X-OCCI-Attribute: occi.core.id="%s"\n' % disk_id
            body += 'X-OCCI-Attribute: occi.core.target="%s/storage/%s"\n' % (self.cloud.path, volume_id)
            body += 'X-OCCI-Attribute: occi.core.source="%s/compute/%s"' % (self.cloud.path, vm.id)
            body += 'X-OCCI-Attribute: occi.storagelink.deviceid="/dev/%s"' % device
            # body += 'X-OCCI-Attribute: occi.storagelink.mountpoint="%s"' % mount_path
            conn.putheader('Content-Length', len(body))
            conn.endheaders(body)

            resp = conn.getresponse()
            output = str(resp.read())
            if resp.status != 201 and resp.status != 200:
                self.logger.error("Error attaching disk to the VM: " + resp.reason + "\n" + output)
                return False
            else:
                return True
        except Exception:
            self.logger.exception("Error connecting with OCCI server")
            return False
        finally:
            self.delete_proxy(conn)


class KeyStoneAuth:
    """
    Class to manage the Keystone auth tokens used in OpenStack
    """

    @staticmethod
    def get_keystone_uri(occi, auth_data):
        """
        Contact the OCCI server to check if it needs to contact a keystone server.
        It returns the keystone server URI or None.
        """
        conn = None
        try:
            headers = {'Accept': 'text/plain', 'Connection': 'close'}
            conn = occi.get_http_connection(auth_data)
            conn.request('HEAD', occi.cloud.path + "/-/", headers=headers)
            resp = conn.getresponse()
            www_auth_head = resp.getheader('Www-Authenticate')
            if www_auth_head and www_auth_head.startswith('Keystone uri'):
                return www_auth_head.split('=')[1].replace("'", "")
            else:
                return None
        except SSLError, ex:
            occi.logger.exception(
                "Error with the credentials when contacting with the OCCI server.")
            raise Exception(
                "Error with the credentials when contacting with the OCCI server: %s. Check your proxy file." % str(ex))
        except:
            occi.logger.exception("Error contacting with the OCCI server.")
            return None
        finally:
            occi.delete_proxy(conn)

    @staticmethod
    def get_keystone_token(occi, keystone_uri, auth):
        """
        Contact the specified keystone server to return the token
        """
        conn = None
        try:
            uri = uriparse(keystone_uri)
            server = uri[1].split(":")[0]
            port = int(uri[1].split(":")[1])

            conn = occi.get_https_connection(auth, server, port)
            conn.putrequest('POST', "/v2.0/tokens")
            conn.putheader('Accept', 'application/json')
            conn.putheader('Content-Type', 'application/json')
            conn.putheader('Connection', 'close')

            body = '{"auth":{"voms":true}}'

            conn.putheader('Content-Length', len(body))
            conn.endheaders(body)

            resp = conn.getresponse()
            occi.delete_proxy(conn)

            # format: -> "{\"access\": {\"token\": {\"issued_at\":
            # \"2014-12-29T17:10:49.609894\", \"expires\":
            # \"2014-12-30T17:10:49Z\", \"id\":
            # \"c861ab413e844d12a61d09b23dc4fb9c\"}, \"serviceCatalog\": [],
            # \"user\": {\"username\":
            # \"/DC=es/DC=irisgrid/O=upv/CN=miguel-caballer\", \"roles_links\":
            # [], \"id\": \"475ce4978fb042e49ce0391de9bab49b\", \"roles\": [],
            # \"name\": \"/DC=es/DC=irisgrid/O=upv/CN=miguel-caballer\"},
            # \"metadata\": {\"is_admin\": 0, \"roles\": []}}}"
            output = json.loads(resp.read())
            if 'access' in output:
                token_id = output['access']['token']['id']
            else:
                occi.logger.exception("Error obtaining Keystone Token.")
                raise Exception("Error obtaining Keystone Token: %s" % str(output))

            conn = occi.get_https_connection(auth, server, port)
            headers = {'Accept': 'application/json', 'Content-Type': 'application/json',
                       'X-Auth-Token': token_id, 'Connection': 'close'}
            conn.request('GET', "/v2.0/tenants", headers=headers)
            resp = conn.getresponse()
            occi.delete_proxy(conn)

            # format: -> "{\"tenants_links\": [], \"tenants\":
            # [{\"description\": \"egi fedcloud\", \"enabled\": true, \"id\":
            # \"fffd98393bae4bf0acf66237c8f292ad\", \"name\": \"egi\"}]}"
            output = json.loads(resp.read())

            tenant_token_id = None
            # retry for each available tenant (usually only one)
            for tenant in output['tenants']:
                conn = occi.get_https_connection(auth, server, port)
                conn.putrequest('POST', "/v2.0/tokens")
                conn.putheader('Accept', 'application/json')
                conn.putheader('Content-Type', 'application/json')
                conn.putheader('X-Auth-Token', token_id)
                conn.putheader('Connection', 'close')

                body = '{"auth":{"voms":true,"tenantName":"' + str(tenant['name']) + '"}}'

                conn.putheader('Content-Length', len(body))
                conn.endheaders(body)

                resp = conn.getresponse()
                occi.delete_proxy(conn)

                # format: -> "{\"access\": {\"token\": {\"issued_at\":
                # \"2014-12-29T17:10:49.609894\", \"expires\":
                # \"2014-12-30T17:10:49Z\", \"id\":
                # \"c861ab413e844d12a61d09b23dc4fb9c\"}, \"serviceCatalog\": [],
                # \"user\": {\"username\":
                # \"/DC=es/DC=irisgrid/O=upv/CN=miguel-caballer\", \"roles_links\":
                # [], \"id\": \"475ce4978fb042e49ce0391de9bab49b\", \"roles\": [],
                # \"name\": \"/DC=es/DC=irisgrid/O=upv/CN=miguel-caballer\"},
                # \"metadata\": {\"is_admin\": 0, \"roles\": []}}}"
                output = json.loads(resp.read())
                if 'access' in output:
                    tenant_token_id = output['access']['token']['id']
                    break

            return tenant_token_id
        except Exception, ex:
            occi.logger.exception("Error obtaining Keystone Token.")
            raise Exception("Error obtaining Keystone Token: %s" % str(ex))
        finally:
            occi.delete_proxy(conn)
