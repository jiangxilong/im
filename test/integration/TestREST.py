#! /usr/bin/env python
#
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

import unittest
import os
import httplib
import time
import sys
import json

sys.path.append("..")
sys.path.append(".")

from IM.VirtualMachine import VirtualMachine
from IM.uriparse import uriparse
from radl import radl_parse
from IM import __version__ as version

PID = None
RADL_ADD = "network publica\nsystem front\ndeploy front 1"
RADL_ADD_ERROR = "system wnno deploy wnno 1"
HOSTNAME = "localhost"
TEST_PORT = 8800


def read_file_as_string(file_name):
    tests_path = os.path.dirname(os.path.abspath(__file__))
    abs_file_path = os.path.join(tests_path, file_name)
    return open(abs_file_path, 'r').read()


class TestIM(unittest.TestCase):

    server = None
    auth_data = None
    inf_id = 0

    @classmethod
    def setUpClass(cls):
        cls.auth_data = read_file_as_string('../auth.dat').replace("\n", "\\n")
        cls.inf_id = "0"

    @classmethod
    def tearDownClass(cls):
        # Assure that the infrastructure is destroyed
        try:
            server = httplib.HTTPConnection(HOSTNAME, TEST_PORT)
            server.request('DELETE', "/infrastructures/" + cls.inf_id,
                           headers={'Authorization': cls.auth_data})
            server.getresponse()
            server.close()
        except Exception:
            pass

    def wait_inf_state(self, state, timeout, incorrect_states=[], vm_ids=None):
        """
        Wait for an infrastructure to have a specific state
        """
        if not vm_ids:
            server = httplib.HTTPConnection(HOSTNAME, TEST_PORT)
            server.request('GET', "/infrastructures/" + self.inf_id,
                           headers={'AUTHORIZATION': self.auth_data})
            resp = server.getresponse()
            output = str(resp.read())
            server.close()
            self.assertEqual(resp.status, 200,
                             msg="ERROR getting infrastructure info:" + output)

            vm_ids = output.split("\n")
        else:
            pass

        err_states = [VirtualMachine.FAILED,
                      VirtualMachine.OFF, VirtualMachine.UNCONFIGURED]
        err_states.extend(incorrect_states)

        wait = 0
        all_ok = False
        while not all_ok and wait < timeout:
            all_ok = True
            for vm_id in vm_ids:
                vm_uri = uriparse(vm_id)
                server = httplib.HTTPConnection(HOSTNAME, TEST_PORT)
                server.request(
                    'GET', vm_uri[2] + "/state", headers={'AUTHORIZATION': self.auth_data})
                resp = server.getresponse()
                vm_state = str(resp.read())
                server.close()
                self.assertEqual(resp.status, 200,
                                 msg="ERROR getting VM info:" + vm_state)

                if vm_state == VirtualMachine.UNCONFIGURED:
                    server = httplib.HTTPConnection(HOSTNAME, TEST_PORT)
                    server.request('GET', "/infrastructures/" + self.inf_id + "/contmsg",
                                   headers={'AUTHORIZATION': self.auth_data})
                    resp = server.getresponse()
                    output = str(resp.read())
                    server.close()
                    print output

                self.assertFalse(vm_state in err_states, msg=("ERROR waiting for a state. '%s' state was expected "
                                                              "and '%s' was obtained in the VM %s" % (state,
                                                                                                      vm_state,
                                                                                                      vm_uri)))

                if vm_state in err_states:
                    return False
                elif vm_state != state:
                    all_ok = False

            if not all_ok:
                wait += 5
                time.sleep(5)

        return all_ok

    def test_05_version(self):
        server = httplib.HTTPConnection(HOSTNAME, TEST_PORT)
        server.request('GET', "/version")
        resp = server.getresponse()
        output = str(resp.read())
        server.close()
        self.assertEqual(resp.status, 200,
                         msg="ERROR getting IM version:" + output)
        self.assertEqual(
            output, version, msg="Incorrect version. Expected %s, obtained: %s" % (version, output))

    def test_10_list(self):
        server = httplib.HTTPConnection(HOSTNAME, TEST_PORT)
        server.request('GET', "/infrastructures",
                       headers={'AUTHORIZATION': self.auth_data})
        resp = server.getresponse()
        output = str(resp.read())
        server.close()
        self.assertEqual(resp.status, 200,
                         msg="ERROR listing user infrastructures:" + output)

    def test_15_get_incorrect_info(self):
        server = httplib.HTTPConnection(HOSTNAME, TEST_PORT)
        server.request('GET', "/infrastructures/999999",
                       headers={'AUTHORIZATION': self.auth_data})
        resp = server.getresponse()
        resp.read()
        server.close()
        self.assertEqual(resp.status, 404,
                         msg="Incorrect error message: " + str(resp.status))

    def test_16_get_incorrect_info_json(self):
        server = httplib.HTTPConnection(HOSTNAME, TEST_PORT)
        server.request('GET', "/infrastructures/999999",
                       headers={'AUTHORIZATION': self.auth_data, 'Accept': 'application/json'})
        resp = server.getresponse()
        output = resp.read()
        server.close()
        self.assertEqual(resp.status, 404,
                         msg="Incorrect error message: " + str(resp.status))
        res = json.loads(output)
        self.assertEqual(res['code'], 404,
                         msg="Incorrect error message: " + output)

    def test_18_get_info_without_auth_data(self):
        server = httplib.HTTPConnection(HOSTNAME, TEST_PORT)
        server.request('GET', "/infrastructures/0")
        resp = server.getresponse()
        resp.read()
        server.close()
        self.assertEqual(resp.status, 401,
                         msg="Incorrect error message: " + str(resp.status))

    def test_20_create(self):
        server = httplib.HTTPConnection(HOSTNAME, TEST_PORT)
        radl = read_file_as_string('../files/test_simple.radl')

        server.request('POST', "/infrastructures", body=radl,
                       headers={'AUTHORIZATION': self.auth_data})
        resp = server.getresponse()
        output = str(resp.read())
        server.close()
        self.assertEqual(resp.status, 200,
                         msg="ERROR creating the infrastructure:" + output)

        self.__class__.inf_id = str(os.path.basename(output))

        all_configured = self.wait_inf_state(VirtualMachine.CONFIGURED, 600)
        self.assertTrue(
            all_configured, msg="ERROR waiting the infrastructure to be configured (timeout).")

    def test_22_get_forbidden_info(self):
        server = httplib.HTTPConnection(HOSTNAME, TEST_PORT)
        server.request('GET', "/infrastructures/" + self.inf_id,
                       headers={'AUTHORIZATION': ("type = InfrastructureManager; "
                                                  "username = some; password = other")})
        resp = server.getresponse()
        resp.read()
        server.close()
        self.assertEqual(resp.status, 403,
                         msg="Incorrect error message: " + str(resp.status))

    def test_30_get_vm_info(self):
        server = httplib.HTTPConnection(HOSTNAME, TEST_PORT)
        server.request('GET', "/infrastructures/" + self.inf_id,
                       headers={'AUTHORIZATION': self.auth_data})
        resp = server.getresponse()
        output = str(resp.read())
        server.close()
        self.assertEqual(resp.status, 200,
                         msg="ERROR getting the infrastructure info:" + output)
        vm_ids = output.split("\n")

        vm_uri = uriparse(vm_ids[0])
        server = httplib.HTTPConnection(HOSTNAME, TEST_PORT)
        server.request('GET', vm_uri[2], headers={'AUTHORIZATION': self.auth_data})
        resp = server.getresponse()
        output = str(resp.read())
        server.close()
        self.assertEqual(resp.status, 200,
                         msg="ERROR getting VM info:" + output)

    def test_32_get_vm_contmsg(self):
        server = httplib.HTTPConnection(HOSTNAME, TEST_PORT)
        server.request('GET', "/infrastructures/" + self.inf_id,
                       headers={'AUTHORIZATION': self.auth_data})
        resp = server.getresponse()
        output = str(resp.read())
        server.close()
        self.assertEqual(resp.status, 200,
                         msg="ERROR getting the infrastructure info:" + output)
        vm_ids = output.split("\n")

        vm_uri = uriparse(vm_ids[0])
        server = httplib.HTTPConnection(HOSTNAME, TEST_PORT)
        server.request('GET', vm_uri[2] + "/contmsg", headers={'AUTHORIZATION': self.auth_data})
        resp = server.getresponse()
        output = str(resp.read())
        server.close()
        self.assertEqual(resp.status, 200,
                         msg="ERROR getting VM contmsg:" + output)
        self.assertEqual(
            len(output), 0, msg="Incorrect VM contextualization message: " + output)

    def test_33_get_contmsg(self):
        server = httplib.HTTPConnection(HOSTNAME, TEST_PORT)
        server.request('GET', "/infrastructures/" + self.inf_id + "/contmsg",
                       headers={'AUTHORIZATION': self.auth_data})
        resp = server.getresponse()
        output = str(resp.read())
        server.close()
        self.assertEqual(resp.status, 200,
                         msg="ERROR getting the infrastructure info:" + output)
        self.assertGreater(
            len(output), 30, msg="Incorrect contextualization message: " + output)

    def test_34_get_radl(self):
        server = httplib.HTTPConnection(HOSTNAME, TEST_PORT)
        server.request('GET', "/infrastructures/" + self.inf_id + "/radl",
                       headers={'AUTHORIZATION': self.auth_data})
        resp = server.getresponse()
        output = str(resp.read())
        server.close()
        self.assertEqual(resp.status, 200,
                         msg="ERROR getting the infrastructure RADL:" + output)
        try:
            radl_parse.parse_radl(output)
        except Exception, ex:
            self.assertTrue(
                False, msg="ERROR parsing the RADL returned by GetInfrastructureRADL: " + str(ex))

    def test_35_get_vm_property(self):
        server = httplib.HTTPConnection(HOSTNAME, TEST_PORT)
        server.request('GET', "/infrastructures/" + self.inf_id,
                       headers={'AUTHORIZATION': self.auth_data})
        resp = server.getresponse()
        output = str(resp.read())
        server.close()
        self.assertEqual(resp.status, 200,
                         msg="ERROR getting the infrastructure info:" + output)
        vm_ids = output.split("\n")

        vm_uri = uriparse(vm_ids[0])
        server = httplib.HTTPConnection(HOSTNAME, TEST_PORT)
        server.request(
            'GET', vm_uri[2] + "/state", headers={'AUTHORIZATION': self.auth_data})
        resp = server.getresponse()
        output = str(resp.read())
        server.close()
        self.assertEqual(resp.status, 200,
                         msg="ERROR getting VM property:" + output)

    def test_40_addresource(self):
        server = httplib.HTTPConnection(HOSTNAME, TEST_PORT)
        server.request('POST', "/infrastructures/" + self.inf_id,
                       body=RADL_ADD, headers={'AUTHORIZATION': self.auth_data})
        resp = server.getresponse()
        output = str(resp.read())
        server.close()
        self.assertEqual(resp.status, 200,
                         msg="ERROR adding resources:" + output)

        server = httplib.HTTPConnection(HOSTNAME, TEST_PORT)
        server.request('GET', "/infrastructures/" + self.inf_id,
                       headers={'AUTHORIZATION': self.auth_data})
        resp = server.getresponse()
        output = str(resp.read())
        server.close()
        self.assertEqual(resp.status, 200,
                         msg="ERROR getting the infrastructure info:" + output)
        vm_ids = output.split("\n")
        self.assertEqual(len(vm_ids), 2, msg=("ERROR getting infrastructure info: Incorrect number of VMs(" +
                                              str(len(vm_ids)) + "). It must be 2"))
        all_configured = self.wait_inf_state(VirtualMachine.CONFIGURED, 600)
        self.assertTrue(
            all_configured, msg="ERROR waiting the infrastructure to be configured (timeout).")

    def test_45_getstate(self):
        server = httplib.HTTPConnection(HOSTNAME, TEST_PORT)
        server.request('GET', "/infrastructures/" + self.inf_id + "/state",
                       headers={'AUTHORIZATION': self.auth_data})
        resp = server.getresponse()
        output = str(resp.read())
        server.close()
        self.assertEqual(
            resp.status, 200, msg="ERROR getting the infrastructure state:" + output)
        res = json.loads(output)
        state = res['state']['state']
        vm_states = res['state']['vm_states']
        self.assertEqual(state, "configured", msg="Unexpected inf state: " +
                         state + ". It must be 'configured'.")
        for vm_id, vm_state in vm_states.iteritems():
            self.assertEqual(vm_state, "configured", msg="Unexpected vm state: " +
                             vm_state + " in VM ID " + str(vm_id) + ". It must be 'configured'.")

    def test_46_removeresource(self):
        server = httplib.HTTPConnection(HOSTNAME, TEST_PORT)
        server.request('GET', "/infrastructures/" + self.inf_id,
                       headers={'AUTHORIZATION': self.auth_data})
        resp = server.getresponse()
        output = str(resp.read())
        server.close()
        self.assertEqual(resp.status, 200,
                         msg="ERROR getting the infrastructure info:" + output)
        vm_ids = output.split("\n")

        vm_uri = uriparse(vm_ids[1])
        server = httplib.HTTPConnection(HOSTNAME, TEST_PORT)
        server.request('DELETE', vm_uri[2], headers={'AUTHORIZATION': self.auth_data})
        resp = server.getresponse()
        output = str(resp.read())
        server.close()
        self.assertEqual(resp.status, 200,
                         msg="ERROR removing resources:" + output)

        server = httplib.HTTPConnection(HOSTNAME, TEST_PORT)
        server.request('GET', "/infrastructures/" + self.inf_id,
                       headers={'AUTHORIZATION': self.auth_data})
        resp = server.getresponse()
        output = str(resp.read())
        server.close()
        self.assertEqual(resp.status, 200,
                         msg="ERROR getting the infrastructure info:" + output)
        vm_ids = output.split("\n")
        self.assertEqual(len(vm_ids), 1, msg=("ERROR getting infrastructure info: Incorrect number of VMs(" +
                                              str(len(vm_ids)) + "). It must be 1"))

        all_configured = self.wait_inf_state(VirtualMachine.CONFIGURED, 300)
        self.assertTrue(
            all_configured, msg="ERROR waiting the infrastructure to be configured (timeout).")

    def test_47_addresource_noconfig(self):
        server = httplib.HTTPConnection(HOSTNAME, TEST_PORT)
        server.request('POST', "/infrastructures/" + self.inf_id + "?context=0",
                       body=RADL_ADD, headers={'AUTHORIZATION': self.auth_data})
        resp = server.getresponse()
        output = str(resp.read())
        server.close()
        self.assertEqual(resp.status, 200,
                         msg="ERROR adding resources:" + output)

    def test_50_removeresource_noconfig(self):
        server = httplib.HTTPConnection(HOSTNAME, TEST_PORT)
        server.request('GET', "/infrastructures/" + self.inf_id + "?context=0",
                       headers={'AUTHORIZATION': self.auth_data})
        resp = server.getresponse()
        output = str(resp.read())
        server.close()
        self.assertEqual(resp.status, 200,
                         msg="ERROR getting the infrastructure info:" + output)
        vm_ids = output.split("\n")

        vm_uri = uriparse(vm_ids[1])
        server = httplib.HTTPConnection(HOSTNAME, TEST_PORT)
        server.request('DELETE', vm_uri[2], headers={'AUTHORIZATION': self.auth_data})
        resp = server.getresponse()
        output = str(resp.read())
        server.close()
        self.assertEqual(resp.status, 200,
                         msg="ERROR removing resources:" + output)

        server = httplib.HTTPConnection(HOSTNAME, TEST_PORT)
        server.request('GET', "/infrastructures/" + self.inf_id,
                       headers={'AUTHORIZATION': self.auth_data})
        resp = server.getresponse()
        output = str(resp.read())
        server.close()
        self.assertEqual(resp.status, 200,
                         msg="ERROR getting the infrastructure info:" + output)
        vm_ids = output.split("\n")
        self.assertEqual(len(vm_ids), 1, msg=("ERROR getting infrastructure info: Incorrect number of VMs(" +
                                              str(len(vm_ids)) + "). It must be 1"))

    def test_55_reconfigure(self):
        server = httplib.HTTPConnection(HOSTNAME, TEST_PORT)
        server.request('PUT', "/infrastructures/" + self.inf_id + "/reconfigure",
                       headers={'AUTHORIZATION': self.auth_data})
        resp = server.getresponse()
        output = str(resp.read())
        server.close()
        self.assertEqual(resp.status, 200, msg="ERROR reconfiguring:" + output)

        all_configured = self.wait_inf_state(VirtualMachine.CONFIGURED, 300)
        self.assertTrue(
            all_configured, msg="ERROR waiting the infrastructure to be configured (timeout).")

    def test_57_reconfigure_list(self):
        server = httplib.HTTPConnection(HOSTNAME, TEST_PORT)
        server.request('PUT', "/infrastructures/" + self.inf_id + "/reconfigure?vm_list=0",
                       headers={'AUTHORIZATION': self.auth_data})
        resp = server.getresponse()
        output = str(resp.read())
        server.close()
        self.assertEqual(resp.status, 200, msg="ERROR reconfiguring:" + output)

        all_configured = self.wait_inf_state(VirtualMachine.CONFIGURED, 300)
        self.assertTrue(
            all_configured, msg="ERROR waiting the infrastructure to be configured (timeout).")

    def test_60_stop(self):
        time.sleep(10)
        server = httplib.HTTPConnection(HOSTNAME, TEST_PORT)
        server.request('PUT', "/infrastructures/" + self.inf_id + "/stop",
                       headers={'AUTHORIZATION': self.auth_data})
        resp = server.getresponse()
        output = str(resp.read())
        server.close()
        self.assertEqual(resp.status, 200,
                         msg="ERROR stopping the infrastructure:" + output)
        time.sleep(10)

        all_stopped = self.wait_inf_state(
            VirtualMachine.STOPPED, 120, [VirtualMachine.RUNNING])
        self.assertTrue(
            all_stopped, msg="ERROR waiting the infrastructure to be stopped (timeout).")

    def test_70_start(self):
        # To assure the VM is stopped
        time.sleep(10)
        server = httplib.HTTPConnection(HOSTNAME, TEST_PORT)
        server.request('PUT', "/infrastructures/" + self.inf_id + "/start",
                       headers={'AUTHORIZATION': self.auth_data})
        resp = server.getresponse()
        output = str(resp.read())
        server.close()
        self.assertEqual(resp.status, 200,
                         msg="ERROR starting the infrastructure:" + output)
        time.sleep(10)

        all_configured = self.wait_inf_state(
            VirtualMachine.CONFIGURED, 120, [VirtualMachine.RUNNING])
        self.assertTrue(
            all_configured, msg="ERROR waiting the infrastructure to be started (timeout).")

    def test_80_stop_vm(self):
        time.sleep(10)
        server = httplib.HTTPConnection(HOSTNAME, TEST_PORT)
        server.request('PUT', "/infrastructures/" + self.inf_id + "/vms/0/stop",
                       headers={'AUTHORIZATION': self.auth_data})
        resp = server.getresponse()
        output = str(resp.read())
        server.close()
        self.assertEqual(resp.status, 200,
                         msg="ERROR stopping the vm:" + output)
        time.sleep(10)

        all_stopped = self.wait_inf_state(VirtualMachine.STOPPED, 120, [
                                          VirtualMachine.RUNNING], ["/infrastructures/" + self.inf_id + "/vms/0"])
        self.assertTrue(
            all_stopped, msg="ERROR waiting the infrastructure to be stopped (timeout).")

    def test_90_start_vm(self):
        # To assure the VM is stopped
        time.sleep(10)
        server = httplib.HTTPConnection(HOSTNAME, TEST_PORT)
        server.request('PUT', "/infrastructures/" + self.inf_id + "/vms/0/start",
                       headers={'AUTHORIZATION': self.auth_data})
        resp = server.getresponse()
        output = str(resp.read())
        server.close()
        self.assertEqual(resp.status, 200,
                         msg="ERROR starting the vm:" + output)
        time.sleep(10)

        all_configured = self.wait_inf_state(VirtualMachine.CONFIGURED, 120, [
                                             VirtualMachine.RUNNING], ["/infrastructures/" + self.inf_id + "/vms/0"])
        self.assertTrue(
            all_configured, msg="ERROR waiting the vm to be started (timeout).")

    def test_95_destroy(self):
        server = httplib.HTTPConnection(HOSTNAME, TEST_PORT)
        server.request('DELETE', "/infrastructures/" + self.inf_id,
                       headers={'Authorization': self.auth_data})
        resp = server.getresponse()
        output = str(resp.read())
        server.close()
        self.assertEqual(resp.status, 200,
                         msg="ERROR destroying the infrastructure:" + output)

if __name__ == '__main__':
    unittest.main()
