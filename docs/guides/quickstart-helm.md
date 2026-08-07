# Quickstart on Kubernetes (dev sandbox)

The same fifteen-minute goal as the [Compose quickstart](quickstart.md) —
seeded users, curated sample corpus, first claims-filtered query — but on a
**local Kubernetes cluster** with the real Helm chart. You stand up throwaway
dev Postgres and Keycloak the way Compose does, hand the chart its Secrets,
and seed with the same script the Compose stack uses.

!!! note "This is the sandbox, not production — and not your distribution"
    Dev-grade shortcuts on purpose: literal dev credentials, `start-dev`
    Keycloak, a throwaway MinIO/SeaweedFS instead of enterprise S3, no
    ingress/TLS, port-forwards. Two boundaries to respect:

    1. **Sandbox ≠ production.** The [production Helm guide](deploy-helm.md)
       is the real contract (existing enterprise Keycloak/Postgres/S3,
       generated secrets, ingress), and [air-gapped](deploy-airgapped.md) is
       its disconnected promotion.
    2. **kind ≠ your real Kubernetes.** A local kind/minikube behaves
       differently from an actual distribution — RKE/RKE2, VMware
       Tanzu/TKG, OpenShift, or managed EKS/AKS/GKE — in exactly the places
       this walkthrough takes shortcuts: default StorageClasses and PVC
       binding, Pod Security admission (OpenShift in particular will reject
       the naive dev Deployments below without SCC adjustments), ingress
       controllers, image-pull policy against private registries, and how
       S3 is really provided (an operator-managed MinIO, Ceph RGW, or
       enterprise S3 — not a single dev pod). Update images, storage, and
       security contexts accordingly for your platform.

    *Honest label:* this walkthrough is assembled from the chart's documented
    values contract; the chart is CI-rendered and the pieces are the same
    ones the live-validated Compose stack runs, but this exact kind-cluster
    sequence hasn't itself been exercised end-to-end yet — file anything that
    fights you.

## 1. Prerequisites and a local cluster

**What you'll need** (this stack is heavier than a hello-world — the model
pulls alone are ~10 GB):

- 4 CPUs or more, 8 GB free memory, 30 GB free disk
- Internet connection for the first boot (model pulls)
- A container/VM manager (Docker is the usual choice), `kubectl`, `helm`,
  and this repo cloned

Then bring up a local cluster with whichever tool you prefer:

