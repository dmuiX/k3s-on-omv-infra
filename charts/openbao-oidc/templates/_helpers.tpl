{{- define "openbao-oidc.metadata" -}}
name: {{ .name | quote }}
{{- if hasKey . "syncWave" }}
annotations:
  argocd.argoproj.io/sync-wave: {{ .syncWave | quote }}
{{- end }}
{{- end -}}

{{- define "openbao-oidc.common" -}}
deletionPolicy: {{ .deletionPolicy }}
providerConfigRef:
  name: {{ .providerConfigName | quote }}
{{- end -}}
