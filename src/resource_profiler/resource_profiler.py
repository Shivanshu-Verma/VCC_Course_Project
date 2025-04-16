from kubernetes import client, config
from kubernetes.client.rest import ApiException
import time
import threading
from flask import Flask, jsonify

# For in-cluster deployment use load_incluster_config(); for local tests use load_kube_config()
config.load_incluster_config()
core_v1 = client.CoreV1Api()
custom_api = client.CustomObjectsApi()

def update_node_profiles():
    while True:
        try:
            nodes = core_v1.list_node().items
            for node in nodes:
                name = node.metadata.name
                labels = node.metadata.labels
                if "workload" not in labels:
                    continue  # Only update nodes that are part of our HTAS system
                
                capacity = node.status.capacity
                allocatable = node.status.allocatable
                
                # For demonstration, we assume "used" resources are calculated from running pods.
                pods = core_v1.list_pod_for_all_namespaces(field_selector=f"spec.nodeName={name}").items
                # In practice, parse container resources and sum them (this is left as an exercise)
                used_cpu = sum(container.resources.requests.get('cpu', 0) for pod in pods 
                            for container in pod.spec.containers if pod.status.phase == 'Running')
                used_memory = sum(container.resources.requests.get('memory', 0) for pod in pods 
                               for container in pod.spec.containers if pod.status.phase == 'Running')
            
            # Calculate available resources
            cpu_available = allocatable['cpu'] - used_cpu
            memory_available = allocatable['memory'] - used_memory
                
                # Update or create a NodeProfile
            body = {
                "apiVersion": "htas.cloud/v1",
                "kind": "NodeProfile",
                "metadata": {"name": name},
                "spec": {
                    "instanceName": name,
                    "instanceType": labels.get("beta.kubernetes.io/instance-type", "unknown"),
                    "cpuCapacity": int(float(capacity['cpu']) * 1000),  # in millicores
                    "memoryCapacity": memory_available,
                    "cpuAvailable": cpu_available,
                    "memoryAvailable": memory_available,
                    "runtime": 0  # updated later by Task Packer
                }
            }
                
            try:
                custom_api.get_namespaced_custom_object(
                    group="htas.cloud", version="v1", namespace="default",
                    plural="nodeprofiles", name=name)
                custom_api.replace_namespaced_custom_object(
                    group="htas.cloud", version="v1", namespace="default",
                    plural="nodeprofiles", name=name, body=body)
            except ApiException:
                custom_api.create_namespaced_custom_object(
                    group="htas.cloud", version="v1", namespace="default",
                    plural="nodeprofiles", body=body)
        except ApiException as e:
            print(f"Error updating node profiles: {e}")
        time.sleep(20)

# Start the update thread
update_thread = threading.Thread(target=update_node_profiles)
update_thread.daemon = True
update_thread.start()

# Expose a simple REST API to serve node profiles
app = Flask(__name__)
@app.route('/nodes', methods=['GET'])
def get_nodes():
    try:
        np_list = custom_api.list_namespaced_custom_object(
            group="htas.cloud", version="v1", namespace="default", plural="nodeprofiles")
        return jsonify(np_list)
    except ApiException as e:
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
