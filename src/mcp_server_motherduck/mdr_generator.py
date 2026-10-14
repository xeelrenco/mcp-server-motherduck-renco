"""Generatore MDR dentro la chat di Gemini Enterprise.

Pannello interattivo (MCP Apps) con cui l'utente carica gli Scope of Work, avvia una generazione,
ne segue l'avanzamento e scarica l'MDR. Il server comanda il motore di generazione su Google Cloud:
  - bucket Cloud Storage: PDF caricati direttamente dal browser (link firmati), stato e risultati
  - Cloud Run Job: il generatore vero e proprio
Il pannello compare sempre; se mancano le variabili d'ambiente sotto mostra il motivo e non genera:
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

TOTAL_STEPS = 12
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


STEP_NAMES = ["Lettura Scope of Work", "Validazione discipline e capitoli", "Verifica catalogo documenti", "Filtri SoW sui documenti",
              "Profili documento", "Numero di istanze per documento", "Titoli specifici da SoW", "Espansione righe MDR",
              "Controllo documenti obbligatori", "Pianificazione date", "Confronto con storico", "Compilazione Excel MDR"]


def _parse_iso(value: Any) -> Optional[dt.datetime]:
    try:
        d = dt.datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)


# Passi del motore che non si mostrano (anche nelle generazioni già fatte): il 11 "Confronto con storico" non serve a leggere l'MDR.
HIDDEN_STEPS = {11}
VISIBLE_STEPS = [n for n in range(1, len(STEP_NAMES) + 1) if n not in HIDDEN_STEPS]


def _shown_step(step: int) -> int:
    """Passo da mostrare: se il motore è su un passo nascosto, per chi guarda è ancora in quello prima."""
    while step in HIDDEN_STEPS:
        step -= 1
    return step


def _timeline(run: Dict) -> List[Dict]:
    """I passi visibili con stato (done/current/failed/stopped/todo) e secondi impiegati (None se non registrati)."""
    state = run.get("state", "queued")
    step = _shown_step(int(run.get("step") or 0))
    times = run.get("step_times") or {}
    finished = _parse_iso(run.get("finished_at"))
    now = dt.datetime.now(dt.timezone.utc)
    out = []
    for k, n in enumerate(VISIBLE_STEPS):
        label = STEP_NAMES[n - 1]
        if state == "succeeded" or n < step:
            st = "done"
        elif n == step:
            st = {"failed": "failed", "cancelled": "stopped"}.get(state, "current")
        else:
            st = "todo"
        nxt = VISIBLE_STEPS[k + 1] if k + 1 < len(VISIBLE_STEPS) else None
        start, end = _parse_iso(times.get(str(n))), _parse_iso(times.get(str(nxt))) if nxt else None
        if start and not end:
            if st == "current":
                end = now
            elif finished and (n == step or (state == "succeeded" and nxt is None)):
                end = finished
        sec = round((end - start).total_seconds()) if start and end and st != "todo" and end >= start else None
        out.append({"n": n, "label": label, "st": st, "sec": sec})
    return out


SUMMARY_LABELS = [("mdr_rows", "Righe MDR"), ("documents", "Documenti"), ("elapsed_minutes", "Durata (minuti)")]


def _public_digest(d: Any) -> Optional[Dict]:
    """Riepilogo dell'MDR per il pannello: righe per disciplina e righe previste per mese (solo dati puliti)."""
    if not isinstance(d, dict):
        return None
    def num(v: Any) -> int:
        return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0
    discs = [{"code": str(x.get("code") or "")[:12], "name": str(x.get("name") or "")[:60], "rows": num(x.get("rows"))}
             for x in (d.get("disciplines") or [])[:60] if isinstance(x, dict) and num(x.get("rows")) > 0]
    months = [{"ym": str(x.get("ym") or "")[:7], "rows": num(x.get("rows"))}
              for x in (d.get("months") or [])[:240] if isinstance(x, dict) and re.match(r"^\d{4}-\d{2}$", str(x.get("ym") or ""))]
    out: Dict[str, Any] = {"disciplines": discs, "months": months}
    for k in ("first", "last"):
        v = str(d.get(k) or "")
        if re.match(r"^\d{4}-\d{2}-\d{2}$", v):
            out[k] = v
    return out if discs else None


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


