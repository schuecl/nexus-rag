{{/*
Chart name, truncated and DNS-1123-safe.
*/}}
{{- define "nexus-rag.name" -}}
{{- .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Fully qualified app name: <release>-<chart>, unless the release name already
contains the chart name.
*/}}
{{- define "nexus-rag.fullname" -}}
{{- if contains .Chart.Name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{/*
Common labels applied to every resource.
*/}}
{{- define "nexus-rag.labels" -}}
helm.sh/chart: {{ printf "%s-%s" (include "nexus-rag.name" .) .Chart.Version | replace "+" "_" }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{ include "nexus-rag.selectorLabels" . }}
{{- with .Values.global.labels }}
{{ toYaml . }}
{{- end }}
{{- end -}}

{{/*
Selector labels shared by a component's Deployment/StatefulSet and Service.
Pass a dict with "root" (the top-level context) and "component" (e.g. "ingestion-api").
*/}}
{{- define "nexus-rag.componentSelectorLabels" -}}
app.kubernetes.io/name: {{ include "nexus-rag.name" .root }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{/*
Base selector labels (no component) -- used by the shared labels block above.
*/}}
{{- define "nexus-rag.selectorLabels" -}}
app.kubernetes.io/name: {{ include "nexus-rag.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/*
Resolve an image reference: <global.imageRegistry>/<component.image.repository>:<tag>,
or just <repository>:<tag> if global.imageRegistry is empty. Always prefixes
(doesn't try to detect an already-fully-qualified repository) -- mirroring
into global.imageRegistry is expected to preserve each image's original
path (e.g. "qdrant/qdrant" stays "qdrant/qdrant" under the mirror prefix).
Pass a dict with "global" (the top-level .Values.global) and "image" (a component's .image block).
*/}}
{{- define "nexus-rag.image" -}}
{{- $repo := .image.repository -}}
{{- if .global.imageRegistry -}}
{{- printf "%s/%s:%s" .global.imageRegistry $repo .image.tag -}}
{{- else -}}
{{- printf "%s:%s" $repo .image.tag -}}
{{- end -}}
{{- end -}}

{{/*
Pod-level securityContext for the four custom-built services (ingestion-api,
ingestion-worker, orchestration-mcp, reranker-service). UID/GID 10001 is the
fixed non-root user baked into each service's own Dockerfile -- not
runtime-injected, so it has to match exactly. Not used by qdrant,
embeddingService, or nats, which run upstream images (qdrant/qdrant,
ollama/ollama, nats) whose own user conventions this chart doesn't override.
*/}}
{{- define "nexus-rag.podSecurityContext" -}}
runAsNonRoot: true
runAsUser: 10001
runAsGroup: 10001
fsGroup: 10001
{{- end -}}

{{/*
Container-level securityContext paired with the pod-level one above.
readOnlyRootFilesystem is safe here because the only runtime writes these
images make are to HF_HOME (a mounted PVC/volume, not the root filesystem)
and /tmp (an emptyDir volume the caller must mount alongside this -- see
each Deployment template).
*/}}
{{- define "nexus-rag.containerSecurityContext" -}}
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
capabilities:
  drop: ["ALL"]
{{- end -}}

{{/*
Issue #401: Qdrant endpoint URL. qdrant.enabled (self-deploy) wins if true;
otherwise qdrant.external.host must be set -- fails the render rather than
silently emitting a broken URL, same reasoning as oidcRedirectUri below.
Only meaningful when vectorBackend is "qdrant"; callers must gate on that
themselves (this helper doesn't know the caller's context).
*/}}
{{- define "nexus-rag.qdrantUrl" -}}
{{- if .Values.qdrant.enabled -}}
{{- printf "http://%s-qdrant:%v" (include "nexus-rag.fullname" .) .Values.qdrant.service.httpPort -}}
{{- else if .Values.qdrant.external.host -}}
{{- printf "%s://%s:%v" (ternary "https" "http" .Values.qdrant.external.tls) .Values.qdrant.external.host .Values.qdrant.external.httpPort -}}
{{- else -}}
{{- fail "Set qdrant.external.host when qdrant.enabled is false (external Qdrant mode)." -}}
{{- end -}}
{{- end -}}

{{/*
Issue #401: Milvus endpoint URL, same enabled/external pattern as Qdrant
above. Only meaningful when vectorBackend is "milvus".
*/}}
{{- define "nexus-rag.milvusUrl" -}}
{{- if .Values.milvus.enabled -}}
{{- printf "http://%s-milvus:%v" (include "nexus-rag.fullname" .) .Values.milvus.service.port -}}
{{- else if .Values.milvus.external.host -}}
{{- printf "%s://%s:%v" (ternary "https" "http" .Values.milvus.external.tls) .Values.milvus.external.host .Values.milvus.external.port -}}
{{- else -}}
{{- fail "Set milvus.external.host when milvus.enabled is false (external Milvus mode)." -}}
{{- end -}}
{{- end -}}

{{/*
Issue #401: NATS client URL, same enabled/external pattern as Qdrant above.
*/}}
{{- define "nexus-rag.natsUrl" -}}
{{- if .Values.nats.enabled -}}
{{- printf "nats://%s-nats:%v" (include "nexus-rag.fullname" .) .Values.nats.service.clientPort -}}
{{- else if .Values.nats.external.host -}}
{{- printf "%s://%s:%v" (ternary "tls" "nats" .Values.nats.external.tls) .Values.nats.external.host .Values.nats.external.port -}}
{{- else -}}
{{- fail "Set nats.external.host when nats.enabled is false (external NATS mode)." -}}
{{- end -}}
{{- end -}}

{{/*
Issue #401: embedding-service endpoint URL, same enabled/external pattern as
Qdrant above. The self-deployed instance always speaks Ollama's native API;
an external instance may speak either that or an OpenAI-API-compliant one,
selected by embeddingService.external.apiCompatibility (issue #403).
*/}}
{{- define "nexus-rag.embeddingUrl" -}}
{{- if .Values.embeddingService.enabled -}}
{{- printf "http://%s-embedding-service:%v" (include "nexus-rag.fullname" .) .Values.embeddingService.service.port -}}
{{- else if .Values.embeddingService.external.host -}}
{{- printf "%s://%s:%v" (ternary "https" "http" .Values.embeddingService.external.tls) .Values.embeddingService.external.host .Values.embeddingService.external.port -}}
{{- else -}}
{{- fail "Set embeddingService.external.host when embeddingService.enabled is false (external embedding mode)." -}}
{{- end -}}
{{- end -}}

{{/*
OIDC browser-login callback URL for ingestion-api (ARCHITECTURE.md Section
4.4), passed through as OIDC_REDIRECT_URI. ingestionApi.oidcRedirectUri wins
if set explicitly; otherwise derived from ingestionApi.ingress.host, using
https if ingress.tls is set (else http). Fails the render (rather than
silently emitting an empty/broken callback URL that would only surface as a
confusing runtime login failure) if neither an explicit override nor an
enabled ingress with a host is available to derive one from.
*/}}
{{- define "nexus-rag.oidcRedirectUri" -}}
{{- if .Values.ingestionApi.oidcRedirectUri -}}
{{- .Values.ingestionApi.oidcRedirectUri -}}
{{- else if and .Values.ingestionApi.ingress.enabled .Values.ingestionApi.ingress.host -}}
{{- $scheme := "http" -}}
{{- if .Values.ingestionApi.ingress.tls -}}
{{- $scheme = "https" -}}
{{- end -}}
{{- printf "%s://%s/auth/callback" $scheme .Values.ingestionApi.ingress.host -}}
{{- else -}}
{{- fail "Set ingestionApi.oidcRedirectUri explicitly, or enable ingestionApi.ingress with a host, so the OIDC login callback URL (ARCHITECTURE.md Section 4.4) can be derived." -}}
{{- end -}}
{{- end -}}
