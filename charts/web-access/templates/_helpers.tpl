{{- define "web-access.name" -}}
{{- required "name is required" .Values.name -}}
{{- end -}}

{{- define "web-access.namespace" -}}
{{- required "namespace is required" .Values.namespace -}}
{{- end -}}

{{- define "web-access.proxyName" -}}
{{- default (printf "%s-oidc-proxy" (include "web-access.name" .)) .Values.proxy.name -}}
{{- end -}}

{{- define "web-access.issuerURL" -}}
{{- default .Values.oidc.issuerURL .Values.proxy.issuerURL -}}
{{- end -}}

{{- define "web-access.redirectURL" -}}
{{- $redirect := .Values.proxy.redirectURL -}}
{{- if and (empty $redirect) (gt (len .Values.oidc.redirectURLs) 0) -}}
{{- $redirect = index .Values.oidc.redirectURLs 0 -}}
{{- end -}}
{{- $redirect -}}
{{- end -}}

{{- define "web-access.backchannelURL" -}}
{{- if kindIs "invalid" .Values.proxy.openbaoBackchannelURL -}}
{{- if .Values.proxy.ambient -}}
{{- printf "http://openbao-active.openbao.svc.cluster.local:8200/v1/identity/oidc/provider/%s" (include "web-access.name" .) -}}
{{- end -}}
{{- else -}}
{{- .Values.proxy.openbaoBackchannelURL -}}
{{- end -}}
{{- end -}}

{{- define "web-access.oidcMetadata" -}}
name: {{ .name | quote }}
{{- if hasKey . "syncWave" }}
annotations:
  argocd.argoproj.io/sync-wave: {{ .syncWave | quote }}
{{- end }}
{{- end -}}

{{- define "web-access.oidcCommon" -}}
deletionPolicy: {{ .deletionPolicy }}
providerConfigRef:
  name: {{ .providerConfigName | quote }}
{{- end -}}

{{- define "web-access.proxyInputsConfigMap" -}}
apiVersion: v1
kind: ConfigMap
metadata:
  name: {{ include "web-access.proxyName" . }}-inputs
  namespace: {{ include "web-access.namespace" . }}
data:
  issuer-url: {{ required "proxy.issuerURL (or oidc.issuerURL) is required when proxy.enabled is true" (include "web-access.issuerURL" .) | quote }}
  redirect-url: {{ required "proxy.redirectURL (or oidc.redirectURLs[0]) is required when proxy.enabled is true" (include "web-access.redirectURL" .) | quote }}
{{- end -}}
