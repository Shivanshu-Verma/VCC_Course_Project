#!/usr/bin/env python3
from kubernetes import client, config
import requests
import time

# Load Kubernetes configuration (assumes this runs inside a cluster)
config.load_incluster_config()
core_api = client.CoreV1Api()
custom_api = client.CustomObjectsApi()

# URL for resource profiler (optional external service)
RESOURCE_PROFILER_URL = "http://resource-profiler:8080/nodes"

# -----------------------------------------------------------------------------
# Helpers for pod filtering and resource parsing
# -----------------------------------------------------------------------------

def get_pending_pods():
    """
    Retrieve pending pods assigned to our custom scheduler.
    Only returns pods that have schedulerName set to "htas-scheduler".
    """
    pods = core_api.list_namespaced_pod(
        namespace="default",
        field_selector="status.phase=Pending"
    ).items
    return [pod for pod in pods if pod.spec.scheduler_name == "htas-scheduler"]

def get_pod_runtime(pod):
    """
    Extracts an expected 'runtime' (in seconds) from pod annotations.
    Returns a default of 300 seconds if not set.
    """
    try:
        return int(pod.metadata.annotations.get("runtime", "300"))
    except Exception as e:
        print(f"Error retrieving runtime for pod {pod.metadata.name}: {e}")
        return 300

def parse_cpu(cpu_str):
    if cpu_str.endswith("m"):
        try:
            return float(cpu_str[:-1]) / 1000.0
        except:
            return 0.0
    try:
        return float(cpu_str)
    except:
        return 0.0

def parse_memory(mem_str):
    try:
        mem_str = mem_str.lower()
        if mem_str.endswith("mi"):
            return float(mem_str[:-2])
        elif mem_str.endswith("gi"):
            return float(mem_str[:-2]) * 1024
        else:
            return float(mem_str)
    except:
        return 0.0

# -----------------------------------------------------------------------------
# Node Profiles
# -----------------------------------------------------------------------------

def get_node_profiles():
    """
    Retrieves node profiles from the Resource Profiler service.
    Fallback to CRD-based custom objects if the service is unavailable.
    """
    try:
        response = requests.get(RESOURCE_PROFILER_URL, timeout=5)
        if response.status_code == 200:
            data = response.json()
            return data.get("items", [])
        else:
            print(f"Resource profiler responded with code: {response.status_code}")
    except Exception as e:
        print("Failed to get node profiles from resource profiler:", e)

    # Fallback: retrieve custom objects
    try:
        data = custom_api.list_namespaced_custom_object(
            group="htas.cloud",
            version="v1",
            namespace="default",
            plural="nodeprofiles"
        )
        return data.get("items", [])
    except Exception as e:
        print("Failed to get node profiles from custom objects:", e)

    return []

# -----------------------------------------------------------------------------
# Scheduling Algorithms
# -----------------------------------------------------------------------------

def bfd_algorithm(pod, node_group):
    """
    Best-Fit Decreasing (BFD) scheduling for long-running workloads.
    """
    pod_cpu_request = 0.0
    pod_memory_request = 0.0
    try:
        container = pod.spec.containers[0]
        resources = container.resources.requests or {}
        pod_cpu_request = parse_cpu(resources.get("cpu", "0"))
        pod_memory_request = parse_memory(resources.get("memory", "0"))
    except Exception as e:
        print(f"Error parsing resource requests for pod {pod.metadata.name}: {e}")

    suitable_nodes = []
    for node in node_group:
        if (node.get('cpuAvailable', 0) >= pod_cpu_request and 
            node.get('memoryAvailable', 0) >= pod_memory_request):
            suitable_nodes.append(node)

    suitable_nodes.sort(key=lambda n: n.get('memoryAvailable', 0))
    if suitable_nodes:
        return suitable_nodes[0]['instanceName']
    return None

def time_bin_bfd(pod, batch_node_group, scaling_cycle):
    """
    Time-Bin BFD for batch workloads.
    """
    pod_runtime = get_pod_runtime(pod)
    bin_number = pod_runtime // scaling_cycle

    bins = {}
    for node in batch_node_group:
        node_runtime = node.get('runtime', scaling_cycle)
        node_bin = node_runtime // scaling_cycle
        bins.setdefault(node_bin, []).append(node)

    for b in [bin_number] + sorted([x for x in bins if x > bin_number]) + sorted([x for x in bins if x < bin_number], reverse=True):
        if b in bins:
            node_name = bfd_algorithm(pod, bins[b])
            if node_name:
                return node_name
    return None

# -----------------------------------------------------------------------------
# Trigger Autoscaling via AutoScaleRequest CRD
# -----------------------------------------------------------------------------

def trigger_autoscaling(workload_type, pods):
    """
    Creates an AutoScaleRequest custom resource to signal the autoscaler.
    """
    pod_names = [pod.metadata.name for pod in pods]
    request_name = f"asr-{int(time.time())}-{workload_type}"

    body = {
        "apiVersion": "htas.cloud/v1",
        "kind": "AutoScaleRequest",
        "metadata": {
            "name": request_name
        },
        "spec": {
            "workloadType": workload_type,
            "podNames": pod_names
        }
    }

    try:
        custom_api.create_namespaced_custom_object(
            group="htas.cloud",
            version="v1",
            namespace="default",
            plural="autoscalerequests",
            body=body
        )
        print(f"[Task Packer] Autoscaling requested for '{workload_type}' due to pods: {pod_names}")
    except Exception as e:
        print(f"[Task Packer] Failed to create AutoScaleRequest: {e}")

# -----------------------------------------------------------------------------
# Main Scheduling Loop
# -----------------------------------------------------------------------------

def schedule_pods():
    scaling_cycle = 300  # 5 minutes
    while True:
        pending_pods = get_pending_pods()
        if not pending_pods:
            time.sleep(20)
            continue

        node_profiles = get_node_profiles()

        long_running_nodes = []
        batch_nodes = []
        for np in node_profiles:
            spec = np.get("spec", {})
            instance_name = spec.get("instanceName", "")
            if "longrunning" in instance_name.lower():
                long_running_nodes.append(spec)
            elif "batch" in instance_name.lower():
                batch_nodes.append(spec)

        for pod in pending_pods:
            pod_labels = pod.metadata.labels or {}
            pod_type = pod_labels.get("workload-type", "batch").lower()
            target_node = None

            if pod_type == "long-running":
                target_node = bfd_algorithm(pod, long_running_nodes)
            elif pod_type == "batch":
                target_node = time_bin_bfd(pod, batch_nodes, scaling_cycle)
            else:
                target_node = time_bin_bfd(pod, batch_nodes, scaling_cycle)

            if target_node:
                binding = client.V1Binding(
                    metadata=client.V1ObjectMeta(name=pod.metadata.name),
                    target=client.V1ObjectReference(
                        kind="Node",
                        api_version="v1",
                        name=target_node
                    )
                )
                try:
                    core_api.create_namespaced_binding(
                        namespace=pod.metadata.namespace,
                        body=binding
                    )
                    print(f"[Task Packer] Scheduled pod {pod.metadata.name} to node {target_node}")
                except Exception as e:
                    print(f"[Task Packer] Error binding pod {pod.metadata.name}: {e}")
            else:
                print(f"[Task Packer] No suitable node found for pod {pod.metadata.name}")
                trigger_autoscaling(pod_type, [pod])

        time.sleep(20)

if __name__ == "__main__":
    schedule_pods()
