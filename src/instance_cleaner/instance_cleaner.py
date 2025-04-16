#!/usr/bin/env python3
import time
import subprocess
import os
from kubernetes import client, config
import google.auth
import google.auth.transport.requests
from googleapiclient import discovery

# Load in-cluster Kubernetes configuration (or use config.load_kube_config() for local testing)
config.load_incluster_config()
api = client.CoreV1Api()
custom_api = client.CustomObjectsApi()

# Set up GCP Compute API for instance deletion
credentials, project = google.auth.default()
auth_req = google.auth.transport.requests.Request()
credentials.refresh(auth_req)
compute = discovery.build('compute', 'v1', credentials=credentials)

# Utilization threshold (in percent) for underutilization
UTILIZATION_THRESHOLD = int(os.environ.get('UTILIZATION_THRESHOLD', '50'))

# Helper: Convert memory string (e.g., "512Mi", "1Gi") to MiB.
def parse_memory(memory_str):
    if isinstance(memory_str, int):
        return memory_str
    if memory_str.endswith('Ki'):
        return int(memory_str[:-2]) / 1024  # convert KiB to MiB
    elif memory_str.endswith('Mi'):
        return int(memory_str[:-2])
    elif memory_str.endswith('Gi'):
        return int(memory_str[:-2]) * 1024
    else:
        return int(memory_str)

# Calculate average resource utilization (CPU and Memory) on a node as a percentage.
def calculate_node_utilization(node_name):
    node = api.read_node(name=node_name)
    capacity = node.status.capacity

    # Get running pods on the node in the default namespace
    field_selector = f"spec.nodeName={node_name},status.phase=Running"
    pods = api.list_namespaced_pod(namespace="default", field_selector=field_selector).items

    used_cpu = sum(
        float(container.resources.requests.get('cpu', '0').replace('m', '')) / 1000
        for pod in pods
        for container in pod.spec.containers
        if container.resources and container.resources.requests.get('cpu')
    )

    used_memory = sum(
        parse_memory(container.resources.requests.get('memory', '0'))
        for pod in pods
        for container in pod.spec.containers
        if container.resources and container.resources.requests.get('memory')
    )

    cpu_capacity = float(capacity['cpu'])
    memory_capacity = parse_memory(capacity['memory'])

    cpu_utilization = (used_cpu / cpu_capacity) * 100 if cpu_capacity > 0 else 0
    memory_utilization = (used_memory / memory_capacity) * 100 if memory_capacity > 0 else 0

    avg_utilization = (cpu_utilization + memory_utilization) / 2
    return avg_utilization

# Check whether a given node has sufficient available resources for a pod.
def check_node_resources(node, pod):
    allocatable = node.status.allocatable

    pod_cpu = sum(
        float(container.resources.requests.get('cpu', '0').replace('m', '')) / 1000
        for container in pod.spec.containers
        if container.resources and container.resources.requests.get('cpu')
    )
    pod_memory = sum(
        parse_memory(container.resources.requests.get('memory', '0'))
        for container in pod.spec.containers
        if container.resources and container.resources.requests.get('memory')
    )

    # Get current usage on the node
    field_selector = f"spec.nodeName={node.metadata.name},status.phase=Running"
    node_pods = api.list_namespaced_pod(namespace="default", field_selector=field_selector).items
    current_cpu = sum(
        float(container.resources.requests.get('cpu', '0').replace('m', '')) / 1000
        for p in node_pods
        for container in p.spec.containers
        if container.resources and container.resources.requests.get('cpu')
    )
    current_memory = sum(
        parse_memory(container.resources.requests.get('memory', '0'))
        for p in node_pods
        for container in p.spec.containers
        if container.resources and container.resources.requests.get('memory')
    )

    cpu_available = float(allocatable['cpu']) - current_cpu
    memory_available = parse_memory(allocatable['memory']) - current_memory

    return cpu_available >= pod_cpu and memory_available >= pod_memory

