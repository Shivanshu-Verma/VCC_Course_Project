# VCC Project: Heterogeneous Task Allocation Strategy (HTAS) for Kubernetes Clusters  

This repository contains the implementation of a **Heterogeneous Task Allocation Strategy (HTAS)** for Kubernetes clusters deployed on Google Cloud Platform (GCP). The project is designed to optimize resource utilization and cost-efficiency by leveraging custom scheduling, autoscaling, and node management strategies.  

## Table of Contents  
1. [Overview](#overview)  
2. [Features](#features)  
3. [Folder Structure](#folder-structure)  
4. [Prerequisites](#prerequisites)  
5. [Installation and Setup](#installation-and-setup)  
6. [Building and Pushing Docker Images](#building-and-pushing-docker-images)  
7. [Deploying Kubernetes Resources](#deploying-kubernetes-resources)  
8. [Usage](#usage)  
9. [Testing](#testing)  
10. [Final Notes](#final-notes)  

---

## Overview  

The project implements a Kubernetes orchestration strategy with the following key components:  

- **Resource Profiler**: Tracks available nodes and updates a custom resource (CRD) with details like CPU/memory capacity, usage, and runtime.  
- **Task Packer**: A custom scheduler that uses a Best-Fit Decreasing (BFD) algorithm for long-running services and an extended time-bin BFD for batch jobs.  
- **Autoscaler**: Implements cost-efficient autoscaling algorithms for both long-running and batch workloads.  
- **Instance Cleaner**: Detects underutilized nodes, reschedules pods using container checkpoint/restore (via CRIU), and terminates unnecessary VMs.  

---

## Features  

- **Custom Kubernetes Scheduler**: Optimized for heterogeneous workloads.  
- **Dynamic Autoscaling**: Cost-efficient scaling for long-running and batch jobs.  
- **Node Management**: Automated cleanup of underutilized nodes.  
- **Cloud Integration**: Designed for GCP with APIs for VM provisioning and management.  

---

## Folder Structure  

The project follows a modular structure:  

```
vcc_project  
├── docs  
│      # (Place your documentation and the original VCC paper PDFs/markdown here)  
├── k8s  
│   └── yamls  
│         ├── resource-profiler-crd.yaml  
│         ├── resource-profiler.yaml  
│         ├── task-packer.yaml  
│         ├── autoscaler.yaml  
│         └── instance-cleaner.yaml  
└── src  
    ├── resource_profiler  
    │      ├── Dockerfile  
    │      ├── requirements.txt  
    │      └── resource_profiler.py  
    ├── task_packer  
    │      ├── Dockerfile  
    │      ├── requirements.txt  
    │      └── task_packer.py  
    ├── autoscaler  
    │      ├── Dockerfile  
    │      ├── requirements.txt  
    │      └── autoscaler.py  
    ├── instance_cleaner  
    │      ├── Dockerfile  
    │      ├── requirements.txt  
    │      └── instance_cleaner.py  
    └── cloud_adaptor  
           ├── Dockerfile  
           ├── requirements.txt  
           └── cloud_adaptor.py  
```  

---

## Prerequisites  

Before you begin, ensure you have the following:  

1. **Google Cloud Platform (GCP) Account**  
2. **Google Cloud SDK installed**  
3. **kubectl installed**  
4. **Docker installed and configured**  
5. **APIs enabled**:  
   - `container.googleapis.com`  
   - `compute.googleapis.com`  
   - `monitoring.googleapis.com`  

---

## Installation and Setup  

### Step 1: Install Required Tools  

Run the following commands to install the necessary tools:  

```bash  
# Install Google Cloud SDK and initialize  
curl https://sdk.cloud.google.com | bash  
gcloud init  

# Install kubectl component  
gcloud components install kubectl  

# Enable required APIs on GCP  
gcloud services enable container.googleapis.com compute.googleapis.com monitoring.googleapis.com  
```  

### Step 2: Configure Docker for GCP  

Authenticate Docker with GCP to push images to Google Container Registry (GCR):  

```bash  
gcloud auth configure-docker  
```  

### Step 3: Create Kubernetes Cluster and Node Pools  

Create a Kubernetes cluster with one default node pool and two additional node pools:  

```bash  
# Create the primary cluster  
gcloud container clusters create htas-cluster --zone us-central1-a --num-nodes 1 --machine-type e2-standard-2  

# Create a node pool for long-running services  
gcloud container node-pools create longrunning-pool \  
  --cluster htas-cluster \  
  --zone us-central1-a \  
  --machine-type e2-standard-4 \  
  --num-nodes 2 \  
  --node-labels=workload=longrunning  

# Create a node pool for batch jobs  
gcloud container node-pools create batch-pool \  
  --cluster htas-cluster \  
  --zone us-central1-a \  
  --machine-type e2-standard-2 \  
  --num-nodes 2 \  
  --node-labels=workload=batch  
```  

---

## Building and Pushing Docker Images  

### Step 1: Build Docker Images  

Navigate to each component folder and build the Docker images:  

```bash  
# For resource_profiler:  
cd vcc_project/src/resource_profiler  
docker build -t gcr.io/YOUR_PROJECT_ID/resource-profiler:v1 .  

# For task_packer:  
cd ../task_packer  
docker build -t gcr.io/YOUR_PROJECT_ID/task-packer:v1 .  

# For autoscaler:  
cd ../autoscaler  
docker build -t gcr.io/YOUR_PROJECT_ID/htas-autoscaler:v1 .  

# For instance_cleaner:  
cd ../instance_cleaner  
docker build -t gcr.io/YOUR_PROJECT_ID/instance-cleaner:v1 .  

# For cloud_adaptor:  
cd ../cloud_adaptor  
docker build -t gcr.io/YOUR_PROJECT_ID/cloud-adaptor:v1 .  
```  

### Step 2: Push Docker Images to GCR  

Push the built images to Google Container Registry:  

```bash  
docker push gcr.io/YOUR_PROJECT_ID/resource-profiler:v1  
docker push gcr.io/YOUR_PROJECT_ID/task-packer:v1  
docker push gcr.io/YOUR_PROJECT_ID/htas-autoscaler:v1  
docker push gcr.io/YOUR_PROJECT_ID/instance-cleaner:v1  
docker push gcr.io/YOUR_PROJECT_ID/cloud-adaptor:v1  
```  

Replace `YOUR_PROJECT_ID` with your GCP project ID.  

---

## Deploying Kubernetes Resources  

Apply the Kubernetes YAML files to deploy the components:  

```bash  
kubectl apply -f vcc_project/k8s/yamls/resource-profiler-crd.yaml  
kubectl apply -f vcc_project/k8s/yamls/resource-profiler.yaml  
kubectl apply -f vcc_project/k8s/yamls/task-packer.yaml  
kubectl apply -f vcc_project/k8s/yamls/autoscaler.yaml  
kubectl apply -f vcc_project/k8s/yamls/instance-cleaner.yaml  
```  

Verify the deployment:  

```bash  
kubectl get pods  
kubectl get services  
```  

---

## Usage  

1. **Monitor Resource Usage**: Use the Resource Profiler to track node utilization.  
2. **Schedule Tasks**: Submit workloads to the cluster and let the Task Packer optimize scheduling.  
3. **Autoscaling**: Observe the Autoscaler dynamically adjust node pools based on workload demands.  
4. **Node Cleanup**: The Instance Cleaner will automatically manage underutilized nodes.  

---

## Testing  

- Test the deployment in a staging environment before production.  
- Use `kubectl logs` to debug any issues with the deployed components.  
- Verify the functionality of each module (e.g., Resource Profiler, Task Packer) independently.  

---

## Final Notes  

- Replace placeholder values like `YOUR_PROJECT_ID` with actual values.  
- Customize resource requests and limits in the YAML files based on your workload requirements.  
- Ensure proper permissions and configurations for CRIU-based container migration.  

By following this guide, you should have a fully functional implementation of the HTAS strategy for Kubernetes clusters.  

---  

**Authors**: Shivanshu Verma , Abhishek Yadav , Anuj Chincholikar 
**License**: MIT  