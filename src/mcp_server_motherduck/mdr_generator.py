"""Generatore MDR dentro la chat di Gemini Enterprise.

Pannello interattivo (MCP Apps) con cui l'utente carica gli Scope of Work, avvia una generazione,
ne segue l'avanzamento e scarica l'MDR. Il server comanda il motore di generazione su Google Cloud:
  - bucket Cloud Storage: PDF caricati direttamente dal browser (link firmati), stato e risultati
  - Cloud Run Job: il generatore vero e proprio
Attivo solo se MDR_GENERATOR_ENABLED è "true". Variabili d'ambiente:
  MDR_GCP_KEY_JSON   contenuto della chiave dell'account di servizio "mdr-gemini" (segreto)
  MDR_GCP_PROJECT    progetto Google Cloud (es. mdr-ai)
  MDR_GCP_REGION     regione del job (es. europe-west1)
  MDR_BUCKET         bucket dati (es. mdr-ai-gemini-mdr)
  MDR_JOB_NAME       nome del Cloud Run Job (es. mdr-generator-job)
Facoltative: MDR_MAX_PARALLEL (default 0 = nessun limite), MDR_STALE_HOURS (default 4),
  MDR_MAX_FILE_MB (default 32), MDR_MAX_UPLOAD_MB (default 100).
Layout nel bucket (lo scrive anche il motore): jobs/<id>/input/*.pdf, status.json, log.txt, output/...
"""

import datetime as dt
import json
import logging
import os
import re
import secrets
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import anyio
import mcp.types as mcp_types
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

logger = logging.getLogger("mcp_server_motherduck")

GEN_URI = "ui://renco-mdr/generatore"
GEN_MIME = "text/html;profile=mcp-app"
GEN_HTML = Path(__file__).parent / "assets" / "generatore_mdr.html"
GEN_UI_META = {
    "ui": {
        "prefersBorder": True,
        "csp": {
            # I PDF vanno direttamente nel bucket tramite link temporanei firmati.
            "connectDomains": ["https://storage.googleapis.com"],
            "resourceDomains": [],
        },
    }
}
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

TOTAL_STEPS = 13
STATE_LABELS = {
    "queued": "In coda",
    "running": "In corso",
    "succeeded": "Completato",
    "failed": "Errore",
    "cancelled": "Annullato",
}
ACTIVE_STATES = {"queued", "running"}
PROJECT_CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,39}$")
JOB_ID_RE = re.compile(r"^[0-9]{8}-[0-9]{6}-[A-Za-z0-9_\-]{1,40}-[0-9a-f]{4}$")
SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._\- ]+")
MB = 1024 * 1024
LINK_MINUTES = 15


