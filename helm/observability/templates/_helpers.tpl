{{/*
Chart name, truncated and DNS-1123-safe.
*/}}
{{- define "observability.name" -}}
{{- .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Fully qualified app name: <release>-<chart>, unless the release name already
contains the chart name. Same shape as nexus-rag.fullname so the two charts'
objects read alike in `kubectl get`.
*/}}
{{- define "observability.fullname" -}}
{{- if contains .Chart.Name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{/*
Common labels applied to every resource.
*/}}
{{- define "observability.labels" -}}
helm.sh/chart: {{ printf "%s-%s" (include "observability.name" .) .Chart.Version | replace "+" "_" }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{ include "observability.selectorLabels" . }}
{{- with .Values.global.labels }}
{{ toYaml . }}
{{- end }}
{{- end -}}

{{- define "observability.selectorLabels" -}}
app.kubernetes.io/name: {{ include "observability.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/*
Selector labels for one component. Pass dict "root" . "component" "prometheus".
*/}}
{{- define "observability.componentSelectorLabels" -}}
app.kubernetes.io/name: {{ include "observability.name" .root }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{/*
Resolve an image reference the same way nexus-rag.image does: always prefixes
global.imageRegistry when set, on the assumption that mirroring into the
air-gapped registry preserves each image's original path (NFR-1/NFR-16).
Pass dict "global" .Values.global "image" <component>.image.
*/}}
{{- define "observability.image" -}}
{{- if .global.imageRegistry -}}
{{- printf "%s/%s:%s" .global.imageRegistry .image.repository .image.tag -}}
{{- else -}}
{{- printf "%s:%s" .image.repository .image.tag -}}
{{- end -}}
{{- end -}}

{{/*
Container securityContext. Same shape as nexus-rag.containerSecurityContext.
None of these images need to write to their root filesystem: each one's
writable paths are explicit volumes (a PVC for the stores, an emptyDir for
/tmp), so readOnlyRootFilesystem holds.
*/}}
{{- define "observability.containerSecurityContext" -}}
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
capabilities:
  drop: ["ALL"]
{{- end -}}

{{/*
Pod securityContext for the upstream images this chart runs. Unlike
nexus-rag.podSecurityContext (uid 10001, baked into this project's own
Dockerfiles) these uids come from each upstream image's own convention and
must match it or the container cannot write to its PVC:
  prom/prometheus, prom/alertmanager, prom/blackbox-exporter  -> nobody (65534)
  grafana/loki, grafana/tempo, grafana/alloy                  -> 10001
  otel/opentelemetry-collector-contrib                        -> 10001
  postgres-exporter, prometheus-nats-exporter                 -> 65534
Pass the uid as the argument.
*/}}
{{- define "observability.podSecurityContext" -}}
runAsNonRoot: true
runAsUser: {{ . }}
runAsGroup: {{ . }}
fsGroup: {{ . }}
{{- end -}}

{{/*
Namespace the nexus-rag release lives in. Defaults to this chart's namespace,
which is the common single-namespace install.
*/}}
{{- define "observability.nexusRagNamespace" -}}
{{- .Values.nexusRag.namespace | default .Release.Namespace -}}
{{- end -}}

{{/*
A nexus-rag Service's in-cluster DNS name. The nexus-rag chart names Services
<release>-nexus-rag-<component>, and nexus-rag.fullname collapses the prefix
when the release name already contains the chart name -- both cases are handled
here so a release literally named "nexus-rag" resolves correctly.
Pass dict "root" . "component" "qdrant".
*/}}
{{- define "observability.nexusRagService" -}}
{{- $rel := .root.Values.nexusRag.releaseName -}}
{{- $base := "" -}}
{{- if contains "nexus-rag" $rel -}}
{{- $base = $rel | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $base = printf "%s-nexus-rag" $rel | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- printf "%s-%s.%s.svc" $base .component (include "observability.nexusRagNamespace" .root) -}}
{{- end -}}

{{/*
Shared spec body for the LoadBalancer Services the external Grafana connects
to. Refuses to render an unrestricted LoadBalancer: these backends carry no
authentication of their own, so an empty source-range list publishes read
access to the corpus's operational telemetry. Failing the template is the
point -- an accidentally-open endpoint is not something to warn about in NOTES
after the fact.
Pass dict "root" . "component" "prometheus" "svc" <component>.service.
*/}}
{{- define "observability.externalServiceSpec" -}}
{{- $ea := .root.Values.externalAccess -}}
type: {{ .svc.type }}
{{- if eq .svc.type "LoadBalancer" }}
{{- if and (not $ea.loadBalancerSourceRanges) (not $ea.allowUnrestricted) }}
{{- fail (printf "%s is a LoadBalancer with no externalAccess.loadBalancerSourceRanges. Prometheus/Loki/Tempo/Alertmanager are unauthenticated (see issue #257), so this would publish the corpus's operational telemetry to anything that can route to the address. Set the CIDRs the external Grafana connects from, or set externalAccess.allowUnrestricted=true if access is restricted elsewhere." .component) }}
{{- end }}
{{- with .svc.loadBalancerIP }}
loadBalancerIP: {{ . | quote }}
{{- end }}
{{- with $ea.loadBalancerClass }}
loadBalancerClass: {{ . | quote }}
{{- end }}
{{- with $ea.loadBalancerSourceRanges }}
loadBalancerSourceRanges:
  {{- toYaml . | nindent 2 }}
{{- end }}
externalTrafficPolicy: {{ $ea.externalTrafficPolicy }}
{{- end }}
{{- end -}}

{{/*
Merged annotations for an external Service: chart-wide externalAccess.annotations
first, the component's own second so it wins on conflict.
Pass dict "root" . "svc" <component>.service.
*/}}
{{- define "observability.externalServiceAnnotations" -}}
{{- $merged := merge (deepCopy (.svc.annotations | default dict)) (.root.Values.externalAccess.annotations | default dict) -}}
{{- with $merged }}
annotations:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- end -}}

{{/*
imagePullSecrets block, or nothing.
*/}}
{{- define "observability.imagePullSecrets" -}}
{{- with .Values.global.imagePullSecrets }}
imagePullSecrets:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- end -}}