# Migrate a container from the current pod to a target node using CRIU for checkpoint/restore.
def migrate_container(pod, target_node):
    pod_name = pod.metadata.name
    namespace = pod.metadata.namespace
    source_node = pod.spec.node_name
    # Assume single-container pod; adjust if multi-container logic is needed.
    container_name = pod.spec.containers[0].name

    print(f"Migrating pod {pod_name} from {source_node} to {target_node}")

    try:
        # Step 1: Checkpoint the container on the source node
        checkpoint_cmd = (
            f"kubectl exec {pod_name} -n {namespace} -c {container_name} -- "
            f"criu dump --tree $(pgrep -f {container_name}) --images-dir /tmp/checkpoint "
            f"--shell-job --leave-running"
        )
        subprocess.run(checkpoint_cmd, shell=True, check=True)
        
        # Step 2: Copy checkpoint data from the source pod to local storage
        local_checkpoint_path = f"/tmp/checkpoint-{pod_name}"
        copy_cmd = f"kubectl cp {namespace}/{pod_name}:/tmp/checkpoint {local_checkpoint_path}"
        subprocess.run(copy_cmd, shell=True, check=True)
        
        # Step 3: Create a new pod on the target node with the same specification
        new_pod_name = f"{pod_name}-migrated"
        new_pod = client.V1Pod(
            metadata=client.V1ObjectMeta(
                name=new_pod_name,
                namespace=namespace,
                labels=pod.metadata.labels  # maintain the same labels
            ),
            spec=client.V1PodSpec(
                containers=pod.spec.containers,
                node_name=target_node,
                restart_policy="Never"
            )
        )
        api.create_namespaced_pod(namespace=namespace, body=new_pod)
        print(f"Created new pod {new_pod_name} on target node {target_node}")
        
        # Wait briefly for the new pod to start up (this is a simple delay)
        time.sleep(30)
        
        # Step 4: Copy the checkpoint data into the new pod
        copy_to_new_cmd = (
            f"kubectl cp {local_checkpoint_path} {namespace}/{new_pod_name}:/tmp/checkpoint"
        )
        subprocess.run(copy_to_new_cmd, shell=True, check=True)
        
        # Step 5: Restore the container from the checkpoint in the new pod
        restore_cmd = (
            f"kubectl exec {new_pod_name} -n {namespace} -c {container_name} -- "
            f"criu restore --images-dir /tmp/checkpoint --shell-job"
        )
        subprocess.run(restore_cmd, shell=True, check=True)
        
        # Step 6: Delete the original pod
        api.delete_namespaced_pod(name=pod_name, namespace=namespace)
        print(f"Migration of pod {pod_name} to {target_node} completed successfully.")
        return True
    except Exception as e:
        print(f"Migration failed for pod {pod_name}: {e}")
        return False

# Reschedule all batch pods from an underutilized node and deprovision the node if successful.
def reschedule_node(node_name):
    # Retrieve all running pods on this node
    field_selector = f"spec.nodeName={node_name},status.phase=Running"
    pods = api.list_namespaced_pod(namespace="default", field_selector=field_selector).items

    # Filter only batch jobs based on the 'workload-type' label
    batch_pods = [pod for pod in pods if pod.metadata.labels.get('workload-type') == 'batch']

    if not batch_pods:
        print(f"No batch pods found on node {node_name}, skipping rescheduling.")
        return False

    # Get list of candidate nodes (with workload label 'batch') except the current node
    candidate_nodes = []
    for n in api.list_node().items:
        if n.metadata.name != node_name and n.metadata.labels.get('workload') == 'batch':
            candidate_nodes.append(n)

    if not candidate_nodes:
        print("No candidate nodes available for rescheduling.")
        return False

    migration_success = True
    for pod in batch_pods:
        target = None
        for cand in candidate_nodes:
            if check_node_resources(cand, pod):
                target = cand.metadata.name
                break
        if target:
            success = migrate_container(pod, target)
            if not success:
                migration_success = False
                break
        else:
            print(f"No suitable target found for pod {pod.metadata.name}.")
            migration_success = False
            break

    # If all migrations succeeded, proceed to cordon and deprovision the node.
    if migration_success:
        try:
            # Cordon the node: mark as unschedulable.
            api.patch_node(name=node_name, body={"spec": {"unschedulable": True}})
            print(f"Node {node_name} cordoned successfully.")

            # Delete the node from Kubernetes.
            api.delete_node(name=node_name)
            print(f"Node {node_name} has been deleted from the cluster.")

            # Delete the corresponding VM from GCP.
            # (Assuming the node name is the same as the VM instance name)
            try:
                operation = compute.instances().delete(
                    project=project,
                    zone='us-central1-a',
                    instance=node_name
                ).execute()
                print(f"Deletion of VM instance {node_name} initiated successfully.")
            except Exception as e:
                print(f"Failed to delete VM instance {node_name}: {e}")

            return True
        except Exception as e:
            print(f"Error during node deprovisioning for {node_name}: {e}")
            return False
    else:
        print(f"Migration of pods from node {node_name} was not fully successful.")
        return False

# Main loop: Periodically check all batch nodes for underutilization, reschedule pods if needed.
def check_underutilized_nodes():
    while True:
        # Get all nodes with workload label set to "batch"
        batch_nodes = [node for node in api.list_node().items if node.metadata.labels.get('workload') == 'batch']

        for node in batch_nodes:
            node_name = node.metadata.name
            utilization = calculate_node_utilization(nodse_name)
            print(f"Node {node_name} utilization: {utilization:.2f}%")
            if utilization < UTILIZATION_THRESHOLD:
                print(f"Node {node_name} is underutilized ({utilization:.2f}% < {UTILIZATION_THRESHOLD}%), attempting reschedule.")
                reschedule_node(node_name)
        # Check every 5 minutes.
        time.sleep(300)

if _name_ == '_main_':
    check_underutilized_nodes()
