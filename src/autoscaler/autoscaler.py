#!/usr/bin/env python3
import time
from kubernetes import client, config
import google.auth
import google.auth.transport.requests
from googleapiclient.discovery import build

# -----------------------------------------------------------------------------
# Initialization and GCP setup
# -----------------------------------------------------------------------------

config.load_incluster_config()
core_api = client.CoreV1Api()
custom_api = client.CustomObjectsApi()

credentials, project = google.auth.default()
auth_req = google.auth.transport.requests.Request()
credentials.refresh(auth_req)
compute = build('compute', 'v1', credentials=credentials)

ZONE = "us-central1-a"

# Define available node pool types for simulation purposes
# (NOTE: VM_FLAVORS kept for simulation and cost analysis)
VM_FLAVORS = [
    {"name": "e2-micro", "cpu": 2, "memory": 1, "price": 0.0060},
    {"name": "e2-standard-2", "cpu": 2, "memory": 8, "price": 0.0686}
]

# -----------------------------------------------------------------------------
# Helper Functions
# -----------------------------------------------------------------------------

def calculate_score(flavor, cpu_usage, memory_usage):
    normalized_cpu = min(cpu_usage / flavor["cpu"], 1.0)
    normalized_memory = min(memory_usage / flavor["memory"], 1.0)
    return (0.5 * normalized_cpu + 0.5 * normalized_memory) / flavor["price"]

def get_pod_cpu_request(pod):
    try:
        cpu = pod.spec.containers[0].resources.requests.get('cpu', '0')
        return float(cpu.replace('m', '')) / 1000 if 'm' in cpu else float(cpu)
    except Exception:
        return 0.0

def get_pod_memory_request(pod):
    try:
        mem = pod.spec.containers[0].resources.requests.get('memory', '0')
        if mem.endswith("Mi"):
            return float(mem.replace("Mi", ""))
        elif mem.endswith("Gi"):
            return float(mem.replace("Gi", "")) * 1024
        return float(mem)
    except Exception:
        return 0.0

# -----------------------------------------------------------------------------
# Autoscaling Algorithms
# -----------------------------------------------------------------------------

def greedy_autoscaling(pending_pods, vm_flavors):
    total_cpu = sum(get_pod_cpu_request(p) for p in pending_pods)
    total_mem = sum(get_pod_memory_request(p) for p in pending_pods)
    selected = []

    while total_cpu > 0 or total_mem > 0:
        best = None
        best_score = -1
        for flavor in vm_flavors:
            score = calculate_score(flavor, min(total_cpu, flavor["cpu"]),
                                      min(total_mem, flavor["memory"]))
            if score > best_score:
                best_score = score
                best = flavor
        if not best:
            break
        selected.append(best)
        total_cpu -= best["cpu"]
        total_mem -= best["memory"]

    return selected

def batch_node_autoscaling(pending_pods, batch_nodes, scaling_cycle, vm_flavors):
    total_cpu = sum(get_pod_cpu_request(p) for p in pending_pods)
    total_mem = sum(get_pod_memory_request(p) for p in pending_pods)

    zero_bin_nodes = [node for node in batch_nodes if node["spec"].get("runtime", scaling_cycle) < scaling_cycle]
    for node in zero_bin_nodes:
        total_cpu -= node["spec"].get("cpuCapacity", 0)
        total_mem -= node["spec"].get("memoryCapacity", 0)
    
    if total_cpu <= 0 and total_mem <= 0:
        return []

    return greedy_autoscaling(pending_pods, vm_flavors)

# -----------------------------------------------------------------------------
# GKE-Compatible Placeholder for Node Provisioning
# -----------------------------------------------------------------------------

