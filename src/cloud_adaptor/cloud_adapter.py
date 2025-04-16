#!/usr/bin/env python3
"""
Cloud Adapter Module

This module provides functions to:
  - Deploy pods on specific nodes (via Kubernetes API bindings)
  - Provision new VM instances using GCP's Compute API
  - Deprovision VM instances when they are no longer needed

Ensure that the environment has an in-cluster Kubernetes configuration (or use kubeconfig locally)
and that the GCP credentials are available.
"""

import time
import subprocess
import os
from kubernetes import client, config
import google.auth
import google.auth.transport.requests
from googleapiclient.discovery import build

class CloudAdapter:
    def _init_(self):
        # Load the Kubernetes configuration from within the cluster.
        # For local testing, consider using config.load_kube_config()
        config.load_incluster_config()
        self.core_api = client.CoreV1Api()
        
        # Set up the GCP Compute API client
        credentials, self.project = google.auth.default()
        auth_req = google.auth.transport.requests.Request()
        credentials.refresh(auth_req)
        self.compute = build('compute', 'v1', credentials=credentials)
        
        # Set zone from environment variable or default value
        self.zone = os.environ.get("GCP_ZONE", "us-central1-a")
    
    def deploy_pod(self, binding_info):
        """
        Deploy a pod onto a specific node using a binding.
        :param binding_info: Dictionary with keys:
               - 'podName': Name of the pod to bind.
               - 'node': Target node name.
        """
        pod_name = binding_info.get('podName')
        target_node = binding_info.get('node')
        binding = client.V1Binding(
            metadata=client.V1ObjectMeta(name=pod_name),
            target=client.V1ObjectReference(kind="Node", apiVersion="v1", name=target_node)
        )
        try:
            self.core_api.create_namespaced_binding(namespace="default", body=binding)
            print(f"[CloudAdapter] Pod '{pod_name}' successfully deployed to node '{target_node}'.")
        except Exception as e:
            print(f"[CloudAdapter] Error deploying pod '{pod_name}': {e}")
    
    def provision_vm(self, vm_config):
        """
        Provision a new VM instance on GCP.
        :param vm_config: Dictionary with configuration parameters:
            - name: (Optional) Instance name; if not provided, one is generated.
            - machineType: String (e.g., 'e2-standard-2').
            - sourceImage: (Optional) Source image URL; defaults to Debian 10 family.
            - startupScript: (Optional) Script to run upon startup (e.g., to join the Kubernetes cluster).
            - labels: (Optional) Dictionary with labels to assign.
        :return: The instance name if provisioning succeeds, otherwise None.
        """
        instance_name = vm_config.get("name")
        if not instance_name:
            instance_name = f"vm-{int(time.time())}"
        
        machine_type = f"zones/{self.zone}/machineTypes/{vm_config.get('machineType', 'e2-standard-2')}"
        source_image = vm_config.get("sourceImage", "projects/debian-cloud/global/images/family/debian-10")
        startup_script = vm_config.get(
            "startupScript",
            "#!/bin/bash\necho 'Hello from VM; please join cluster using kubeadm join [YOUR_JOIN_COMMAND]'"
        )
        
        instance_body = {
            'name': instance_name,
            'machineType': machine_type,
            'disks': [{
                'boot': True,
                'autoDelete': True,
                'initializeParams': {
                    'sourceImage': source_image,
                }
            }],
            'networkInterfaces': [{
                'network': 'global/networks/default',
                'accessConfigs': [{'type': 'ONE_TO_ONE_NAT', 'name': 'External NAT'}]
            }],
            'metadata': {
                'items': [{
                    'key': 'startup-script',
                    'value': startup_script
                }]
            },
            'labels': vm_config.get("labels", {})
        }
        try:
            print(f"[CloudAdapter] Provisioning VM '{instance_name}' (machine type: {machine_type})...")
            operation = self.compute.instances().insert(
                project=self.project,
                zone=self.zone,
                body=instance_body
            ).execute()
            self._wait_for_operation(operation['name'])
            print(f"[CloudAdapter] VM instance '{instance_name}' provisioned successfully.")
            return instance_name
        except Exception as e:
            print(f"[CloudAdapter] Error provisioning VM: {e}")
            return None

    def deprovision_vm(self, instance_name):
        """
        Deprovision a VM instance (delete it) using the GCP Compute API.
        :param instance_name: Name of the VM instance to delete.
        :return: True if deletion is initiated successfully, False otherwise.
        """
        try:
            print(f"[CloudAdapter] Deprovisioning VM instance '{instance_name}'...")
            operation = self.compute.instances().delete(
                project=self.project,
                zone=self.zone,
                instance=instance_name
            ).execute()
            self._wait_for_operation(operation['name'])
            print(f"[CloudAdapter] VM instance '{instance_name}' deprovisioned successfully.")
            return True
        except Exception as e:
            print(f"[CloudAdapter] Error deprovisioning VM instance '{instance_name}': {e}")
            return False
    
    def _wait_for_operation(self, operation_name):
        """
        Poll the operation until it completes.
        :param operation_name: The name of the GCP operation.
        """
        print(f"[CloudAdapter] Waiting for operation '{operation_name}' to complete...")
        while True:
            result = self.compute.zoneOperations().get(
                project=self.project,
                zone=self.zone,
                operation=operation_name
            ).execute()
            if result.get('status') == 'DONE':
                if 'error' in result:
                    raise Exception(result['error'])
                print(f"[CloudAdapter] Operation '{operation_name}' completed.")
                break
            time.sleep(5)

# Example usage
if _name_ == '_main_':
    adapter = CloudAdapter()
    
    # Example: Deploy a pod
    sample_binding = {'podName': 'example-pod', 'node': 'target-node'}
    adapter.deploy_pod(sample_binding)
    
    # Example: Provision a new VM
    vm_configuration = {
        "name": "example-vm",
        "machineType": "e2-standard-2",
        "sourceImage": "projects/debian-cloud/global/images/family/debian-10",
        "startupScript": "#!/bin/bash\necho 'Joining the Kubernetes cluster: run kubeadm join [YOUR_JOIN_COMMAND]'",  # Replace [YOUR_JOIN_COMMAND]
        "labels": {"workload": "longrunning"}
    }
    instance = adapter.provision_vm(vm_configuration)
    
    # Example: Deprovision the VM if it was provisioned
    if instance:
        adapter.deprovision_vm(instance)
