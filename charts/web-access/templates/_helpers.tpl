{{- /*
  Required explicit identity (never derived from .Release.Name / .Release.Namespace).
*/ -}}
{{- define "web-access.name" -}}
  {{- required "name is required" .Values.name -}}
{{- end -}}

{{- define "web-access.namespace" -}}
  {{- required "namespace is required" .Values.namespace -}}
{{- end -}}

{{- /*
  Derived proxy Service / Deployment name: defaults to "<name>-oidc-proxy".
*/ -}}
{{- define "web-access.proxyName" -}}
  {{- $appName := include "web-access.name" . -}}
  {{- default (printf "%s-oidc-proxy" $appName) .Values.proxy.name -}}
{{- end -}}

{{- /*
  Shared OIDC URLs: proxy automatically reuses oidc.issuerURL and oidc.redirectURLs[0]
  unless explicitly overridden under proxy.*.
*/ -}}
{{- define "web-access.issuerURL" -}}
  {{- default .Values.oidc.issuerURL .Values.proxy.issuerURL -}}
{{- end -}}

{{- define "web-access.redirectURL" -}}
  {{- $redirectURL := .Values.proxy.redirectURL -}}
  {{- if and (empty $redirectURL) (gt (len .Values.oidc.redirectURLs) 0) -}}
    {{- $redirectURL = index .Values.oidc.redirectURLs 0 -}}
  {{- end -}}
  {{- $redirectURL -}}
{{- end -}}

{{- /*
  Internal OpenBao mesh backchannel URL for token exchange & JWKS.
  - null (default): auto-derived when proxy.ambient is true
  - "": explicitly disabled (use standard public OIDC discovery)
  - "<url>": explicit override
*/ -}}
{{- define "web-access.backchannelURL" -}}
  {{- if kindIs "invalid" .Values.proxy.openbaoBackchannelURL -}}
    {{- if .Values.proxy.ambient -}}
      {{- $appName := include "web-access.name" . -}}
      {{- printf "http://openbao-active.openbao.svc.cluster.local:8200/v1/identity/oidc/provider/%s" $appName -}}
    {{- end -}}
  {{- else -}}
    {{- .Values.proxy.openbaoBackchannelURL -}}
  {{- end -}}
{{- end -}}

{{- /*
  Crossplane OpenBao OIDC metadata (omits annotations when syncWave is unset).
*/ -}}
{{- define "web-access.oidcMetadata" -}}
name: {{ .name | quote }}
{{- if hasKey . "syncWave" }}
annotations:
  argocd.argoproj.io/sync-wave: {{ .syncWave | quote }}
{{- end }}
{{- end -}}

{{- /*
  Common Crossplane spec fields (deletionPolicy + providerConfigRef).
*/ -}}
{{- define "web-access.oidcCommon" -}}
deletionPolicy: {{ .deletionPolicy }}
providerConfigRef:
  name: {{ .providerConfigName | quote }}
{{- end -}}

{{- /*
  oauth2-proxy inputs ConfigMap (also hashed by Deployment checksum/oidc-inputs).
*/ -}}
{{- define "web-access.proxyInputsConfigMap" -}}
  {{- $proxyName   := include "web-access.proxyName" . -}}
  {{- $namespace   := include "web-access.namespace" . -}}
  {{- $issuerURL   := required "proxy.issuerURL (or oidc.issuerURL) is required when proxy.enabled is true" (include "web-access.issuerURL" .) -}}
  {{- $redirectURL := required "proxy.redirectURL (or oidc.redirectURLs[0]) is required when proxy.enabled is true" (include "web-access.redirectURL" .) }}
apiVersion: v1
kind: ConfigMap
metadata:
  name: {{ $proxyName }}-inputs
  namespace: {{ $namespace }}
data:
  issuer-url: {{ $issuerURL | quote }}
  redirect-url: {{ $redirectURL | quote }}
{{- end -}}