def _explain_failure(run: Dict) -> str:
    """Causa in italiano, leggibile da un utente non tecnico, per una generazione fallita."""
    text = " ".join([str(run.get("error") or "")] + [str(x) for x in (run.get("log_tail") or [])]).lower()
    rules = [
        (("nessun aggiornamento", "considerata interrotta"), "La generazione si è fermata senza dare più notizie. Di solito basta riprovare."),
        (("excel mdr non prodotto",), "Il motore è terminato ma non ha prodotto l'Excel MDR. Controlla che lo Scope of Work contenga l'elenco dei documenti, poi riprova."),
        (("429", "quota", "rate limit", "resource_exhausted", "too many requests"), "Il servizio di intelligenza artificiale ha raggiunto il limite di richieste. Attendi qualche minuto e riprova."),
        (("timeout", "timed out", "deadline"), "Il servizio ha impiegato troppo tempo a rispondere. Riprova tra poco."),
        (("encrypted", "password", "pdfsyntax", "pdf read", "cannot read pdf", "invalid pdf", "no /root"), "Uno dei file PDF non è leggibile (protetto da password o danneggiato). Controlla i file caricati e riprova."),
        (("api key", "api_key", "unauthorized", "401", "403", "permission"), "Il motore non è riuscito ad accedere a uno dei servizi esterni. Segnalalo a Xeel: riprovare non basta."),
        (("memory", "oom", "killed"), "Il motore ha esaurito la memoria elaborando i file. Prova con meno file o file più piccoli."),
    ]
    for keys, msg in rules:
        if any(k in text for k in keys):
            return msg
    return "La generazione si è interrotta per un errore non previsto. Riprova; se si ripete, scarica il log e invialo a Xeel."


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
        self._final_at: Dict[str, float] = {}  # quando è stato messo in memoria (per i completati senza riepilogo)
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

    def view(self, run: Dict, detail: bool = False) -> Dict:
        state = run.get("state", "queued")
        step = int(run.get("step") or 0)
        outputs = run.get("outputs") or {}
        view = {
            "job_id": run["job_id"],
            "project_code": run.get("project_code"),
            "state": state,
            "state_label": STATE_LABELS.get(state, state),
            "active": state in ACTIVE_STATES,
            "progress": 100 if state == "succeeded" else min(99, round(100 * sum(1 for n in VISIBLE_STEPS if n <= _shown_step(step)) / len(VISIBLE_STEPS))),
            "step": step,
            "total_steps": len(VISIBLE_STEPS),
            "step_label": STEP_NAMES[_shown_step(step) - 1] if step in HIDDEN_STEPS and state in ACTIVE_STATES else run.get("step_label"),
            "created_label": _fmt_dt(run.get("created_at")),
            "finished_label": _fmt_dt(run.get("finished_at")),
            "start_label": _fmt_date(run.get("project_start_date")) if run.get("project_start_date") else "",
            "created_by": run.get("created_by"),
            "name": run.get("name") or "",
            "error": run.get("error"),
            "error_hint": _explain_failure(run) if state == "failed" else None,
            "sow_files": run.get("sow_files") or [],
            "has_mdr": bool(outputs.get("mdr")),
            "summary": _public_summary(run.get("summary")),
        }
        if detail and (step or state == "succeeded"):
            view["steps"] = _timeline(run)
        if detail and state not in ACTIVE_STATES:  # dimensioni dei SoW: servono a riusarli in una nuova generazione
            view["sow_sizes"] = {n: self.b.size(f"jobs/{run['job_id']}/input/{n}") or 0 for n in (run.get("sow_files") or [])[:20]}
        if detail and state == "succeeded":
            view["digest"] = _public_digest(run.get("digest"))
        return view

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
        if cached is not None and cached.get("state") == "succeeded" and not cached.get("digest") \
                and time.monotonic() - self._final_at.get(job_id, 0) > 30:
            cached = None  # il riepilogo può essere stato aggiunto dopo (recupero): si rilegge ogni 30 secondi
        if cached is not None:
            return dict(cached)
        run = self.b.read_json(status_key(job_id))
        if not run:
            return None
        run = self._refresh(run)
        if run.get("state") not in ACTIVE_STATES:
            self._final[job_id] = dict(run)
            self._final_at[job_id] = time.monotonic()
        elif self.b.exists(f"jobs/{job_id}/cancel"):
            # Annullata dall'utente: il motore impiega qualche secondo a fermarsi e nel frattempo può riscrivere
            # "in corso". Per chi guarda la generazione è già annullata (non si salva: lo stato finale lo scrive il motore).
            run = dict(run, state="cancelled", step_label="Annullato")
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

    @staticmethod
    def _clean_name(name: str) -> str:
        """Nome facoltativo scelto dall'utente: una riga, senza caratteri di controllo, al massimo 60 caratteri."""
        return " ".join("".join(ch for ch in str(name or "") if ch.isprintable()).split())[:60]

    def prepare_upload(self, project_code: str, start_date: str, files: List[Tuple[str, int]], user: str = "",
                       name: str = "", reuse_job: str = "", reuse_names: Optional[List[str]] = None) -> Dict:
        """Prepara una nuova generazione. Oltre ai PDF da caricare si possono riusare quelli di una generazione
        precedente (reuse_job + nomi): si copiano nel bucket solo all'avvio, senza passare dal browser."""
        project_code, start_date = project_code.strip(), start_date.strip()
        reused: List[Tuple[str, int]] = []
        if reuse_names:
            if not valid_job_id(reuse_job):
                raise ValueError("Generazione di origine non trovata.")
            source = self.get(reuse_job)
            for rn in dict.fromkeys(reuse_names):
                if rn not in (source.get("sow_files") or []):
                    raise ValueError(f"'{rn}' non è tra i file della generazione di origine.")
                size = self.b.size(f"jobs/{reuse_job}/input/{rn}")
                if size is None:
                    raise ValueError(f"'{rn}' non è più disponibile: caricalo di nuovo.")
                reused.append((rn, size))
        error = self._check(project_code, start_date, list(files) + reused)
        if error:
            raise ValueError(error)
        job_id = new_job_id(project_code)
        names = self._safe_names([n for n, _ in files] + [n for n, _ in reused])
        uploads, pending_files = [], []
        for safe, (original, size) in zip(names[:len(files)], files):  # (non riusare "name": è il nome scelto dall'utente)
            uploads.append({"name": safe, "original": original, "size": size,
                            "url": self.b.signed_put_url(f"jobs/{job_id}/input/{safe}", "application/pdf")})
            pending_files.append({"name": safe, "size": size})
        for safe, (original, size) in zip(names[len(files):], reused):
            pending_files.append({"name": safe, "size": size, "from_job": reuse_job, "from_name": original})
        self.b.write_json(f"jobs/{job_id}/pending.json", {
            "project_code": project_code, "start_date": start_date, "user": user, "created_at": _now_iso(),
            "name": self._clean_name(name), "files": pending_files,
        })
        return {"job_id": job_id, "uploads": uploads}

    def start(self, job_id: str, user: str = "") -> Dict:
        if not valid_job_id(job_id):
            raise ValueError("Generazione non trovata.")
        pending = self.b.read_json(f"jobs/{job_id}/pending.json")
        if not pending:
            raise ValueError("Caricamento non trovato o già avviato.")
        names = [f["name"] for f in pending.get("files", [])]
        for f in pending.get("files", []):  # file riusati da una generazione precedente: si copiano ora
            if f.get("from_job"):
                src = f"jobs/{f['from_job']}/input/{f.get('from_name') or f['name']}"
                if not valid_job_id(f["from_job"]) or not self.b.exists(src):
                    raise ValueError(f"'{f['name']}' non è più disponibile: caricalo di nuovo.")
                self.b.copy(src, f"jobs/{job_id}/input/{f['name']}")
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
                            user or pending.get("user", ""), name=pending.get("name") or "")
        self.b.delete_key(f"jobs/{job_id}/pending.json")
        return view

    def _launch(self, job_id: str, project_code: str, start_date: str, names: List[str], user: str,
                retry_of: Optional[str] = None, name: str = "") -> Dict:
        """Scrive lo stato iniziale e avvia il motore (i SoW sono già nel bucket)."""
        status = {
            "job_id": job_id, "project_code": project_code,
            "project_start_date": start_date or None,
            "state": "queued", "step": 0, "total_steps": TOTAL_STEPS, "step_label": "In coda",
            "created_at": _now_iso(), "updated_at": _now_iso(),
            "created_by": user, "sow_files": sorted(names),
        }
        if name:
            status["name"] = name
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
                            names, user, retry_of=job_id, name=run.get("name") or "")

    def rename(self, job_id: str, name: str) -> Dict:
        """Cambia il nome di una generazione conclusa (durante l'esecuzione il motore riscrive lo stato)."""
        run = self.get(job_id)
        if run.get("state") in ACTIVE_STATES:
            raise ValueError("La generazione è in corso: rinominala quando è finita.")
        clean = self._clean_name(name)
        if clean:
            run["name"] = clean
        else:
            run.pop("name", None)
        self.b.write_json(status_key(job_id), run)
        self._final.pop(job_id, None)
        return self.view(run)

    def existing(self, project_code: str) -> Dict:
        """Generazioni già presenti per lo stesso codice progetto (escluse annullate e fallite).
        Si scorrono le più recenti a blocchi: tante prove annullate non devono nascondere quella completata."""
        code = (project_code or "").strip().lower()
        ids = [i for i in self.b.list_job_ids() if "-".join(i.split("-")[2:-1]).lower() == code] if code else []
        out: List[Dict] = []
        with ThreadPoolExecutor(max_workers=8) as pool:
            for start in range(0, min(len(ids), 80), 8):
                for run in pool.map(self._load, ids[start:start + 8]):
                    if run and run.get("state") in ACTIVE_STATES | {"succeeded"}:
                        v = self.view(run)
                        out.append({"job_id": v["job_id"], "state": v["state"], "state_label": v["state_label"],
                                    "created_label": v["created_label"], "name": v["name"]})
                if len(out) >= 3:
                    break
        return {"esistenti": out[:3]}

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


