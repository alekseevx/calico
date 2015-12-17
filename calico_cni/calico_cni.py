# Copyright 2015 Metaswitch Networks
#
# Licensed under the Apache License, Version 2.0 (the 'License');
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an 'AS IS' BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import print_function
import logging
import json
import os
import sys

from subprocess import Popen, PIPE 
from netaddr import IPNetwork, AddrFormatError

from pycalico import netns
from pycalico.netns import Namespace, remove_veth, CalledProcessError
from pycalico.datastore import DatastoreClient
from pycalico.datastore_errors import MultipleEndpointsMatch
from util import (configure_logging, parse_cni_args, print_cni_error, 
        handle_datastore_error)
from constants import *

import policy_drivers
from container_engines import DefaultEngine, DockerEngine

# Logging configuration.
LOG_FILENAME = "cni.log"
_log = logging.getLogger(__name__)


class CniPlugin(object):
    """
    Class which encapsulates the function of a CNI plugin.
    """
    def __init__(self, network_config, env):
        self.network_config = network_config
        """
        Network config as provided in the CNI network file passed in
        via stdout.
        """

        self.env = env
        """
        Copy of the environment variable dictionary. Contains CNI_* 
        variables.
        """

        self._client = DatastoreClient()
        """
        DatastoreClient for access to the Calico datastore.
        """

        self.command = env[CNI_COMMAND_ENV]
        """
        The command to execute for this plugin instance. Required. 
        One of:
          - CNI_CMD_ADD
          - CNI_CMD_DELETE
        """

        self.container_id = env[CNI_CONTAINERID_ENV]
        """
        The container's ID in the containerizer. Required.
        """

        self.cni_netns = env[CNI_NETNS_ENV]
        """
        Relative path to the network namespace of this container.
        """

        self.interface = env[CNI_IFNAME_ENV]
        """
        Name of the interface to create within the container.
        """

        self.cni_args = parse_cni_args(env[CNI_ARGS_ENV])
        """
        Dictionary of additional CNI arguments provided via
        the CNI_ARGS environment variable.
        """

        self.cni_path = env[CNI_PATH_ENV]
        """
        Path in which to search for CNI plugins.
        """

        self.network_name = network_config["name"]
        """
        Name of the network from the provided network config file.
        """

        self.ipam_result = None
        """
        Stores the output generated by the IPAM plugin.  This is printed
        to stdout at the end of execution.
        """

        self.policy_driver = self._get_policy_driver()
        """
        Chooses the correct policy driver based on the given configuration
        """

        self.container_engine = self._get_container_engine()
        """
        Chooses the correct container engine based on the given configuration.
        """

    def execute(self):
        """Executes this plugin.
        Handles unexpected Exceptions in plugin execution.

        :return The plugin return code.
        """
        rc = 0
        try:
            _log.info("Starting Calico CNI plugin execution")
            self._execute()
        except SystemExit, e:
            # SystemExit indicates an error that was handled earlier
            # in the stack.  Just set the return code.
            rc = e.code
        except BaseException:
            # An unexpected Exception has bubbled up - catch it and
            # log it out.
            _log.exception("Unhandled Exception killed plugin")
            rc = ERR_CODE_UNHANDLED
            print_cni_error(rc, "Unhandled Exception killed plugin")
        finally:
            _log.info("Calico CNI execution complete, rc=%s", rc)
            return rc

    def _execute(self):
        """Private method to execute this plugin.

        Uses the given CNI_COMMAND to determine which action to take.

        :return: None.
        """
        if self.command == CNI_CMD_ADD:
            self.add()
        else:
            assert self.command == CNI_CMD_DELETE, \
                    "Invalid command: %s" % self.command
            self.delete()

    def add(self):
        """"Handles CNI_CMD_ADD requests. 

        Configures Calico networking and prints required json to stdout.

        In CNI, a container can be added to multiple networks, in which case
        the CNI plugin will be called multiple times.  In Calico, each network
        is represented by a profile, and each container only receives a single
        endpoint / veth / IP address even when it is on multiple CNI networks.

        :return: None.
        """
        # If this container uses host networking, don't network it.
        if self.container_engine.uses_host_networking(self.container_id):
            _log.info("Cannot network container %s since it is configured "
                      "with host networking.", self.container_id)
            sys.exit(0)

        _log.info("Configuring networking for container: %s", 
                  self.container_id)

        _log.debug("Checking for existing Calico endpoint")
        endpoint = self._get_endpoint()
        if endpoint:
            # This endpoint already exists, add it to another network.
            _log.info("Endpoint for container exists - add to new network")
            output = self._add_existing_endpoint(endpoint)
        else:
            # No endpoint exists - we need to configure a new one.
            _log.info("Configuring a new Endpoint for container")
            output = self._add_new_endpoint()

        # If all successful, print the IPAM plugin's output to stdout.
        dump = json.dumps(output)
        _log.debug("Printing CNI result to stdout: %s", dump)
        print(dump)

        _log.info("Finished networking container: %s", self.container_id)

    def _add_new_endpoint(self):
        """
        Handled adding a new container to a Calico network.
        """
        # Assign IP addresses using the given IPAM plugin.
        ipv4, ipv6 = self._assign_ips(self.env)

        # Create the Calico endpoint object.  For now, we only 
        # support creating endpoints with IPv4.
        endpoint = self._create_endpoint([ipv4])
    
        # Provision the veth for this endpoint.
        endpoint = self._provision_veth(endpoint)
        
        # Provision / apply profile on the created endpoint.
        try:
            self.policy_driver.apply_profile(endpoint)
        except policy_drivers.ApplyProfileError, e:
            _log.error("Failed to create set profile for endpoint %s",
                       endpoint.name)
            self._remove_veth(endpoint)
            self._remove_endpoint()
            env = self.env.copy()
            env[CNI_COMMAND_ENV] = CNI_CMD_DELETE
            self._release_ip(env)
            print_cni_error(ERR_CODE_GENERIC, e.message)
            sys.exit(ERR_CODE_GENERIC)

        # Return the IPAM plugin's result.
        return self.ipam_result

    def _add_existing_endpoint(self, endpoint):
        """
        Handles adding an existing container to a new Calico network.

        We've already assigned an IP address and created the veth,
        we just need to apply a new profile to this endpoint.
        """
        # Get the already existing IP information for this Endpoint. 
        try:
            ip4 = next(iter(endpoint.ipv4_nets))
        except StopIteration:
            # No IPv4 address on existing endpoint.
            _log.error("No IPV4 address attached to existing endpoint")
            print_cni_error(ERR_CODE_GENERIC, 
                    "Cannot add network - no IPv4 address")
            sys.exit(ERR_CODE_GENERIC)

        try:
            ip6 = next(iter(endpoint.ipv6_nets))
        except StopIteration:
            # Not all deployments will use IPv6 - don't treat this as an error,
            # but log a warning and use an null IPv6 address.
            _log.warning("No IPV6 address attached to existing endpoint")
            ip6 = IPNetwork("::/128")

        # Apply a new profile to this endpoint.
        self.policy_driver.apply_profile(endpoint)

        return {"ip4": {"ip": str(ip4.cidr)}, 
                "ip6": {"ip": str(ip6.cidr)}}
    
    def delete(self):
        """Handles CNI_CMD_DELETE requests.

        Remove this container from Calico networking.

        :return: None.
        """
        _log.info("Remove networking from container: %s", self.container_id)

        # Step 1: Remove any IP assignments.
        self._release_ip(self.env)

        # Step 2: Get the Calico endpoint for this workload. If it does not
        # exist, log a warning and exit successfully.
        endpoint = self._get_endpoint()
        if not endpoint:
            _log.warning("No Calico Endpoint for container: %s",
                         self.container_id)
            sys.exit(0)

        # Step 3: Delete the veth interface for this endpoint.
        self._remove_veth(endpoint)

        # Step 4: Delete the Calico endpoint.
        self._remove_endpoint()

        # Step 5: Delete any profiles for this endpoint
        self.policy_driver.remove_profile()

        _log.info("Finished removing container: %s", self.container_id)

    def _assign_ips(self, env):
        """Assigns and returns an IPv4 address using the IPAM plugin
        specified in the network config file.

        :return: ipv4, ipv6 - The IP addresses assigned by the IPAM plugin.
        """
        # Call the IPAM plugin.  Returns the plugin returncode,
        # as well as the CNI result from stdout.
        _log.debug("Assigning IP address")
        assert env[CNI_COMMAND_ENV] == CNI_CMD_ADD
        rc, result = self._call_ipam_plugin(env)

        try:
            # Load the response
            self.ipam_result = json.loads(result)
        except ValueError:
            message = "Failed to parse IPAM response, exiting"
            _log.exception(message)
            print_cni_error(ERR_CODE_GENERIC, message)
            sys.exit(ERR_CODE_GENERIC)

        if rc:
            # The IPAM plugin failed to assign an IP address. At this point in
            # execution, we haven't done anything yet, so we don't have to
            # clean up.
            _log.error("IPAM plugin error (rc=%s): %s", rc, result)
            code = self.ipam_result.get("code", ERR_CODE_GENERIC)
            msg = self.ipam_result.get("msg", "Unknown IPAM error")
            details = self.ipam_result.get("details")
            print_cni_error(code, msg, details)
            sys.exit(int(code))

        try:
            ipv4 = IPNetwork(self.ipam_result["ip4"]["ip"])
        except KeyError:
            message = "IPAM plugin did not return an IPv4 address."
            print_cni_error(ERR_CODE_GENERIC, message)
            sys.exit(ERR_CODE_GENERIC)
        except (AddrFormatError, ValueError):
            message = "Invalid or Empty IPv4 address: %s" % \
                      (self.ipam_result["ip4"]["ip"])
            print_cni_error(ERR_CODE_GENERIC, message)
            sys.exit(ERR_CODE_GENERIC)

        try:
            ipv6 = IPNetwork(self.ipam_result["ip6"]["ip"])
        except KeyError:
            message = "IPAM plugin did not return an IPv6 address."
            print_cni_error(ERR_CODE_GENERIC, message)
            sys.exit(ERR_CODE_GENERIC)
        except (AddrFormatError, ValueError):
            message = "Invalid or Empty IPv6 address: %s" % \
                      (self.ipam_result["ip6"]["ip"])
            print_cni_error(ERR_CODE_GENERIC, message)
            sys.exit(ERR_CODE_GENERIC)

        _log.info("IPAM plugin assigned IPv4 address: %s", ipv4)
        _log.info("IPAM plugin assigned IPv6 address: %s", ipv6)
        return ipv4, ipv6

    def _release_ip(self, env):
        """Releases the IP address(es) for this container using the IPAM plugin
        specified in the network config file.

        :param env  - A dictionary of environment variables to pass to the
        IPAM plugin
        :return: None.
        """
        _log.info("Releasing IP address")
        assert env[CNI_COMMAND_ENV] == CNI_CMD_DELETE
        rc, result = self._call_ipam_plugin(env)

        if rc:
            _log.error("IPAM plugin failed to release IP address")

    def _call_ipam_plugin(self, env):
        """Calls through to the specified IPAM plugin.
    
        Utilizes the IPAM config as specified in the CNI network
        configuration file.  A dictionary with the following form:
            {
              type: <IPAM TYPE>
            }

        :param env  - A dictionary of environment variables to pass to the
        IPAM plugin
        :return: Response from the IPAM plugin.
        """
        # Find the correct plugin based on the given type.
        plugin_path = self._find_ipam_plugin()
        if not plugin_path:
            message = "Could not find IPAM plugin of type %s in path %s." % \
                      (self.network_config['ipam']['type'], self.cni_path)
            print_cni_error(ERR_CODE_GENERIC, message)
            sys.exit(ERR_CODE_GENERIC)
    
        # Execute the plugin and return the result.
        _log.info("Using IPAM plugin at: %s", plugin_path)
        _log.debug("Passing in environment to IPAM plugin: \n%s",
                   json.dumps(env, indent=2))
        p = Popen(plugin_path, stdin=PIPE, stdout=PIPE, stderr=PIPE, env=env)
        stdout, stderr = p.communicate(json.dumps(self.network_config))
        _log.debug("IPAM plugin return code: %s", p.returncode)
        _log.debug("IPAM plugin output: \nstdout:\n%s\nstderr:\n%s", 
                   stdout, stderr)
        return p.returncode, stdout

    def _create_endpoint(self, ip_list):
        """Creates an endpoint in the Calico datastore with the client.

        :param ip_list - list of IP addresses that has been already allocated
        :return Calico endpoint object
        """
        _log.debug("Creating Calico endpoint")
        try:
            endpoint = self._client.create_endpoint(HOSTNAME,
                                                    ORCHESTRATOR_ID,
                                                    self.container_id,
                                                    ip_list)
        except (AddrFormatError, KeyError), e:
            # AddrFormatError: Raised when an IP address type is not 
            #                  compatible with the node.
            # KeyError: Raised when BGP config for host is not found.
            _log.exception("Failed to create Calico endpoint.")
            env = self.env.copy()
            env[CNI_COMMAND_ENV] = CNI_CMD_DELETE
            self._release_ip(env)
            print_cni_error(ERR_CODE_GENERIC, e.message)
            sys.exit(ERR_CODE_GENERIC)

        _log.info("Created Calico endpoint with IP address(es) %s", ip_list)
        return endpoint

    def _remove_endpoint(self):
        """Removes the given endpoint from the Calico datastore

        :param endpoint:
        :return: None
        """
        try:
            _log.info("Removing endpoint from the Calico datastore")
            self._client.remove_workload(hostname=HOSTNAME,
                                         orchestrator_id=ORCHESTRATOR_ID,
                                         workload_id=self.container_id)
        except KeyError:
            _log.warning("Unable to remove workload with ID %s from datastore.",
                         self.container_id)

    def _provision_veth(self, endpoint):
        """Provisions veth for given endpoint.

        Uses the netns relative path passed in through CNI_NETNS_ENV and
        interface passed in through CNI_IFNAME_ENV.

        :param endpoint
        :return Calico endpoint object
        """
        _log.debug("Provisioning Calico veth interface")
        netns_path = os.path.abspath(os.path.join(os.getcwd(), self.cni_netns))
        _log.debug("netns path: %s", netns_path)

        try:
            endpoint.mac = endpoint.provision_veth(
                Namespace(netns_path), self.interface)
        except CalledProcessError, e:
            _log.exception("Failed to provision veth interface for endpoint %s",
                           endpoint.name)
            self._remove_endpoint()
            env = self.env.copy()
            env[CNI_COMMAND_ENV] = CNI_CMD_DELETE
            self._release_ip(env)
            print_cni_error(ERR_CODE_GENERIC, e.message)
            sys.exit(ERR_CODE_GENERIC)

        _log.debug("Endpoint has mac address: %s", endpoint.mac)

        self._client.set_endpoint(endpoint)
        _log.info("Provisioned %s in netns %s", self.interface, netns_path)
        return endpoint

    def _remove_veth(self, endpoint):
        """Remove the veth from given endpoint

        Handles errors and logs warnings if operation was unsuccessful

        :return: Boolean - True if veth was removed, False if veth 
        could not be removed
        """
        _log.info("Removing veth for endpoint: %s", endpoint.name)
        try:
            if not netns.remove_veth(endpoint.name):
                _log.warning("Veth %s does not exist", endpoint.name)
                return False
        except CalledProcessError:
            _log.warning("Unable to remove veth %s", endpoint.name)
            return False

        return True

    def _get_container_engine(self):
        """Returns a container engine based on the CNI configuration arguments.

        :return: a container engine of type BaseContainerEngine.
        """
        if K8S_POD_NAME in self.cni_args:
            _log.debug("Using Kubernetes + Docker container engine")
            return DockerEngine()
        else:
            _log.debug("Using default container engine")
            return DefaultEngine()

    def _get_policy_driver(self):
        """Returns a policy driver based on CNI configuration arguments.

        :return: a policy driver of type BasePolicyDriver
        """
        try:
            self.cni_args[K8S_POD_NAME]
        except KeyError:
            _log.debug("Using default policy driver")
            try:
                driver = policy_drivers.DefaultPolicyDriver(self.network_name)
            except ValueError, e:
                # ValueError raised because profile name passed into
                # policy driver contains illegal characters
                print_cni_error(ERR_CODE_GENERIC, e.message)
                sys.exit(ERR_CODE_GENERIC)
        else:
            _log.debug("Using Default Kubernetes Policy Driver")
            driver = policy_drivers.KubernetesDefaultPolicyDriver(
                    self.network_name
            )

        return driver

    @handle_datastore_error
    def _get_endpoint(self):
        """Gets endpoint matching the container_id.

        Return None if no endpoint is found.
        Exits with an error if multiple endpoints are found.

        :param container_id:
        :return: Calico endpoint object if found, None if not found
        """
        try:
            _log.debug("Looking for endpoint that matches container ID %s",
                      self.container_id)
            endpoint = self._client.get_endpoint(
                hostname=HOSTNAME,
                orchestrator_id=ORCHESTRATOR_ID,
                workload_id=self.container_id
            )
        except KeyError:
            _log.debug("No endpoint found matching ID %s", self.container_id)
            endpoint = None
        except MultipleEndpointsMatch:
            message = "Multiple Endpoints found matching ID %s" % \
                    self.container_id
            print_cni_error(ERR_CODE_GENERIC, message)
            sys.exit(ERR_CODE_GENERIC)

        return endpoint

    def _find_ipam_plugin(self):
        """Locates IPAM plugin binary in plugin path and returns absolute path
        of plugin if found; if not found returns an empty string.

        IPAM plugin type is set in the network config file.
        The plugin path is the CNI path passed through the environment variable
        CNI_PATH.

        :rtype : str
        :return: plugin_path - absolute path of IPAM plugin binary
        """
        plugin_type = self.network_config['ipam']['type']
        plugin_path = ""
        for path in self.cni_path.split(":"):
            _log.debug("Looking for plugin %s in path %s", plugin_type, path)
            temp_path = os.path.abspath(os.path.join(path, plugin_type))
            if os.path.isfile(temp_path):
                _log.debug("Found plugin %s in path %s", plugin_type, path)
                plugin_path = temp_path
                break
        return str(plugin_path)


