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
Pod-level securityContext for the three custom-built services (ingestion-api,
orchestration-mcp, reranker-service). UID/GID 10001 is the fixed non-root
user baked into services/*/Dockerfile -- not runtime-injected, so it has to
match exactly. Not used by qdrant or embeddingService, which run upstream
images (qdrant/qdrant, ollama/ollama) whose own user conventions this chart
doesn't override.
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