def generator_enabled() -> bool:
    return os.environ.get("MDR_GENERATOR_ENABLED", "").strip().lower() in ("1", "true", "yes")


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _env_num(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


# --------------------------------------------------------------------------
# Archivio e avvio dei run: Google Cloud in produzione, cartella locale nelle prove
# --------------------------------------------------------------------------
class GcsBackend:
    """Cloud Storage + Cloud Run Job, con la chiave dell'account di servizio dedicato."""

    def __init__(self, key_json: str, project: str, region: str, bucket: str, job: str) -> None:
        from google.cloud import run_v2, storage
        from google.oauth2 import service_account

        info = json.loads(key_json)
        self.creds = service_account.Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        self.project, self.region, self.job = project, region, job
        self.client = storage.Client(project=project, credentials=self.creds)
        self.bucket = self.client.bucket(bucket)
        self._run_v2 = run_v2
        self.jobs = run_v2.JobsClient(credentials=self.creds)
        self.executions = run_v2.ExecutionsClient(credentials=self.creds)

    def list_job_ids(self) -> List[str]:
        it = self.client.list_blobs(self.bucket, prefix="jobs/", delimiter="/")
        for _ in it:  # i "prefissi" si riempiono scorrendo le pagine
            pass
        return sorted((p.split("/")[1] for p in it.prefixes if p.count("/") >= 2), reverse=True)

    def list_keys(self, prefix: str) -> List[str]:
        return sorted(b.name for b in self.client.list_blobs(self.bucket, prefix=prefix))

    def read_json(self, key: str) -> Optional[Dict]:
        from google.api_core.exceptions import NotFound

        try:
            return json.loads(self.bucket.blob(key).download_as_text())
        except NotFound:
            return None
        except ValueError:
            return None

    def write_json(self, key: str, data: Dict) -> None:
        self.bucket.blob(key).upload_from_string(
            json.dumps(data, ensure_ascii=False, indent=2), content_type="application/json"
        )

    def exists(self, key: str) -> bool:
        return self.bucket.blob(key).exists()

    def size(self, key: str) -> Optional[int]:
        blob = self.bucket.get_blob(key)
        return blob.size if blob is not None else None

    def read_head(self, key: str, length: int = 5) -> bytes:
        return self.bucket.blob(key).download_as_bytes(start=0, end=length - 1)

    def delete_prefix(self, prefix: str) -> None:
        if not prefix.endswith("/") or prefix.count("/") < 2:
            raise ValueError(f"Prefisso non valido: {prefix}")
        for blob in self.client.list_blobs(self.bucket, prefix=prefix):
            blob.delete()

    def delete_key(self, key: str) -> None:
        blob = self.bucket.blob(key)
        if blob.exists():
            blob.delete()

    def copy(self, src: str, dst: str) -> None:
        self.bucket.copy_blob(self.bucket.blob(src), self.bucket, dst)

    def signed_put_url(self, key: str, content_type: str, minutes: int = 30) -> str:
        return self.bucket.blob(key).generate_signed_url(
            version="v4", expiration=dt.timedelta(minutes=minutes), method="PUT",
            content_type=content_type, credentials=self.creds,
        )

    def signed_get_url(self, key: str, filename: str, content_type: str, disposition: str, minutes: int = 10) -> str:
        return self.bucket.blob(key).generate_signed_url(
            version="v4", expiration=dt.timedelta(minutes=minutes), method="GET",
            response_type=content_type,
            response_disposition=f"{disposition}; filename*=UTF-8''{quote(filename)}",
            credentials=self.creds,
        )

    def launch(self, job_id: str, project_code: str, start_date: str) -> Optional[str]:
        env = {"JOB_ID": job_id, "PROJECT_CODE": project_code}
        if start_date:
            env["PROJECT_START_DATE"] = start_date
        run_v2 = self._run_v2
        overrides = run_v2.RunJobRequest.Overrides(
            container_overrides=[
                run_v2.RunJobRequest.Overrides.ContainerOverride(
                    env=[run_v2.EnvVar(name=k, value=v) for k, v in env.items()]
                )
            ]
        )
        name = f"projects/{self.project}/locations/{self.region}/jobs/{self.job}"
        operation = self.jobs.run_job(request=run_v2.RunJobRequest(name=name, overrides=overrides))
        try:  # non si attende la fine (decine di minuti): basta il nome dell'esecuzione
            return operation.metadata.name or None
        except Exception:  # noqa: BLE001
            return None

    def execution_failed(self, execution: Optional[str]) -> Optional[str]:
        if not execution or not execution.startswith("projects/"):
            return None
        try:
            ex = self.executions.get_execution(name=execution)
        except Exception:  # noqa: BLE001
            return None
        if not ex.completion_time or ex.succeeded_count:
            return None
        if ex.cancelled_count:
            return "Esecuzione annullata"
        return "L'esecuzione si è interrotta prima di completare"

    def cancel(self, execution: Optional[str]) -> None:
        if not execution or not execution.startswith("projects/"):
            return
        try:
            self.executions.cancel_execution(name=execution)
        except Exception as exc:  # noqa: BLE001
            # Il motore si ferma comunque da solo entro pochi secondi grazie al marcatore "cancel".
            logger.warning("cancel_execution non riuscito (%s): il motore si fermerà dal marcatore", exc)


class LocalBackend:
    """Cartella locale: solo per le prove (nessun Google Cloud). Gli URL firmati sono simulati."""

    def __init__(self, root: str, base_url: str = "http://127.0.0.1:8099", launcher=None) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.base_url, self.launcher = base_url, launcher

    def _p(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if self.root not in path.parents:
            raise ValueError(f"Chiave non valida: {key}")
        return path

    def list_job_ids(self) -> List[str]:
        base = self.root / "jobs"
        return sorted((p.name for p in base.iterdir() if p.is_dir()), reverse=True) if base.exists() else []

    def list_keys(self, prefix: str) -> List[str]:
        return sorted(p.relative_to(self.root).as_posix() for p in self.root.rglob("*")
                      if p.is_file() and p.relative_to(self.root).as_posix().startswith(prefix))

    def read_json(self, key: str) -> Optional[Dict]:
        p = self._p(key)
        try:
            return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None
        except ValueError:
            return None

    def write_json(self, key: str, data: Dict) -> None:
        p = self._p(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def exists(self, key: str) -> bool:
        return self._p(key).is_file()

    def size(self, key: str) -> Optional[int]:
        p = self._p(key)
        return p.stat().st_size if p.is_file() else None

    def read_head(self, key: str, length: int = 5) -> bytes:
        with self._p(key).open("rb") as f:
            return f.read(length)

    def delete_prefix(self, prefix: str) -> None:
        if not prefix.endswith("/") or prefix.count("/") < 2:
            raise ValueError(f"Prefisso non valido: {prefix}")
        shutil.rmtree(self._p(prefix.rstrip("/")), ignore_errors=True)

    def delete_key(self, key: str) -> None:
        self._p(key).unlink(missing_ok=True)

    def copy(self, src: str, dst: str) -> None:
        d = self._p(dst)
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self._p(src), d)

    def signed_put_url(self, key: str, content_type: str, minutes: int = 30) -> str:
        return f"{self.base_url}/put/{key}"

    def signed_get_url(self, key: str, filename: str, content_type: str, disposition: str, minutes: int = 10) -> str:
        return f"{self.base_url}/get/{key}"

    def launch(self, job_id: str, project_code: str, start_date: str) -> Optional[str]:
        return self.launcher(job_id, project_code, start_date) if self.launcher else "local-test"

    def execution_failed(self, execution: Optional[str]) -> Optional[str]:
        return None

    def cancel(self, execution: Optional[str]) -> None:
        return None


# --------------------------------------------------------------------------
# Logica del generatore (stessa del sito mdr-cloud, senza sito)
# --------------------------------------------------------------------------
def valid_job_id(job_id: str) -> bool:
    return bool(JOB_ID_RE.match(job_id or ""))


def new_job_id(project_code: str) -> str:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{project_code}-{secrets.token_hex(2)}"


def status_key(job_id: str) -> str:
    return f"jobs/{job_id}/status.json"


def _fmt_dt(value: Optional[str]) -> str:
    if not value:
        return ""
    try:
        d = dt.datetime.fromisoformat(value)
    except ValueError:
        return value
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d.astimezone(dt.timezone(dt.timedelta(hours=_tz_offset_hours(d)))).strftime("%d/%m/%Y %H:%M")


def _tz_offset_hours(d: dt.datetime) -> int:
    """Ora italiana (CET/CEST) senza dipendere dai dati di fuso del sistema."""
    year = d.year
    def last_sunday(month: int) -> dt.datetime:
        day = dt.datetime(year, month, 31, 1, 0, tzinfo=dt.timezone.utc)
        return day - dt.timedelta(days=(day.weekday() + 1) % 7)
    return 2 if last_sunday(3) <= d.astimezone(dt.timezone.utc) < last_sunday(10) else 1


SUMMARY_LABELS = [("mdr_rows", "Righe MDR"), ("documents", "Documenti"), ("elapsed_minutes", "Durata (minuti)")]


def _public_summary(summary: Any) -> Optional[Dict]:
    """Del riepilogo del motore si mostrano solo alcuni dati, in italiano (niente costi)."""
    if not isinstance(summary, dict):
        return None
    out = {label: summary[key] for key, label in SUMMARY_LABELS
           if isinstance(summary.get(key), (int, float, str)) and summary.get(key) != ""}
    return out or None


def _fmt_date(value: Optional[str]) -> str:
    if not value:
        return "data del run"
    try:
        return dt.date.fromisoformat(value).strftime("%d/%m/%Y")
    except ValueError:
        return value


def _age_hours(value: Optional[str]) -> float:
    if not value:
        return 0.0
    try:
        then = dt.datetime.fromisoformat(value)
    except ValueError:
        return 0.0
    if then.tzinfo is None:
        then = then.replace(tzinfo=dt.timezone.utc)
    return (dt.datetime.now(dt.timezone.utc) - then).total_seconds() / 3600


class GeneratorService:
    def __init__(self, backend) -> None:
        self.b = backend
        self._final: Dict[str, Dict] = {}
        self.max_parallel = int(_env_num("MDR_MAX_PARALLEL", 0))
        self.stale_hours = _env_num("MDR_STALE_HOURS", 4)
        self.max_file_mb = int(_env_num("MDR_MAX_FILE_MB", 32))
        self.max_total_mb = int(_env_num("MDR_MAX_UPLOAD_MB", 100))

    # -- stato -----------------------------------------------------------------
    def _refresh(self, run: Dict) -> Dict:
        """Allinea lo stato se l'esecuzione è finita senza aggiornarlo (annullata, interrotta, bloccata)."""
        if run.get("state") not in ACTIVE_STATES:
            return run
        job_id = run["job_id"]
        changes: Dict = {}
        reason = self.b.execution_failed(run.get("execution"))
        if reason:
            if self.b.exists(f"jobs/{job_id}/cancel"):
                changes = {"state": "cancelled", "step_label": "Annullato", "error": None}
            else:
                changes = {"state": "failed", "error": reason}
        elif _age_hours(run.get("updated_at") or run.get("created_at")) > self.stale_hours:
            changes = {"state": "failed",
                       "error": f"Nessun aggiornamento da oltre {self.stale_hours:g} ore: generazione considerata interrotta."}
        if changes:
            run.update(changes, finished_at=_now_iso(), updated_at=_now_iso())
            self.b.write_json(status_key(job_id), run)
        return run

    def view(self, run: Dict) -> Dict:
        state = run.get("state", "queued")
        step = int(run.get("step") or 0)
        outputs = run.get("outputs") or {}
        return {
            "job_id": run["job_id"],
            "project_code": run.get("project_code"),
            "state": state,
            "state_label": STATE_LABELS.get(state, state),
            "active": state in ACTIVE_STATES,
            "progress": 100 if state == "succeeded" else round(100 * step / TOTAL_STEPS),
            "step": step,
            "total_steps": run.get("total_steps") or TOTAL_STEPS,
            "step_label": run.get("step_label"),
            "created_label": _fmt_dt(run.get("created_at")),
            "finished_label": _fmt_dt(run.get("finished_at")),
            "start_label": _fmt_date(run.get("project_start_date")) if run.get("project_start_date") else "",
            "created_by": run.get("created_by"),
            "error": run.get("error"),
            "sow_files": run.get("sow_files") or [],
            "has_mdr": bool(outputs.get("mdr")),
            "summary": _public_summary(run.get("summary")),
        }

    def get(self, job_id: str) -> Dict:
        if not valid_job_id(job_id):
            raise ValueError("Generazione non trovata.")
        run = self._load(job_id)
        if not run:
            raise ValueError("Generazione non trovata.")
        return run

    def _load(self, job_id: str) -> Optional[Dict]:
        """Stato di una generazione. Gli stati finali non cambiano più: si tengono in memoria."""
        cached = self._final.get(job_id)
        if cached is not None:
            return dict(cached)
        run = self.b.read_json(status_key(job_id))
        if not run:
            return None
        run = self._refresh(run)
        if run.get("state") not in ACTIVE_STATES:
            self._final[job_id] = dict(run)
        return run

    def list_runs(self, limit: int = 10, state: str = "", q: str = "") -> Tuple[List[Dict], bool]:
        ids = self.b.list_job_ids()
        q = (q or "").strip().lower()
        if q:  # il codice progetto è nell'id: si filtra senza leggere gli stati
            ids = [i for i in ids if q in "-".join(i.split("-")[2:-1]).lower()]
        out: List[Dict] = []
        batch = max(limit + 1, 8)
        with ThreadPoolExecutor(max_workers=8) as pool:
            for start in range(0, len(ids), batch):
                runs = list(pool.map(self._load, ids[start:start + batch]))  # in parallelo, nell'ordine degli id
                for run in runs:
                    if not run:
                        continue
                    if state == "active" and run.get("state") not in ACTIVE_STATES:
                        continue
                    if state and state != "active" and run.get("state") != state:
                        continue
                    out.append(self.view(run))
                if len(out) > limit:
                    break
        return out[:limit], len(out) > limit

    def active_count(self) -> int:
        with ThreadPoolExecutor(max_workers=8) as pool:
            runs = list(pool.map(self._load, self.b.list_job_ids()[:30]))
        return sum(1 for run in runs if run and run.get("state") in ACTIVE_STATES)

    # -- nuova generazione -------------------------------------------------------
    def _check(self, project_code: str, start_date: str, files: List[Tuple[str, int]]) -> Optional[str]:
        if not PROJECT_CODE_RE.match(project_code):
            return "Codice progetto non valido: usa lettere, numeri, '-' o '_'."
        if start_date:
            try:
                dt.date.fromisoformat(start_date)
            except ValueError:
                return "Data di inizio non valida."
        if not files:
            return "Carica almeno un PDF di Scope of Work."
        for name, size in files:
            if not str(name).lower().endswith(".pdf"):
                return f"'{name}' non è un PDF."
            if size > self.max_file_mb * MB:
                return f"'{name}' supera {self.max_file_mb} MB: il limite per singolo PDF."
        if sum(size for _, size in files) > self.max_total_mb * MB:
            return f"I file superano {self.max_total_mb} MB in totale."
        if self.max_parallel and self.active_count() >= self.max_parallel:
            return "Ci sono già generazioni in corso: attendi che finiscano (o annullale) e riprova."
        return None

    @staticmethod
    def _safe_names(names: List[str]) -> List[str]:
        out: List[str] = []
        seen: set = set()
        for name in names:
            safe = SAFE_NAME_RE.sub("_", Path(str(name)).name).strip() or "sow.pdf"
            while safe.lower() in seen:
                safe = f"_{safe}"
            seen.add(safe.lower())
            out.append(safe)
        return out

    def prepare_upload(self, project_code: str, start_date: str, files: List[Tuple[str, int]], user: str = "") -> Dict:
        project_code, start_date = project_code.strip(), start_date.strip()
        error = self._check(project_code, start_date, files)
        if error:
            raise ValueError(error)
        job_id = new_job_id(project_code)
        names = self._safe_names([n for n, _ in files])
        uploads = []
        for name, (original, size) in zip(names, files):
            uploads.append({"name": name, "original": original, "size": size,
                            "url": self.b.signed_put_url(f"jobs/{job_id}/input/{name}", "application/pdf")})
        self.b.write_json(f"jobs/{job_id}/pending.json", {
            "project_code": project_code, "start_date": start_date, "user": user, "created_at": _now_iso(),
            "files": [{"name": u["name"], "size": u["size"]} for u in uploads],
        })
        return {"job_id": job_id, "uploads": uploads}

    def start(self, job_id: str, user: str = "") -> Dict:
        if not valid_job_id(job_id):
            raise ValueError("Generazione non trovata.")
        pending = self.b.read_json(f"jobs/{job_id}/pending.json")
        if not pending:
            raise ValueError("Caricamento non trovato o già avviato.")
        names = [f["name"] for f in pending.get("files", [])]
        sizes: List[Tuple[str, int]] = []
        for name in names:
            key = f"jobs/{job_id}/input/{name}"
            size = self.b.size(key)
            if size is None:
                raise ValueError(f"'{name}' non è arrivato: riprova.")
            if not self.b.read_head(key, 5).startswith(b"%PDF"):
                self.b.delete_prefix(f"jobs/{job_id}/")
                raise ValueError(f"'{name}' non è un PDF valido.")
            sizes.append((name, size))
        error = self._check(pending["project_code"], pending.get("start_date") or "", sizes)
        if error:
            self.b.delete_prefix(f"jobs/{job_id}/")
            raise ValueError(error)
        view = self._launch(job_id, pending["project_code"], pending.get("start_date") or "", names,
                            user or pending.get("user", ""))
        self.b.delete_key(f"jobs/{job_id}/pending.json")
        return view

    def _launch(self, job_id: str, project_code: str, start_date: str, names: List[str], user: str,
                retry_of: Optional[str] = None) -> Dict:
        """Scrive lo stato iniziale e avvia il motore (i SoW sono già nel bucket)."""
        status = {
            "job_id": job_id, "project_code": project_code,
            "project_start_date": start_date or None,
            "state": "queued", "step": 0, "total_steps": TOTAL_STEPS, "step_label": "In coda",
            "created_at": _now_iso(), "updated_at": _now_iso(),
            "created_by": user, "sow_files": sorted(names),
        }
        if retry_of:
            status["retry_of"] = retry_of
        self.b.write_json(status_key(job_id), status)
        try:
            execution = self.b.launch(job_id, project_code, start_date)
        except Exception as exc:  # noqa: BLE001
            logger.exception("avvio del job non riuscito")
            status.update(state="failed", error=f"Avvio non riuscito: {exc}", finished_at=_now_iso())
            self.b.write_json(status_key(job_id), status)
            return self.view(status)
        # Il motore potrebbe aver già scritto lo stato: si aggiunge solo l'esecuzione.
        current = self.b.read_json(status_key(job_id)) or status
        current["execution"] = execution
        self.b.write_json(status_key(job_id), current)
        return self.view(current)

    def delete(self, job_ids: List[str]) -> Dict:
        """Elimina dal bucket le generazioni indicate (PDF, risultati, log). Quelle in corso si saltano.
        Lo storico di audit del progetto (history/) resta: serve al confronto di stabilità del run successivo."""
        deleted: List[str] = []
        skipped: List[Dict] = []
        for job_id in dict.fromkeys(job_ids or []):
            if not valid_job_id(job_id):
                skipped.append({"job_id": job_id, "motivo": "non valida"})
                continue
            run = self.b.read_json(status_key(job_id))
            if run and self._refresh(run).get("state") in ACTIVE_STATES:
                skipped.append({"job_id": job_id, "motivo": "in corso: annullala prima"})
                continue
            self.b.delete_prefix(f"jobs/{job_id}/")
            self._final.pop(job_id, None)
            deleted.append(job_id)
        return {"eliminati": deleted, "saltati": skipped}

    def retry(self, job_id: str, user: str = "") -> Dict:
        """Nuova generazione con gli stessi SoW e parametri (quella originale resta com'è)."""
        run = self.get(job_id)
        if run.get("state") in ACTIVE_STATES:
            raise ValueError("La generazione è ancora in corso.")
        if self.max_parallel and self.active_count() >= self.max_parallel:
            raise ValueError("Ci sono già generazioni in corso: attendi che finiscano (o annullale) e riprova.")
        names = [n for n in (run.get("sow_files") or []) if self.b.exists(f"jobs/{job_id}/input/{n}")]
        if not names:
            raise ValueError("I file SoW di questa generazione non sono più disponibili.")
        new_id = new_job_id(run.get("project_code", ""))
        for name in names:
            self.b.copy(f"jobs/{job_id}/input/{name}", f"jobs/{new_id}/input/{name}")
        return self._launch(new_id, run.get("project_code", ""), run.get("project_start_date") or "",
                            names, user, retry_of=job_id)

    def cancel(self, job_id: str, user: str = "") -> Dict:
        run = self.get(job_id)
        if run.get("state") not in ACTIVE_STATES:
            raise ValueError("La generazione non è in corso.")
        self.b.write_json(f"jobs/{job_id}/cancel", {"by": user, "at": _now_iso()})
        self.b.cancel(run.get("execution"))
        run.update(state="cancelled", step_label="Annullato", cancelled_by=user or None,
                   finished_at=_now_iso(), updated_at=_now_iso())
        self.b.write_json(status_key(job_id), run)
        return self.view(run)

    def link(self, job_id: str, kind: str, name: str = "") -> Dict:
        run = self.get(job_id)
        outputs = run.get("outputs") or {}
        if kind == "mdr":
            key, filename, ctype, disp = outputs.get("mdr"), None, XLSX, "attachment"
        elif kind == "log":
            key, filename, ctype, disp = f"jobs/{job_id}/log.txt", f"log-{job_id}.txt", "text/plain", "attachment"
        elif kind == "sow":
            if name not in (run.get("sow_files") or []):
                raise ValueError("File non trovato.")
            key, filename, ctype, disp = f"jobs/{job_id}/input/{name}", name, "application/pdf", "inline"
        else:
            raise ValueError("Tipo di file sconosciuto.")
        if not key or not self.b.exists(key):
            raise ValueError("File non disponibile.")
        filename = filename or key.rsplit("/", 1)[-1]
        return {"url": self.b.signed_get_url(key, filename, ctype, disp), "nome": filename}

    def links(self, job_id: str) -> Dict:
        """Tutti i link di download in una volta: il pannello li scarica in anticipo."""
        run = self.get(job_id)
        outputs = run.get("outputs") or {}

        def one(key: str, filename: Optional[str], ctype: str, disp: str) -> Dict:
            name = filename or key.rsplit("/", 1)[-1]
            return {"url": self.b.signed_get_url(key, name, ctype, disp, LINK_MINUTES), "nome": name}

        out: Dict[str, Any] = {"scadenza_minuti": LINK_MINUTES, "sow": {}}
        if outputs.get("mdr"):
            out["mdr"] = one(outputs["mdr"], None, XLSX, "attachment")
        if run.get("state") in ("succeeded", "failed", "cancelled"):
            out["log"] = one(f"jobs/{job_id}/log.txt", f"log-{job_id}.txt", "text/plain", "attachment")
        for name in run.get("sow_files") or []:
            out["sow"][name] = one(f"jobs/{job_id}/input/{name}", name, "application/pdf", "inline")
        return out


# --------------------------------------------------------------------------
# Registrazione nel server MCP
# --------------------------------------------------------------------------
def _inject_resource_meta(mcp: FastMCP) -> None:
    """FastMCP 2.x non riporta _meta nel contenuto letto della risorsa: lo aggiungiamo qui."""
    handlers = mcp._mcp_server.request_handlers
    original = handlers[mcp_types.ReadResourceRequest]

    async def handler(req: mcp_types.ReadResourceRequest):
        result = await original(req)
        if str(req.params.uri) == GEN_URI and isinstance(result.root, mcp_types.ReadResourceResult):
            for item in result.root.contents:
                item.meta = GEN_UI_META
        return result

    handlers[mcp_types.ReadResourceRequest] = handler


def _backend_from_env():
    missing = [n for n in ("MDR_GCP_KEY_JSON", "MDR_GCP_PROJECT", "MDR_GCP_REGION", "MDR_BUCKET", "MDR_JOB_NAME")
               if not os.environ.get(n, "").strip()]
    if missing:
        raise RuntimeError("variabili mancanti: " + ", ".join(missing))
    return GcsBackend(
        os.environ["MDR_GCP_KEY_JSON"], os.environ["MDR_GCP_PROJECT"].strip(), os.environ["MDR_GCP_REGION"].strip(),
        os.environ["MDR_BUCKET"].strip(), os.environ["MDR_JOB_NAME"].strip(),
    )


def register_mdr_generator(mcp: FastMCP, backend=None) -> None:
    if backend is None:
        if not generator_enabled():
            return
        try:
            backend = _backend_from_env()
        except Exception as exc:  # noqa: BLE001
            logger.error("Generatore MDR non attivo: %s", exc)
            return
    if not GEN_HTML.exists():
        logger.warning("Generatore MDR: file %s mancante, non registrato", GEN_HTML)
        return

    svc = GeneratorService(backend)
    _inject_resource_meta(mcp)

    async def run(fn, *args):
        started = time.monotonic()
        try:
            return await anyio.to_thread.run_sync(lambda: fn(*args))
        except ValueError as exc:
            raise ToolError(str(exc))
        finally:
            logger.info("generatore %s: %.1f s", getattr(fn, "__name__", "?"), time.monotonic() - started)

    @mcp.resource(GEN_URI, name="generatore_mdr", title="Generatore MDR",
                  description="Pannello per lanciare e seguire le generazioni MDR",
                  mime_type=GEN_MIME, meta=GEN_UI_META)
    def generatore_html() -> str:
        return GEN_HTML.read_text(encoding="utf-8")

    read_hints = {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False}
    write_hints = {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False}
    app_only = {"ui": {"resourceUri": GEN_URI, "visibility": ["app"]}}

    @mcp.tool(
        name="apri_generatore_mdr", title="Apri il generatore MDR",
        description=(
            "Mostra all'utente il pannello del Generatore MDR: da lì carica i PDF di Scope of Work, "
            "avvia una generazione, vede l'avanzamento e scarica l'MDR e il report qualità. "
            "Usalo quando l'utente vuole generare un MDR, caricare uno Scope of Work, "
            "vedere o riprendere le generazioni. Dopo la chiamata di' solo che il pannello è aperto: "
            "è il pannello a guidare l'utente."
        ),
        annotations=read_hints,
        meta={"ui": {"resourceUri": GEN_URI, "visibility": ["model", "app"]}},
    )
    async def apri_generatore_mdr() -> dict:
        runs, more = await run(svc.list_runs, 10)  # l'elenco arriva col pannello: si vede subito
        return {"messaggio": "Pannello del Generatore MDR aperto.",
                "attive": sum(1 for r in runs if r["active"]), "runs": runs, "altri": more,
                "max_file_mb": svc.max_file_mb, "max_upload_mb": svc.max_total_mb}

    @mcp.tool(
        name="elenco_generazioni_mdr", title="Elenco generazioni MDR",
        description=("Elenca le generazioni MDR più recenti con stato e avanzamento. "
                     "Filtri facoltativi: progetto (parte del codice) e stato (active, succeeded, failed, cancelled)."),
        annotations=read_hints,
    )
    async def elenco_generazioni_mdr(progetto: str = "", stato: str = "", massimo: int = 10) -> dict:
        runs, more = await run(svc.list_runs, max(1, min(massimo, 50)), stato, progetto)
        return {"generazioni": runs, "ce_ne_sono_altre": more}

    @mcp.tool(name="mdr_elenco", title="Elenco generazioni", description="Usato solo dal pannello.",
              annotations=read_hints, meta=app_only)
    async def mdr_elenco(q: str = "", stato: str = "", limit: int = 10) -> dict:
        runs, more = await run(svc.list_runs, max(1, min(int(limit), 50)),
                                 stato if stato in ("", "active", "succeeded", "failed", "cancelled") else "", (q or "")[:40])
        return {"runs": runs, "altri": more}

    @mcp.tool(name="mdr_dettaglio", title="Dettaglio generazione", description="Usato solo dal pannello.",
              annotations=read_hints, meta=app_only)
    async def mdr_dettaglio(job_id: str) -> dict:
        return svc.view(await run(svc.get, job_id))

    @mcp.tool(name="mdr_prepara_upload", title="Prepara caricamento", description="Usato solo dal pannello.",
              annotations=write_hints, meta=app_only)
    async def mdr_prepara_upload(project_code: str, start_date: str = "", files: Optional[List[dict]] = None) -> dict:
        parsed = [(str(f.get("name", "")), int(f.get("size") or 0)) for f in (files or [])]
        return await run(svc.prepare_upload, project_code, start_date, parsed, "gemini")

    @mcp.tool(name="mdr_avvia", title="Avvia generazione", description="Usato solo dal pannello.",
              annotations=write_hints, meta=app_only)
    async def mdr_avvia(job_id: str) -> dict:
        return await run(svc.start, job_id, "gemini")

    @mcp.tool(name="mdr_annulla", title="Annulla generazione", description="Usato solo dal pannello.",
              annotations=write_hints, meta=app_only)
    async def mdr_annulla(job_id: str) -> dict:
        return await run(svc.cancel, job_id, "gemini")

    @mcp.tool(name="mdr_elimina", title="Elimina generazioni", description="Usato solo dal pannello.",
              annotations={"readOnlyHint": False, "destructiveHint": True, "openWorldHint": False}, meta=app_only)
    async def mdr_elimina(job_ids: List[str]) -> dict:
        return await run(svc.delete, [str(j) for j in (job_ids or [])][:50])

    @mcp.tool(name="mdr_rilancia", title="Rilancia generazione", description="Usato solo dal pannello.",
              annotations=write_hints, meta=app_only)
    async def mdr_rilancia(job_id: str) -> dict:
        return await run(svc.retry, job_id, "gemini")

    @mcp.tool(name="mdr_link", title="Link di download", description="Usato solo dal pannello.",
              annotations=read_hints, meta=app_only)
    async def mdr_link(job_id: str, tipo: str, nome: str = "") -> dict:
        return await run(svc.link, job_id, tipo, nome)

    @mcp.tool(name="mdr_links", title="Link di download", description="Usato solo dal pannello.",
              annotations=read_hints, meta=app_only)
    async def mdr_links(job_id: str) -> dict:
        return await run(svc.links, job_id)

    logger.info("Generatore MDR registrato (%s)", GEN_URI)