def scale_gke_node_pool(workload_type, needed_nodes):
    """
    Uses the GCP Compute API to resize a GKE node pool.
    Requires workload_type -> node_pool_name mapping.
    """
    # Map workload type to actual node pool names
    NODE_POOL_MAPPING = {
        "batch": "batch-pool",
        "long-running": "longrunning-pool"
    }

    node_pool = NODE_POOL_MAPPING.get(workload_type.lower())
    if not node_pool:
        print(f"[Autoscaler] No node pool mapping found for workload type '{workload_type}'")
        return

    try:
        # Fetch current size of the node pool
        container = build("container", "v1", credentials=credentials)
        cluster_name = os.environ.get("GKE_CLUSTER_NAME", "your-cluster-name")  # ❗ Set this in env
        zone = os.environ.get("GCP_ZONE", "us-central1-a")

        node_pool_info = container.projects().zones().clusters().nodePools().get(
            projectId=project,
            zone=zone,
            clusterId=cluster_name,
            nodePoolId=node_pool
        ).execute()

        current_size = node_pool_info["autoscaling"]["enabled"] and node_pool_info["autoscaling"].get("maxNodeCount", 0) or node_pool_info["initialNodeCount"]
        print(f"[Autoscaler] Current size of '{node_pool}': {current_size}")

        # Decide new size: cap to maxNodeCount or allow small buffer
        target_size = current_size + needed_nodes
        max_size = node_pool_info["autoscaling"].get("maxNodeCount", 100)

        if target_size > max_size:
            target_size = max_size

        if target_size <= current_size:
            print(f"[Autoscaler] No scale-up needed. Needed: {needed_nodes}, Current: {current_size}")
            return

        print(f"[Autoscaler] Scaling node pool '{node_pool}' to size {target_size}...")

        # Trigger resize
        container.projects().zones().clusters().nodePools().setSize(
            projectId=project,
            zone=zone,
            clusterId=cluster_name,
            nodePoolId=node_pool,
            body={"nodeCount": target_size}
        ).execute()

        print(f"[Autoscaler] Resize triggered for node pool '{node_pool}' to {target_size} nodes.")

    except Exception as e:
        print(f"[Autoscaler] Failed to scale node pool '{node_pool}': {e}")

# -----------------------------------------------------------------------------
# AutoScaleRequest Handling
# -----------------------------------------------------------------------------

def fetch_autoscale_requests():
    try:
        response = custom_api.list_namespaced_custom_object(
            group="htas.cloud",
            version="v1",
            namespace="default",
            plural="autoscalerequests"
        )
        return response.get("items", [])
    except Exception as e:
        print(f"[Autoscaler] Error fetching AutoScaleRequests: {e}")
        return []

# -----------------------------------------------------------------------------
# Main Loop
# -----------------------------------------------------------------------------

def autoscale_loop():
    scaling_cycle = 300
    while True:
        autoscale_requests = fetch_autoscale_requests()

        for req in autoscale_requests:
            workload_type = req["spec"].get("workloadType", "batch")
            pod_names = req["spec"].get("podNames", [])
            pending_pods = []

            for name in pod_names:
                try:
                    pod = core_api.read_namespaced_pod(name=name, namespace="default")
                    if pod.status.phase == "Pending":
                        pending_pods.append(pod)
                except Exception as e:
                    print(f"[Autoscaler] Error reading pod {name}: {e}")

            if not pending_pods:
                continue

            if workload_type.lower() == "long-running":
                vms_needed = greedy_autoscaling(pending_pods, VM_FLAVORS)
            else:
                batch_nodes = []
                try:
                    nodes_data = custom_api.list_namespaced_custom_object(
                        group="htas.cloud",
                        version="v1",
                        namespace="default",
                        plural="nodeprofiles"
                    )['items']
                    for node in nodes_data:
                        node_name = node["spec"].get("instanceName", "").lower()
                        if "batch" in node_name:
                            batch_nodes.append(node)
                except Exception as e:
                    print(f"[Autoscaler] Error fetching batch node profiles: {e}")
                vms_needed = batch_node_autoscaling(pending_pods, batch_nodes, scaling_cycle, VM_FLAVORS)

            scale_gke_node_pool(workload_type, len(vms_needed))

            try:
                custom_api.delete_namespaced_custom_object(
                    group="htas.cloud",
                    version="v1",
                    namespace="default",
                    plural="autoscalerequests",
                    name=req["metadata"]["name"]
                )
            except Exception as e:
                print(f"[Autoscaler] Failed to delete AutoScaleRequest: {e}")

        time.sleep(30)

if __name__ == "__main__":
    autoscale_loop()