class _Unavailable:
    """Al posto del motore quando manca la configurazione: ogni operazione spiega perché non è disponibile."""

    def __init__(self, reason: str) -> None:
        self.reason = reason

    def __getattr__(self, name):
        def fail(*args, **kwargs):
            raise ValueError(self.reason)
        return fail


def _backend_from_env():
    names = ("MDR_GCP_KEY_JSON", "MDR_GCP_PROJECT", "MDR_GCP_REGION", "MDR_BUCKET", "MDR_JOB_NAME")
    missing = [n for n in names if not os.environ.get(n, "").strip()]
    if missing:
        raise RuntimeError("Il generatore non è configurato: su Render mancano le variabili " + ", ".join(missing) + ".")
    return GcsBackend(
        os.environ["MDR_GCP_KEY_JSON"], os.environ["MDR_GCP_PROJECT"].strip(), os.environ["MDR_GCP_REGION"].strip(),
        os.environ["MDR_BUCKET"].strip(), os.environ["MDR_JOB_NAME"].strip(),
    )


def register_mdr_generator(mcp: FastMCP, backend=None) -> None:
    if backend is None:
        try:
            backend = _backend_from_env()
        except Exception as exc:  # noqa: BLE001
            reason = str(exc) if isinstance(exc, RuntimeError) else f"Collegamento a Google Cloud non riuscito: {exc}"
            logger.error("Generatore MDR non operativo: %s", reason)
            backend = _Unavailable(reason)  # il pannello si apre comunque e mostra il motivo
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
            "avvia una generazione, vede l'avanzamento e scarica l'MDR e il log. "
            "Usalo SEMPRE quando l'utente vuole generare un MDR, caricare uno Scope of Work, "
            "oppure vedere, controllare o riprendere le generazioni (es. 'a che punto sono le mie generazioni', "
            "'stato delle generazioni', 'mostrami le generazioni', 'scarica l'MDR generato'). "
            "Dopo la chiamata di' solo che il pannello è aperto: è il pannello a guidare l'utente."
        ),
        annotations=read_hints,
        meta={"ui": {"resourceUri": GEN_URI, "visibility": ["model", "app"]}},
    )
    async def apri_generatore_mdr() -> dict:
        try:
            runs, more = await run(svc.list_runs, 10)  # l'elenco arriva col pannello: si vede subito
        except ToolError as exc:
            return {"messaggio": "Pannello del Generatore MDR aperto, ma il generatore non è disponibile.",
                    "errore": str(exc), "attive": 0, "runs": [], "altri": False,
                    "max_file_mb": svc.max_file_mb, "max_upload_mb": svc.max_total_mb}
        return {"messaggio": "Pannello del Generatore MDR aperto.",
                "attive": sum(1 for r in runs if r["active"]), "runs": runs, "altri": more,
                "max_file_mb": svc.max_file_mb, "max_upload_mb": svc.max_total_mb}

    @mcp.tool(
        name="elenco_generazioni_mdr", title="Elenco generazioni MDR",
        description=("Elenco TESTUALE delle generazioni MDR (stato e avanzamento), solo per rispondere a una domanda "
                     "precisa in chat, ad esempio quante sono in corso. Se l'utente vuole vedere, controllare o gestire "
                     "le generazioni, usa invece apri_generatore_mdr. "
                     "Filtri facoltativi: progetto (parte del codice) e stato (active, succeeded, failed, cancelled)."),
        annotations=read_hints,
    )
    async def elenco_generazioni_mdr(progetto: str = "", stato: str = "", massimo: int = 10) -> dict:
        runs, more = await run(svc.list_runs, max(1, min(massimo, 50)), stato, progetto)
        return {"generazioni": runs, "ce_ne_sono_altre": more}

    @mcp.tool(name="mdr_elenco", title="Elenco generazioni", description="Usato solo dal pannello.",
              annotations=read_hints, meta=app_only)
    async def mdr_elenco(q: str = "", stato: str = "", limit: int = 10) -> dict:
        started = time.monotonic()
        runs, more = await run(svc.list_runs, max(1, min(int(limit), 50)),
                               stato if stato in ("", "active", "succeeded", "failed", "cancelled") else "", (q or "")[:40])
        return {"runs": runs, "altri": more, "ms": int((time.monotonic() - started) * 1000)}

    @mcp.tool(name="mdr_dettaglio", title="Dettaglio generazione", description="Usato solo dal pannello.",
              annotations=read_hints, meta=app_only)
    async def mdr_dettaglio(job_id: str) -> dict:
        return svc.view(await run(svc.get, job_id), detail=True)

    @mcp.tool(name="mdr_prepara_upload", title="Prepara caricamento", description="Usato solo dal pannello.",
              annotations=write_hints, meta=app_only)
    async def mdr_prepara_upload(project_code: str, start_date: str = "", files: Optional[List[dict]] = None,
                                 name: str = "", riuso_job: str = "", riuso_files: Optional[List[str]] = None) -> dict:
        parsed = [(str(f.get("name", "")), int(f.get("size") or 0)) for f in (files or [])]
        return await run(svc.prepare_upload, project_code, start_date, parsed, "gemini", name,
                         riuso_job, [str(n) for n in (riuso_files or [])][:20])

    @mcp.tool(name="mdr_esistenti", title="Generazioni dello stesso progetto", description="Usato solo dal pannello.",
              annotations=read_hints, meta=app_only)
    async def mdr_esistenti(project_code: str) -> dict:
        return await run(svc.existing, project_code[:40])

    @mcp.tool(name="mdr_rinomina", title="Rinomina generazione", description="Usato solo dal pannello.",
              annotations=write_hints, meta=app_only)
    async def mdr_rinomina(job_id: str, name: str = "") -> dict:
        return await run(svc.rename, job_id, name)

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
