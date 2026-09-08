# OpenShift → GitHub Governance Audit

Pipeline de GitHub Actions que audita, para todos los namespaces de un
cluster OpenShift, qué repositorio de GitHub origina cada Deployment y
verifica su estado de gobierno (rama `main`, carpeta `.github`,
`CODEOWNERS`, rulesets).

## Qué hace

1. Se loguea al cluster con `oc` y lista todos los namespaces (excluyendo
   los internos del sistema: `openshift-*`, `kube-*`, `default`, etc.).
2. Lista todos los `Deployment` de cada namespace.
3. Para cada contenedor de cada deployment, intenta resolver el
   `ImageStream`/`tag` correspondiente (vía la anotación
   `image.openshift.io/triggers`, o por coincidencia de
   `dockerImageReference` como fallback). No siempre existe: hay apps sin
   ImageStream (su BuildConfig empuja directo al registry, o se
   construyen fuera de OpenShift).
4. Intenta ubicar el repositorio de GitHub de origen probando, en orden:
   0. **Mapeo manual** (`scripts/repo_mapping.json`, ver más abajo): si hay
      una entrada `namespace/deployment -> owner/repo`, se usa
      directamente y no se corre ninguna heurística automática. Es la
      fuente de mayor prioridad.
   1. El `BuildConfig` cuyo output apunta a ese ImageStreamTag
      (`spec.source.git.uri`).
   2. Anotaciones del ImageStream/tag.
   3. Labels S2I horneadas en la imagen, vía `ImageStreamTag`
      (`io.openshift.build.source-location`, `org.opencontainers.image.source`).
   4. Comparar la imagen del contenedor directo contra el `output` de los
      BuildConfigs del namespace (soporta `output.to.kind: DockerImage`,
      para apps sin ImageStream).
   5. Como último recurso, `oc image info` sobre la imagen misma —
      independiente de ImageStream/BuildConfig —, para leer la label OCI
      `org.opencontainers.image.source` horneada por pipelines de CI
      externos a OpenShift (Jenkins, Tekton, GitHub Actions, etc.).

   Los pasos 1-5 son heurísticas automáticas que dependen de que exista
   **alguna señal en el cluster** (BuildConfig, label o anotación). Si el
   deployment se despliega vía CI externo sin BuildConfig, sin GitOps y
   sin hornear esas labels/anotaciones en la imagen, **no hay ninguna
   señal que leer** — en ese caso la única forma de asociarlo es el
   mapeo manual del paso 0.
5. Por cada repositorio único encontrado, consulta la API de GitHub:
   URL, si existe rama `main`, si tiene carpeta `.github`, si tiene
   `CODEOWNERS` (raíz, `.github/` o `docs/`) y sus `rulesets`.
6. Escribe todo en `audit-result.json` (respaldo local) y, además, lo
   inserta directamente en una base de datos Oracle (ver
   `scripts/oracle_schema.sql`): cada deployment/contenedor queda como una
   fila de `audit_deployments` con el repo de GitHub correspondiente
   **embebido** en la misma fila (owner, url, rama, CODEOWNERS,
   rulesets, etc.), autocontenida y sin necesidad de cruzar con otra
   tabla.

El resultado ya no se sube como artefacto del workflow: se inserta en Oracle
porque el runner no tiene salida a internet hacia GitHub para subir
artefactos.

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
  - `DB_ORACLE_USER`, `DB_ORACLE_PASSWORD`, `DB_ORACLE_DSN`: credenciales
    y cadena de conexión (easy connect, ej.
    `dbhost.miempresa.local:1521/ORCLPDB1`) de la base de datos Oracle
    donde se inserta el resultado. Ejecutar antes `scripts/oracle_schema.sql`
    una sola vez contra ese esquema.
- **Oracle Instant Client** instalado en el runner (lo usa `cx_Oracle`,
  fijado en `scripts/requirements.txt` en la versión 8.3.0 por ser la
  última compatible con Python 3.6.8).
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
python scripts/openshift_github_audit.py --output audit-result.json --skip-db
```

`--skip-db` omite la inserción en Oracle (solo escribe el JSON local); sin
credenciales de `DB_ORACLE_*` el script falla rápido si no se pasa ese flag.

Para diagnosticar namespaces donde el repo sigue quedando `null`, agrega
`--debug-unmatched`: por cada contenedor sin repo, imprime en el log qué
BuildConfigs/ImageStreams existen en su namespace y qué `output`/`source`
tienen, para comparar a mano contra la imagen del contenedor.

## Mapeo manual (`scripts/repo_mapping.json`)

Cuando `--debug-unmatched` confirma que un deployment no tiene ninguna
señal automática detectable (sin BuildConfig, sin labels/anotaciones, sin
ImageStream con output reconocible), la única forma de asociarlo a su
repo es declararlo a mano:

1. Copia `scripts/repo_mapping.example.json` a `scripts/repo_mapping.json`.
2. Agrega una entrada por cada `namespace/deployment` que necesites, con
   el `owner/repo` real:
   ```json
   {
     "credivirtual-prod/crediypy-card-manager": "credibanco-repositories/crediypy-card-manager",
     "credivirtual-prod/crediypy-gateway": "credibanco-repositories/crediypy-gateway"
   }
   ```
   Si un Deployment tiene varios contenedores que van a repos distintos,
   se puede ser más específico con `namespace/deployment/container`.
3. Commitea `scripts/repo_mapping.json` al repo (así el workflow lo
   encuentra automáticamente en `--repo-mapping-file`, que por default
   apunta a esa ruta relativa al checkout).

Una entrada en este archivo tiene **prioridad total**: si existe, se usa
directamente y el deployment no pasa por ninguna de las heurísticas
automáticas (queda con `github_source.detection_method: "manual-mapping"`
en el JSON/Oracle). El archivo es opcional — si no existe, el script
simplemente lo ignora y sigue con las heurísticas automáticas.

## Limitaciones conocidas

- Solo detecta el repo de origen si existe alguna de las señales
  soportadas (ver "Qué hace" arriba). Si una imagen no tiene ninguna —
  típicamente imágenes de terceros/vendor como
  `registry.redhat.io/...` que no corresponden a un repo propio — el
  deployment queda registrado igual pero con `repo: null` y
  `repo_key: null`. Esto es esperado, no un bug: no todo deployment tiene
  un repo de la organización detrás.
- El fallback de `oc image info` requiere que el runner pueda alcanzar el
  registry de la imagen (el interno del cluster siempre debería ser
  alcanzable; uno externo como Docker Hub o Quay puede no serlo si el
  runner no tiene salida a internet, en cuyo caso ese método simplemente
  no encuentra nada para esas imágenes).
- El campo `has_main_branch` verifica una rama llamada literalmente
  `main` (no el `default_branch` configurado del repo).
- Los rulesets requieren permisos de administración sobre el repo; si el
  `GH_PAT` no los tiene, `rulesets.accessible` queda en `false` con el
  motivo en `rulesets.error`.
