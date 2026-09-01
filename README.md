# OpenShift → GitHub Governance Audit

Pipeline de GitHub Actions que audita, para todos los namespaces de un
cluster OpenShift, qué repositorio de GitHub origina cada Deployment y
verifica su estado de gobierno (rama `main`, carpeta `.github`,
`CODEOWNERS`, rulesets).

## Qué hace

1. Se loguea al cluster con `oc` y lista todos los namespaces (excluyendo
   los internos del sistema: `openshift-*`, `kube-*`, `default`, etc.).
2. Lista todos los `Deployment` de cada namespace.
3. Para cada contenedor de cada deployment, resuelve el `ImageStream`/`tag`
   correspondiente (vía la anotación `image.openshift.io/triggers`, o por
   coincidencia de `dockerImageReference` como fallback).
4. Para ese ImageStream/tag, intenta ubicar el repositorio de GitHub de
   origen probando en orden: el `BuildConfig` asociado
   (`spec.source.git.uri`), anotaciones del ImageStream/tag, y labels S2I
   horneados en la imagen (`io.openshift.build.source-location`).
5. Por cada repositorio único encontrado, consulta la API de GitHub:
   URL, si existe rama `main`, si tiene carpeta `.github`, si tiene
   `CODEOWNERS` (raíz, `.github/` o `docs/`) y sus `rulesets`.
6. Escribe todo en `audit-result.json`, con:
   - `repositories`: detalle único por `owner/repo` (sin llamadas
     duplicadas a GitHub).
   - `deployments`: lista `namespace/deployment/container → repo_key`,
     la asociación explícita entre cada deployment y su repositorio.

El JSON se sube como artefacto del workflow run (`openshift-github-audit-<run_id>`).

## Requisitos

- **Runner self-hosted** con red hacia el API server de OpenShift y CLI
  `oc` instalado (el workflow lo descarga automáticamente si falta, en
  Linux).
- **Secrets** (Settings → Secrets and variables → Actions):
  - `OPENSHIFT_SERVER`: URL del API server, ej.
    `https://api.cluster.example.com:6443`.
  - `OPENSHIFT_TOKEN`: token de una ServiceAccount con permisos de
    lectura (`get`/`list`) sobre `namespaces`, `deployments`,
    `imagestreams`, `imagestreamtags` y `buildconfigs` en los namespaces
    a auditar.
  - `GH_PAT`: Personal Access Token de GitHub con permiso de lectura de
    contenido y **Administration: read** (necesario para poder listar
    `rulesets`) en los repositorios a inspeccionar.
- **Variables opcionales**:
  - `OPENSHIFT_SKIP_TLS_VERIFY = true` si el cluster usa certificados
    autofirmados.
  - `GITHUB_API_URL` para GitHub Enterprise Server
    (ej. `https://github.miempresa.com/api/v3`).

## Uso

Se dispara manualmente (`workflow_dispatch`) desde la pestaña Actions,
con dos inputs opcionales para ajustar qué namespaces excluir:

- `namespace_exclude_prefixes` (default `openshift-,kube-`)
- `namespace_exclude_exact` (default `default,openshift,kube-system,kube-public,kube-node-lease`)

## Ejecutar el script localmente (debug)

```bash
oc login --token=<token> --server=<api-server>
export GH_PAT=<tu-pat>
pip install -r scripts/requirements.txt
python scripts/openshift_github_audit.py --output audit-result.json
```

## Limitaciones conocidas

- Solo detecta el repo de origen si existe alguna de las tres señales
  soportadas (BuildConfig, anotación o label S2I). Si un ImageStream fue
  poblado por `oc import-image` sin ninguna de esas señales, el
  deployment queda registrado igual pero con `repo_key: null`.
- El campo `has_main_branch` verifica una rama llamada literalmente
  `main` (no el `default_branch` configurado del repo).
- Los rulesets requieren permisos de administración sobre el repo; si el
  `GH_PAT` no los tiene, `rulesets.accessible` queda en `false` con el
  motivo en `rulesets.error`.
