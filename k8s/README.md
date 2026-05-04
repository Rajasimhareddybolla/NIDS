# Kubernetes HTML Deployment

This directory contains the static benchmark HTML Kubernetes deployment.

## EKS Deployment

The managed AWS EKS cluster is `bd-html-eks` in `ap-south-1`.

```bash
aws eks update-kubeconfig --region ap-south-1 --name bd-html-eks
kubectl get nodes
kubectl apply -f k8s/html-site.yaml
kubectl -n bd-html patch service bd-html-site -p '{"spec":{"type":"LoadBalancer"}}'
```

Verify the deployed workload:

```bash
kubectl -n bd-html get deploy,pods,svc
```

Current EKS public URL:

```text
http://a4a8e8f167dc24fc6898ed3be2d192c6-1601038357.ap-south-1.elb.amazonaws.com
```

## EC2 k3s Deployment

There is also a self-managed k3s cluster on EC2. It appears in the EC2 console,
not in the EKS console.

```bash
KUBECONFIG=k8s/kubeconfig-bd-html-ec2.yaml kubectl get nodes
KUBECONFIG=k8s/kubeconfig-bd-html-ec2.yaml kubectl apply -f k8s/html-site.yaml
```

Current EC2 k3s public URL:

```text
http://13.235.58.110
```
