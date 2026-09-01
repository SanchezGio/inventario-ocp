#!/usr/bin/env python3
"""
openshift_github_audit.py

Recorre namespaces de OpenShift -> Deployments -> ImageStreams/Tags -> intenta
resolver el repositorio de GitHub de origen de cada imagen, y consulta en
GitHub metadata de gobierno (rama main, carpeta .github, CODEOWNERS,
rulesets). Produce un único JSON que asocia cada Deployment con el
repositorio detectado.

Requiere que `oc` ya esté logueado en el cluster (oc login previo) y la
variable de entorno GH_PAT con un token de GitHub.

Uso:
    python openshift_github_audit.py --output audit-result.json \
        --exclude-prefixes "openshift-,kube-" \
        --exclude-exact "default,openshift,kube-system" \
        --github-api-url https://api.github.com
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Any, Optional

import requests

# --------------------------------------------------------------------------
# Utilidades de bajo nivel
# --------------------------------------------------------------------------


def log(msg: str) -> None:
    print(f"[audit] {msg}", flush=True)


def oc_json(*args: str) -> Any:
    """Ejecuta `oc <args> -o json` y devuelve el JSON parseado.
    Devuelve None (y loguea un warning) si el comando falla, en vez de
    interrumpir todo el pipeline por un recurso puntual inaccesible."""
    cmd = ["oc", *args, "-o", "json"]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, check=True, timeout=120
        )
        return json.loads(result.stdout)
    except subprocess.CalledProcessError as exc:
        log(f"WARN: falló '{' '.join(cmd)}': {exc.stderr.strip()[:300]}")
        return None
    except subprocess.TimeoutExpired:
        log(f"WARN: timeout ejecutando '{' '.join(cmd)}'")
        return None
    except json.JSONDecodeError as exc:
        log(f"WARN: respuesta no-JSON de '{' '.join(cmd)}': {exc}")
        return None


# --------------------------------------------------------------------------
# Paso 1-2: Namespaces y Deployments
# --------------------------------------------------------------------------


def list_namespaces(exclude_prefixes: list[str], exclude_exact: set[str]) -> list[str]:
    data = oc_json("get", "namespace")
    if not data:
        return []
    names = [item["metadata"]["name"] for item in data.get("items", [])]
    kept = [
        n
        for n in names
        if n not in exclude_exact and not any(n.startswith(p) for p in exclude_prefixes if p)
    ]
    log(f"Namespaces totales: {len(names)}, tras exclusión: {len(kept)}")
    return kept


def list_deployments(namespace: str) -> list[dict]:
    data = oc_json("get", "deployment", "-n", namespace)
    if not data:
        return []
    return data.get("items", [])


# --------------------------------------------------------------------------
# Paso 3: Resolver ImageStream/Tag por cada contenedor de un Deployment
# --------------------------------------------------------------------------

TRIGGER_FIELDPATH_NAME_RE = re.compile(r'containers\[\?\(@\.name=="([^"]+)"\)\]')
TRIGGER_FIELDPATH_INDEX_RE = re.compile(r"containers\[(\d+)\]")


class NamespaceCache:
    """Cachea ImageStreams y BuildConfigs por namespace para no repetir
    llamadas `oc` por cada deployment/contenedor."""

    def __init__(self) -> None:
        self._imagestreams: dict[str, list[dict]] = {}
        self._buildconfigs: dict[str, list[dict]] = {}

    def imagestreams(self, ns: str) -> list[dict]:
        if ns not in self._imagestreams:
            data = oc_json("get", "imagestream", "-n", ns)
            self._imagestreams[ns] = data.get("items", []) if data else []
        return self._imagestreams[ns]

    def buildconfigs(self, ns: str) -> list[dict]:
        if ns not in self._buildconfigs:
            data = oc_json("get", "buildconfig", "-n", ns)
            self._buildconfigs[ns] = data.get("items", []) if data else []
        return self._buildconfigs[ns]


def resolve_container_imagestream(
    deployment: dict, namespace: str, cache: NamespaceCache
) -> list[dict]:
    """Devuelve, por cada contenedor del deployment, la info de matching:
    {"container": str, "image": str, "imagestream": str|None, "tag": str|None,
     "match_method": str}
    """
    containers = deployment["spec"]["template"]["spec"].get("containers", [])
    results = [
        {
            "container": c["name"],
            "image": c.get("image", ""),
            "imagestream": None,
            "tag": None,
            "match_method": "unmatched",
        }
        for c in containers
    ]

    # Método 1 (preferido): anotación image.openshift.io/triggers, estándar
    # de OpenShift para vincular un Deployment nativo con ImageStreamTags.
    annotations = deployment["metadata"].get("annotations", {})
    triggers_raw = annotations.get("image.openshift.io/triggers")
    if triggers_raw:
        try:
            triggers = json.loads(triggers_raw)
        except json.JSONDecodeError:
            triggers = []
        for trig in triggers:
            frm = trig.get("from", {})
            if frm.get("kind") != "ImageStreamTag":
                continue
            isname_tag = frm.get("name", "")
            if ":" not in isname_tag:
                continue
            isname, tag = isname_tag.split(":", 1)
            field_path = trig.get("fieldPath", "")

            target_idx = None
            m = TRIGGER_FIELDPATH_NAME_RE.search(field_path)
            if m:
                cname = m.group(1)
                for idx, c in enumerate(containers):
                    if c["name"] == cname:
                        target_idx = idx
                        break
            else:
                m2 = TRIGGER_FIELDPATH_INDEX_RE.search(field_path)
                if m2:
                    target_idx = int(m2.group(1))

            if target_idx is not None and 0 <= target_idx < len(results):
                results[target_idx]["imagestream"] = isname
                results[target_idx]["tag"] = tag
                results[target_idx]["match_method"] = "trigger-annotation"

    # Método 2 (fallback): comparar dockerImageReference de cada tag de
    # cada ImageStream del namespace contra el `image` real del contenedor.
    still_unmatched = [r for r in results if r["match_method"] == "unmatched"]
    if still_unmatched:
        streams = cache.imagestreams(namespace)
        for r in still_unmatched:
            if not r["image"]:
                continue
            for stream in streams:
                isname = stream["metadata"]["name"]
                for tag_status in stream.get("status", {}).get("tags", []):
                    tag_name = tag_status.get("tag")
                    for item in tag_status.get("items", []):
                        ref = item.get("dockerImageReference", "")
                        if ref and (ref == r["image"] or ref.split("@")[0] == r["image"].split("@")[0]):
                            r["imagestream"] = isname
                            r["tag"] = tag_name
                            r["match_method"] = "dockerImageReference-match"
                            break
                    if r["match_method"] != "unmatched":
                        break
                if r["match_method"] != "unmatched":
                    break

    return results


# --------------------------------------------------------------------------
# Paso 4: Del ImageStream/Tag, encontrar el repo de GitHub de origen
# --------------------------------------------------------------------------

GITHUB_URL_RE = re.compile(
    r"(?:https?://|git@|ssh://git@)?github\.com[:/]([\w.-]+)/([\w.-]+?)(?:\.git)?/?$"
)


def extract_github_repo(text: str) -> Optional[tuple[str, str]]:
    """Busca un owner/repo de GitHub dentro de un string arbitrario
    (URL de BuildConfig, valor de anotación o label)."""
    if not text:
        return None
    m = GITHUB_URL_RE.search(text.strip())
    if m:
        owner, repo = m.group(1), m.group(2)
        return owner, repo.removesuffix(".git")
    # búsqueda más laxa dentro de un texto largo (p.ej. una anotación con
    # varias palabras)
    m2 = re.search(r"github\.com[:/]([\w.-]+)/([\w.-]+?)(?:\.git)?(?=[\s\"'/]|$)", text)
    if m2:
        return m2.group(1), m2.group(2)
    return None


def find_source_via_buildconfig(
    namespace: str, imagestream: str, tag: str, cache: NamespaceCache
) -> Optional[dict]:
    for bc in cache.buildconfigs(namespace):
        output_to = bc.get("spec", {}).get("output", {}).get("to", {})
        if output_to.get("kind") != "ImageStreamTag":
            continue
        out_name = output_to.get("name", "")
        if out_name != f"{imagestream}:{tag}" and out_name.split(":")[0] != imagestream:
            continue
        source = bc.get("spec", {}).get("source", {})
        git = source.get("git")
        if git and git.get("uri"):
            repo = extract_github_repo(git["uri"])
            if repo:
                return {
                    "owner": repo[0],
                    "repo": repo[1],
                    "raw_uri": git["uri"],
                    "detection_method": "buildconfig-git-source",
                    "buildconfig": bc["metadata"]["name"],
                }
    return None


def find_source_via_imagestream_annotations(
    namespace: str, imagestream: str, tag: str, cache: NamespaceCache
) -> Optional[dict]:
    for stream in cache.imagestreams(namespace):
        if stream["metadata"]["name"] != imagestream:
            continue
        candidates: list[str] = []
        candidates.extend((stream["metadata"].get("annotations") or {}).values())
        for spec_tag in stream.get("spec", {}).get("tags", []):
            if spec_tag.get("name") == tag:
                candidates.extend((spec_tag.get("annotations") or {}).values())
        for value in candidates:
            if not isinstance(value, str):
                continue
            repo = extract_github_repo(value)
            if repo:
                return {
                    "owner": repo[0],
                    "repo": repo[1],
                    "raw_uri": value,
                    "detection_method": "imagestream-annotation",
                }
    return None


def find_source_via_image_labels(namespace: str, imagestream: str, tag: str) -> Optional[dict]:
    istag = oc_json("get", "imagestreamtag", f"{imagestream}:{tag}", "-n", namespace)
    if not istag:
        return None
    labels = (
        istag.get("image", {})
        .get("dockerImageMetadata", {})
        .get("Config", {})
        .get("Labels", {})
        or {}
    )
    # Convenciones estándar de builds S2I de OpenShift
    for key in ("io.openshift.build.source-location", "org.opencontainers.image.source"):
        value = labels.get(key)
        if value:
            repo = extract_github_repo(value)
            if repo:
                return {
                    "owner": repo[0],
                    "repo": repo[1],
                    "raw_uri": value,
                    "detection_method": f"image-label:{key}",
                }
    return None


def find_github_source(namespace: str, imagestream: str, tag: str, cache: NamespaceCache) -> Optional[dict]:
    if not imagestream or not tag:
        return None
    for finder in (
        lambda: find_source_via_buildconfig(namespace, imagestream, tag, cache),
        lambda: find_source_via_imagestream_annotations(namespace, imagestream, tag, cache),
        lambda: find_source_via_image_labels(namespace, imagestream, tag),
    ):
        result = finder()
        if result:
            return result
    return None


# --------------------------------------------------------------------------
# Paso 5: Consultas a GitHub
# --------------------------------------------------------------------------

CODEOWNERS_PATHS = ["CODEOWNERS", ".github/CODEOWNERS", "docs/CODEOWNERS"]


class GitHubClient:
    def __init__(self, token: str, api_url: str) -> None:
        self.api_url = api_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
        )

    def get(self, path: str) -> tuple[int, Any]:
        url = f"{self.api_url}{path}"
        for attempt in range(3):
            resp = self.session.get(url, timeout=30)
            if resp.status_code == 403 and "rate limit" in resp.text.lower():
                reset = resp.headers.get("X-RateLimit-Reset")
                wait = 5
                if reset:
                    wait = max(1, min(60, int(reset) - int(time.time())))
                log(f"WARN: rate limit de GitHub, esperando {wait}s...")
                time.sleep(wait)
                continue
            if resp.status_code >= 500:
                time.sleep(2 * (attempt + 1))
                continue
            try:
                return resp.status_code, resp.json()
            except ValueError:
                return resp.status_code, None
        return resp.status_code, None

    def repo_audit(self, owner: str, repo: str) -> dict:
        info: dict[str, Any] = {
            "owner": owner,
            "repo": repo,
            "url": f"https://github.com/{owner}/{repo}",
            "exists": False,
        }

        status, data = self.get(f"/repos/{owner}/{repo}")
        if status == 404:
            info["error"] = "repositorio no encontrado o sin acceso con el token dado"
            return info
        if status != 200 or not isinstance(data, dict):
            info["error"] = f"HTTP {status} consultando el repositorio"
            return info

        info["exists"] = True
        info["url"] = data.get("html_url", info["url"])
        info["private"] = data.get("private")
        info["default_branch"] = data.get("default_branch")
        info["archived"] = data.get("archived")

        status_b, _ = self.get(f"/repos/{owner}/{repo}/branches/main")
        info["has_main_branch"] = status_b == 200

        if info["has_main_branch"]:
            status_g, _ = self.get(f"/repos/{owner}/{repo}/contents/.github?ref=main")
            info["has_github_folder"] = status_g == 200

            codeowners_found = False
            codeowners_path = None
            for path in CODEOWNERS_PATHS:
                status_c, _ = self.get(f"/repos/{owner}/{repo}/contents/{path}?ref=main")
                if status_c == 200:
                    codeowners_found = True
                    codeowners_path = path
                    break
            info["codeowners"] = {"found": codeowners_found, "path": codeowners_path}
        else:
            info["has_github_folder"] = None
            info["codeowners"] = {"found": None, "path": None}

        status_r, data_r = self.get(f"/repos/{owner}/{repo}/rulesets?per_page=100")
        if status_r == 200 and isinstance(data_r, list):
            info["rulesets"] = {
                "accessible": True,
                "count": len(data_r),
                "names": [r.get("name") for r in data_r],
            }
        elif status_r == 403:
            info["rulesets"] = {
                "accessible": False,
                "error": "permisos insuficientes (se requiere admin/'Administration: read')",
            }
        elif status_r == 404:
            info["rulesets"] = {"accessible": False, "error": "no encontrado / no soportado"}
        else:
            info["rulesets"] = {"accessible": False, "error": f"HTTP {status_r}"}

        return info


# --------------------------------------------------------------------------
# Orquestación principal
# --------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="audit-result.json")
    parser.add_argument("--exclude-prefixes", default="openshift-,kube-")
    parser.add_argument("--exclude-exact", default="default,openshift,kube-system,kube-public,kube-node-lease")
    parser.add_argument("--github-api-url", default="https://api.github.com")
    args = parser.parse_args()

    import os

    gh_token = os.environ.get("GH_PAT")
    if not gh_token:
        log("ERROR: falta la variable de entorno GH_PAT")
        return 1

    exclude_prefixes = [p.strip() for p in args.exclude_prefixes.split(",") if p.strip()]
    exclude_exact = {e.strip() for e in args.exclude_exact.split(",") if e.strip()}

    cache = NamespaceCache()
    gh = GitHubClient(gh_token, args.github_api_url)
    repo_cache: dict[str, dict] = {}  # "owner/repo" -> repo_audit result
    deployments_out: list[dict] = []
    errors: list[str] = []

    namespaces = list_namespaces(exclude_prefixes, exclude_exact)

    for ns in namespaces:
        deployments = list_deployments(ns)
        log(f"Namespace '{ns}': {len(deployments)} deployment(s)")
        for dep in deployments:
            dep_name = dep["metadata"]["name"]
            try:
                container_matches = resolve_container_imagestream(dep, ns, cache)
            except Exception as exc:  # defensivo: nunca abortar todo el run
                errors.append(f"{ns}/{dep_name}: error resolviendo imagestream: {exc}")
                continue

            for cm in container_matches:
                entry: dict[str, Any] = {
                    "namespace": ns,
                    "deployment": dep_name,
                    "container": cm["container"],
                    "image": cm["image"],
                    "imagestream": (
                        {"name": cm["imagestream"], "tag": cm["tag"]}
                        if cm["imagestream"]
                        else None
                    ),
                    "imagestream_match_method": cm["match_method"],
                    "github_source": None,
                    "repo_key": None,
                }

                if cm["imagestream"] and cm["tag"]:
                    try:
                        source = find_github_source(ns, cm["imagestream"], cm["tag"], cache)
                    except Exception as exc:
                        errors.append(
                            f"{ns}/{dep_name}/{cm['container']}: error buscando origen GitHub: {exc}"
                        )
                        source = None

                    if source:
                        entry["github_source"] = {
                            "detection_method": source["detection_method"],
                            "raw_uri": source["raw_uri"],
                        }
                        repo_key = f"{source['owner']}/{source['repo']}"
                        entry["repo_key"] = repo_key

                        if repo_key not in repo_cache:
                            log(f"Consultando GitHub: {repo_key}")
                            try:
                                repo_cache[repo_key] = gh.repo_audit(source["owner"], source["repo"])
                            except Exception as exc:
                                errors.append(f"{repo_key}: error consultando GitHub: {exc}")
                                repo_cache[repo_key] = {
                                    "owner": source["owner"],
                                    "repo": source["repo"],
                                    "exists": None,
                                    "error": str(exc),
                                }

                deployments_out.append(entry)

    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "namespaces_scanned": len(namespaces),
            "deployment_containers_scanned": len(deployments_out),
            "unique_repositories_found": len(repo_cache),
            "deployment_containers_without_repo": sum(
                1 for e in deployments_out if not e["repo_key"]
            ),
        },
        "repositories": repo_cache,
        "deployments": deployments_out,
        "errors": errors,
    }

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    log(f"Resultado escrito en {args.output}")
    log(
        f"Namespaces: {result['summary']['namespaces_scanned']}, "
        f"contenedores: {result['summary']['deployment_containers_scanned']}, "
        f"repos únicos: {result['summary']['unique_repositories_found']}, "
        f"sin repo detectado: {result['summary']['deployment_containers_without_repo']}"
    )
    if errors:
        log(f"Se registraron {len(errors)} advertencias/errores no fatales (ver campo 'errors' del JSON)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