def main():
    """
    Main function - configures and runs the plugin.
    """
    # Read the network config file from stdin. 
    config_raw = ''.join(sys.stdin.readlines()).replace('\n', '')
    network_config = json.loads(config_raw).copy()

    # Get the log level from the config file, default to INFO.
    log_level = network_config.get(LOG_LEVEL_KEY, "INFO").upper()

    # Configure logging.
    configure_logging(_log, LOG_FILENAME, log_level=log_level)
    _log.debug("Loaded network config:\n%s", 
               json.dumps(network_config, indent=2))

    # Get the etcd authority from the config file. Set the 
    # environment variable.
    etcd_authority = network_config.get(ETCD_AUTHORITY_KEY, 
                                        DEFAULT_ETCD_AUTHORITY)
    os.environ[ETCD_AUTHORITY_ENV] = etcd_authority
    _log.debug("Using ETCD_AUTHORITY=%s", etcd_authority)

    # Get the CNI environment. 
    env = os.environ.copy()
    _log.debug("Loaded environment:\n%s", json.dumps(env, indent=2))

    # Create the plugin, passing in the network config, environment,
    # and the Calico configuration options.
    plugin = CniPlugin(network_config, env)

    # Call the CNI plugin.
    sys.exit(plugin.execute())


if __name__ == '__main__': # pragma: no cover
    try:
        main()
    except Exception, e:
        print("Unhandled Exception in main(): %s" % e)
        sys.exit(ERR_CODE_UNHANDLED)