=== "minikube"

    ```bash title="install (x86-64 Linux; other platforms: minikube.sigs.k8s.io/docs/start)"
    curl -LO https://github.com/kubernetes/minikube/releases/latest/download/minikube-linux-amd64
    sudo install minikube-linux-amd64 /usr/local/bin/minikube && rm minikube-linux-amd64
    ```

    ```bash title="start, sized for this stack"
    minikube start --cpus=4 --memory=8192 --disk-size=30g
    kubectl get po -A        # storage-provisioner may take a moment — normal
    ```

    ??? tip "GPU host? Pass the GPU into the sandbox"
        minikube (docker driver, Linux) can pass GPUs through — which lets
        the chart's embedding Ollama, and a CUDA-built reranker image, use
        them. The **NVIDIA** sequence, in full (each step has bitten
        someone):

        ```bash
        nvidia-smi                                  # 1. driver present?
        sudo sysctl net.core.bpf_jit_harden         # 2. must be 0
        # if not: echo "net.core.bpf_jit_harden=0" | sudo tee -a /etc/sysctl.conf && sudo sysctl -p
        sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker
        minikube delete                             # a cluster built BEFORE the
                                                    # nvidia runtime existed won't see it
        minikube start --driver docker --container-runtime docker --gpus all \
          --cpus=4 --memory=8192 --disk-size=30g
        ```

        (`--gpus nvidia.com` is the CDI variant if your host uses CDI
        instead of the container toolkit.) **AMD** works too —
        `minikube start --driver docker --gpus amd` after the ROCm driver
        (`rocminfo` to verify) — but note this repo's GPU path
        ([Configuration → GPU hosts](configuration.md#gpu-hosts)) is
        documented and exercised for NVIDIA/CUDA; an AMD run means a ROCm
        torch index for the reranker and is uncharted here.

    Handy extras: `minikube dashboard` gives you a UI over everything you
    deploy below; `minikube addons list` / `minikube addons enable <name>`
    for extras like `metrics-server`; `minikube stop` / `minikube delete`
    when you're done. In step 6 you can also use minikube's native service
    access — `minikube service nexus-rag-orchestration-mcp --url` — in place
    of `kubectl port-forward` (this guide uses port-forward throughout so
    the commands are identical on kind).

=== "kind"

    ```bash title="install (via Go; or grab a release binary from kind.sigs.k8s.io)"
    go install sigs.k8s.io/kind@latest
    ```

    ```bash title="create the cluster"
    kind create cluster --name nexus-rag
    ```

    kind runs nodes as Docker containers, so its resource ceiling is your
    Docker daemon's — make sure Docker itself has ≥4 CPUs / 8 GB available.

## 2. Throwaway Postgres and Keycloak (what Compose gives you for free)

```bash title="dev Postgres (matches the Compose pin)"
kubectl create deployment postgres --image=postgres:16.14 --port=5432
kubectl set env deployment/postgres POSTGRES_USER=nexus_rag POSTGRES_PASSWORD=nexus_rag POSTGRES_DB=nexus_rag
kubectl expose deployment postgres --port=5432
```

```bash title="dev Keycloak with the repo's seeded realm imported"
kubectl create configmap keycloak-realm \
  --from-file=infra/keycloak/realm-export/
kubectl apply -f - <<'EOF'
apiVersion: apps/v1
kind: Deployment
metadata: {name: keycloak}
spec:
  replicas: 1
  selector: {matchLabels: {app: keycloak}}
  template:
    metadata: {labels: {app: keycloak}}
    spec:
      containers:
        - name: keycloak
          image: quay.io/keycloak/keycloak:26.7.0
          args: ["start-dev", "--import-realm"]
          env:
            - {name: KEYCLOAK_ADMIN, value: admin}
            - {name: KEYCLOAK_ADMIN_PASSWORD, value: admin}
          ports: [{containerPort: 8080}]
          volumeMounts:
            - {name: realm, mountPath: /opt/keycloak/data/import}
      volumes:
        - name: realm
          configMap: {name: keycloak-realm}
---
apiVersion: v1
kind: Service
metadata: {name: keycloak}
spec:
  selector: {app: keycloak}
  ports: [{port: 8080, targetPort: 8080}]
EOF
```

The realm import gives you the same `alice-ingest` / `carol-curator` /
`bob-query` / `dave-admin` personas (password `devpass123`) as Compose.

```bash title="dev MinIO (throwaway S3 for the object store — original uploaded bytes land here)"
kubectl apply -f - <<'EOF'
apiVersion: apps/v1
kind: Deployment
metadata: {name: minio}
spec:
  replicas: 1
  selector: {matchLabels: {app: minio}}
  template:
    metadata: {labels: {app: minio}}
    spec:
      containers:
        - name: minio
          image: quay.io/minio/minio:latest   # dev-only; pin a RELEASE.* tag on any shared cluster
          args: ["server", "/data"]
          env:
            - {name: MINIO_ROOT_USER, value: nexus-rag-dev}
            - {name: MINIO_ROOT_PASSWORD, value: nexus-rag-dev-secret}
          ports: [{containerPort: 9000}]
---
apiVersion: v1
kind: Service
metadata: {name: minio}
spec:
  selector: {app: minio}
  ports: [{port: 9000, targetPort: 9000}]
EOF

# the app never creates its bucket itself (a real S3 wouldn't allow it either).
# --command matters: the mc image's entrypoint IS `mc`, so without it the
# shell invocation is handed to mc as arguments and the pod errors out
kubectl run mc --rm -it --restart=Never --image=quay.io/minio/mc:latest --command -- \
  /bin/sh -c "mc alias set dev http://minio:9000 nexus-rag-dev nexus-rag-dev-secret && mc mb dev/nexus-rag-documents"
```

!!! note "MinIO here vs the alternatives"
    This single-pod MinIO is the *classic dev S3* and mirrors what most
    teams reach for locally — but it is **not** how object storage looks on
    a real RKE/Tanzu/OpenShift/managed cluster (operator-managed MinIO,
    Ceph RGW, or enterprise S3, with TLS and real credentials — see the
    boundary note at the top). If you'd rather deploy *nothing extra*, the
    chart can bundle its own SeaweedFS S3 gateway instead — both options
    are wired in step 4.

## 3. The chart's Secrets, dev-grade

The chart fails closed without these. Default Secret names from
`values.yaml`, dev values matching the seeded realm:

```bash
kubectl create secret generic nexus-rag-db \
  --from-literal=database-url='postgresql+psycopg://nexus_rag:nexus_rag@postgres:5432/nexus_rag'

kubectl create secret generic nexus-rag-keycloak-client-secret \
  --from-literal=client-secret='dev-rag-app-secret'

kubectl create secret generic nexus-rag-session-token-key \
  --from-literal=key="$(python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"

kubectl create secret generic nexus-rag-qdrant-keys \
  --from-literal=api-key="$(openssl rand -hex 32)" \
  --from-literal=read-only-api-key="$(openssl rand -hex 32)"

kubectl create secret generic nexus-rag-nats-ingestion-api \
  --from-literal=password="$(openssl rand -hex 24)"
kubectl create secret generic nexus-rag-nats-ingestion-worker \
  --from-literal=password="$(openssl rand -hex 24)"

# object-store credentials: MinIO's dev root user from step 2
kubectl create secret generic nexus-rag-object-store \
  --from-literal=access-key='nexus-rag-dev' \
  --from-literal=secret-key='nexus-rag-dev-secret'
```

## 4. Install the chart

```yaml title="dev-values.yaml"
global:
  # keep EMPTY in the sandbox. The chart's default is an air-gapped
  # internal-registry EXAMPLE, and this prefix is applied to EVERY image --
  # third-party ones (qdrant/qdrant, nats, ollama/ollama) included, because
  # an internal mirror is expected to hold all of them under their original
  # paths. There is no such mirror on GHCR, so a sandbox blanks the prefix
  # (third-party images then pull from their canonical registries) and
  # points only the project's own images at GHCR, per component below.
  imageRegistry: ""

ingestionWorker:
  image: {repository: ghcr.io/schuecl/nexus-rag/ingestion-worker}
  replicas: 1               # sandbox sizing -- production default is 2
orchestrationMcp:
  image: {repository: ghcr.io/schuecl/nexus-rag/orchestration-mcp}
  replicas: 1
rerankerService:
  image: {repository: ghcr.io/schuecl/nexus-rag/reranker-service}
auditReporting:
  image: {repository: ghcr.io/schuecl/nexus-rag/scripts}

externalKeycloak:
  # comma-separated: in-cluster requests mint tokens as `keycloak:8080`,
  # your port-forwarded curls as `localhost:8080` -- accept both (the same
  # two-hostnames-one-Keycloak accommodation the Compose stack uses)
  issuerUrl: "http://keycloak:8080/realms/nexus-rag,http://localhost:8080/realms/nexus-rag"

ingestionApi:
  image: {repository: ghcr.io/schuecl/nexus-rag/ingestion-api}
  # no ingress in the sandbox; the browser reaches the UI via port-forward
  oidcRedirectUri: "http://localhost:8001/auth/callback"
  # production default is 2 replicas per service -- halve the CPU footprint
  # so the whole stack schedules on a 4-CPU sandbox node
  replicas: 1

embeddingService:
  # the chart's default embedding resources REQUEST A GPU
  # (nvidia.com/gpu: 1) -- on a CPU-only cluster that pod stays Pending
  # forever with `Insufficient nvidia.com/gpu`. Null out both entries for
  # CPU-only; if you passed a GPU through (see the tip above), keep the
  # defaults instead
  resources:
    requests: {cpu: "1", memory: "4Gi", "nvidia.com/gpu": null}
    limits: {cpu: "2", memory: "8Gi", "nvidia.com/gpu": null}

objectStore:
  enabled: false            # external S3 = the dev MinIO from step 2
  external:
    endpoint: "http://minio:9000"
    bucket: "nexus-rag-documents"
    region: "us-east-1"     # MinIO ignores it; boto3 requires some value
```

??? example "No-MinIO alternative: let the chart bundle SeaweedFS"
    Skip the MinIO deployment and its Secret entirely; instead create

    ```bash
    kubectl create secret generic nexus-rag-seaweedfs-keys \
      --from-literal=access-key="$(openssl rand -hex 16)" \
      --from-literal=secret-key="$(openssl rand -hex 32)"
    ```

    and set

    ```yaml
    objectStore:
      enabled: true   # chart deploys SeaweedFS and pre-creates the bucket
    ```

```bash
helm install nexus-rag helm/nexus-rag -f dev-values.yaml
kubectl get pods -w    # wait for Running/Ready; first boot pulls models
```

??? tip "Where the images come from (and how to use your own)"
    - **Default (this guide):** the four service images pull from GHCR
      (public — no credentials needed) via the per-component
      `image.repository` overrides in `dev-values.yaml`; third-party
      components (Qdrant, NATS, Ollama, SeaweedFS) keep their canonical
      upstream names because `global.imageRegistry` is blank. Don't be
      tempted to set `global.imageRegistry: ghcr.io/schuecl/nexus-rag`
      instead — the chart prefixes *every* image with that registry
      (its contract is a full internal mirror, path-preserving), and GHCR
      hosts only this project's own images, so Qdrant/NATS/Ollama would
      `ImagePullBackOff` on nonexistent paths.
    - **Locally-built images** (you're iterating on the services): build
      with the repo's Dockerfiles, then load them straight into the
      cluster — no registry needed:

        ```bash
        minikube image load ghcr.io/schuecl/nexus-rag/ingestion-api:dev
        # kind equivalent:
        kind load docker-image ghcr.io/schuecl/nexus-rag/ingestion-api:dev --name nexus-rag
        ```

        then set the matching tag in your values. On minikube you can also
        build *inside* the cluster's Docker daemon directly:
        `eval $(minikube docker-env) && docker build …`.
    - **A private mirror** (corporate proxy of GHCR): set
      `global.imageRegistry` to it and provide `global.imagePullSecrets`
      (a standard `kubernetes.io/dockerconfigjson` Secret). minikube also
      offers the `registry-creds` addon
      (`minikube addons configure registry-creds && minikube addons enable
      registry-creds`) for ECR/GCR/ACR/Docker-registry credentials.
    - **Insecure/in-cluster registries:** start the cluster with
      `minikube start --insecure-registry "10.0.0.0/24"` (must be set at
      cluster creation — `minikube delete` first if it already exists).

## 5. Seed the sandbox corpus

Same script, same seven documents, same real-API ingest-and-curate flow as
Compose — run it as a one-off pod from the released `scripts` image:

```bash
kubectl run seed --rm -it --restart=Never \
  --image=ghcr.io/schuecl/nexus-rag/scripts:0.6.0 \
  --env INGESTION_API_URL=http://nexus-rag-ingestion-api:8001 \
  --env KEYCLOAK_URL=http://keycloak:8080 \
  --env RAG_APP_KEYCLOAK_CLIENT_SECRET=dev-rag-app-secret \
  -- python3 seed_sample_data.py
```

The `scripts` image is published by the same release workflow as the four
service images — use the tag matching your chart's `appVersion`
(`helm show chart helm/nexus-rag | grep appVersion`).

## 6. First query

```bash
kubectl port-forward svc/nexus-rag-orchestration-mcp 8002:8002 &
kubectl port-forward svc/keycloak 8080:8080 &

TOKEN=$(curl -s http://localhost:8080/realms/nexus-rag/protocol/openid-connect/token \
  -d grant_type=password -d client_id=rag-app -d client_secret=dev-rag-app-secret \
  -d username=bob-query -d password=devpass123 | jq -r .access_token)

curl -s -X POST http://localhost:8002/debug/rag_search \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"query": "how often should passwords be rotated", "top_k": 5}' | jq
```

From here, everything in the [Compose quickstart](quickstart.md) from step 3
onward applies unchanged — same personas, same access-filter proof
(`dave-admin` sees the Signal-Corps SECRET document, `bob-query` provably
doesn't), same hand-run of upload → curate → query (port-forward
`svc/nexus-rag-ingestion-api 8001:8001` for the UI).

??? tip "minikube-native access instead of port-forward"
    The chart's services default to `ClusterIP`, which is why this guide
    uses `kubectl port-forward` (works identically on kind). On minikube you
    can use its native access instead — flip the two user-facing services to
    `NodePort` in `dev-values.yaml`:

    ```yaml
    ingestionApi:
      service: {type: NodePort, port: 8001}
    orchestrationMcp:
      service: {type: NodePort, port: 8002}
    ```

    then, each in its own terminal (the command keeps a tunnel open on the
    Docker driver — Ctrl-C tears it down):

    ```bash
    minikube service nexus-rag-ingestion-api --url
    minikube service nexus-rag-orchestration-mcp --url
    ```

    and use the printed URLs in place of `localhost:8001/8002` (remember the
    OIDC redirect in `dev-values.yaml` must then match the ingestion-api
    URL). `minikube tunnel` + `type: LoadBalancer` works too, but needs root
    for the route and buys nothing extra in a sandbox. Keycloak from step 2
    can get the same treatment (`minikube service keycloak --url`) — and any
    non-`localhost` Keycloak URL must be added to the `issuerUrl` list.

## Where this differs from production

| | This sandbox | [Production Helm](deploy-helm.md) → [air-gapped](deploy-airgapped.md) |
|---|---|---|
| Keycloak / Postgres / S3 | throwaway, in-cluster, dev creds | your existing enterprise services, real secrets |
| Identities & corpus | seeded personas + 7 sample docs | real users, real documents, empty until curated |
| Exposure | `kubectl port-forward` | ingress + TLS, NetworkPolicies doing real work |
| Images | pulled from GHCR | promoted via the verified bundle into an internal registry |

## Sources

- [Helm chart README](https://github.com/schuecl/nexus-rag/blob/main/helm/nexus-rag/README.md)
  — the authoritative Secrets/values contract this page instantiates with
  dev values
- [`infra/keycloak/realm-export/`](https://github.com/schuecl/nexus-rag/blob/main/infra/keycloak/realm-export/)
  — the seeded realm imported in step 2
- [`scripts/seed_sample_data.py`](https://github.com/schuecl/nexus-rag/blob/main/scripts/seed_sample_data.py)
  — the same seeding the Compose stack runs
