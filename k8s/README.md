# Kubernetes Scaffold

This folder contains a working starting point for deploying BESTSUPPORT on Kubernetes.

## What Is Included

- `postgres.yaml`: single-instance PostgreSQL with persistent storage
- `redis.yaml`: single-instance Redis with append-only persistence
- `minio.yaml`: single-instance MinIO for attachment storage
- `minio-init-job.yaml`: creates the MinIO bucket
- `migrate-job.yaml`: runs Django migrations once
- `app.yaml`: Django app plus an nginx sidecar for `/static/` and reverse proxying
- `ingress.yaml`: HTTP entrypoint with websocket-friendly timeouts
- `cronjobs.yaml`: scheduled cleanup and reminder jobs

## Before You Apply

1. Update [configmap.yaml](/e:/pythonproject/BESTSUPPORT/k8s/configmap.yaml) with your real hostnames, email settings, and retention values.
2. Replace the placeholder secrets in [secret.yaml](/e:/pythonproject/BESTSUPPORT/k8s/secret.yaml).
3. Set the app image in [kustomization.yaml](/e:/pythonproject/BESTSUPPORT/k8s/kustomization.yaml) to the registry/tag your cluster can pull.
4. Make sure your cluster has an ingress controller and a default storage class.
5. Create the TLS secret referenced by [ingress.yaml](/e:/pythonproject/BESTSUPPORT/k8s/ingress.yaml), or change that reference to match your cert manager flow.

## Remote Cluster Sequence

Use this when your cluster pulls images from a registry such as GHCR or Docker Hub.

```powershell
$IMAGE_REPOSITORY = "ghcr.io/your-org/bestsupport"
$IMAGE_TAG = "2026-04-14-01"

docker build -t "${IMAGE_REPOSITORY}:${IMAGE_TAG}" .
docker push "${IMAGE_REPOSITORY}:${IMAGE_TAG}"

.\k8s\set-image.ps1 -ImageRepository $IMAGE_REPOSITORY -ImageTag $IMAGE_TAG

kubectl -n bestsupport delete job bestsupport-migrate --ignore-not-found
kubectl -n bestsupport delete job bestsupport-minio-init --ignore-not-found

kubectl apply -k k8s
kubectl -n bestsupport wait --for=condition=complete job/bestsupport-minio-init --timeout=5m
kubectl -n bestsupport wait --for=condition=complete job/bestsupport-migrate --timeout=10m
kubectl -n bestsupport rollout status deployment/bestsupport-web --timeout=10m
kubectl -n bestsupport get pods
kubectl -n bestsupport get ingress
```

Use a new image tag for each deploy. That works better with `imagePullPolicy: IfNotPresent` and makes rollbacks easier.

## Local Cluster Sequence

Use this when your Kubernetes cluster can use locally built images directly, such as Docker Desktop Kubernetes. If you use `kind`, load the image into the cluster after the build.

```powershell
$IMAGE_REPOSITORY = "bestsupport-app"
$IMAGE_TAG = "dev"

docker build -t "${IMAGE_REPOSITORY}:${IMAGE_TAG}" .

# kind only:
# kind load docker-image "${IMAGE_REPOSITORY}:${IMAGE_TAG}" --name your-cluster-name

.\k8s\set-image.ps1 -ImageRepository $IMAGE_REPOSITORY -ImageTag $IMAGE_TAG

kubectl -n bestsupport delete job bestsupport-migrate --ignore-not-found
kubectl -n bestsupport delete job bestsupport-minio-init --ignore-not-found

kubectl apply -k k8s-docker-desktop
kubectl -n bestsupport wait --for=condition=complete job/bestsupport-minio-init --timeout=5m
kubectl -n bestsupport wait --for=condition=complete job/bestsupport-migrate --timeout=10m
kubectl -n bestsupport rollout status deployment/bestsupport-web --timeout=10m
kubectl -n bestsupport get pods
```

The Docker Desktop overlay in [k8s-docker-desktop/kustomization.yaml](/e:/pythonproject/BESTSUPPORT/k8s-docker-desktop/kustomization.yaml) removes the Ingress and switches the app settings to `http://localhost:8080`, including non-secure cookies for local browser access.

## Notes On The Sequence

- Deleting the two Jobs before `kubectl apply -k k8s` avoids immutable-job update problems on repeat deploys.
- The web deployment is expected to stay unready until the migration Job finishes.
- If the rollout stalls, check:
  - `kubectl -n bestsupport logs job/bestsupport-migrate`
  - `kubectl -n bestsupport describe pod -l app.kubernetes.io/component=web`
  - `kubectl -n bestsupport logs deployment/bestsupport-web -c app`
  - `kubectl -n bestsupport logs deployment/bestsupport-web -c nginx`

## Quick Apply

If you have already built and published the image, and [kustomization.yaml](/e:/pythonproject/BESTSUPPORT/k8s/kustomization.yaml) already points at the right tag, the shortest deploy path is:

```powershell
kubectl -n bestsupport delete job bestsupport-migrate --ignore-not-found
kubectl -n bestsupport delete job bestsupport-minio-init --ignore-not-found
kubectl apply -k k8s
kubectl -n bestsupport logs job/bestsupport-migrate
```

The web pod stays unready until the database is reachable and migrations are no longer pending. That keeps traffic away from the app while the bootstrap job is still running.

## Notes

- This scaffold assumes MinIO stays enabled in Kubernetes. The app has a filesystem fallback, but that is not a good multi-pod production pattern.
- `coturn` is intentionally left out of the base manifests. TURN usually needs broader UDP exposure than the web app and is often easier to run as a separate dedicated service.
- If you re-run the migration job before Kubernetes garbage-collects the old finished job, delete `bestsupport-migrate` first and apply again.
- If you already use managed PostgreSQL, Redis, or S3-compatible storage, remove the in-cluster manifests and point the app config at your managed endpoints instead.
