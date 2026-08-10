"""FastAPI HTTP surface for the interview-focused CapyReview runtime."""
import hashlib
import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, Optional

from fastapi import FastAPI, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from .config import Settings
from .github import verify_signature
from .report import to_markdown

if TYPE_CHECKING:
    from .service import ReviewService


WEB_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "web"))
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
EVALUATION_OUTPUT_ROOT = os.path.join(PROJECT_ROOT, "output", "llm-evaluation")
EVALUATION_DATASET_PATH = os.path.join(
    PROJECT_ROOT, "evaluation_data", "pr_diff_100.jsonl"
)


class ReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository: str = Field(min_length=1, max_length=250)
    diff: str = Field(min_length=1)
    pull_request: Optional[int] = Field(default=None, ge=1)


class FeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: str
    finding: Optional[Dict[str, Any]] = None
    note: str = ""


class PromptProposalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skill_name: str = "llm-review"
    prompt: str
    regression_score: Optional[float] = None


class EvolutionAutoRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skill_name: str = "llm-review"


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, PermissionError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, ValueError):
        message = str(exc)
        if "DeepSeek" in message and (
            "not configured" in message or "DEEPSEEK_API_KEY" in message
        ):
            return HTTPException(status_code=503, detail=message)
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=500, detail="operation failed")


def _controlled_dataset_summary() -> Dict[str, Any]:
    """Summarise the frozen JSONL dataset without importing evaluation code."""
    cases = []
    try:
        with open(EVALUATION_DATASET_PATH, encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    value = json.loads(line)
                    if isinstance(value, dict):
                        cases.append(value)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        cases = []
    risk_cases = sum(bool(item.get("expected_findings")) for item in cases)
    repositories = {str(item.get("repository", "")) for item in cases}
    repositories.discard("")
    return {
        "kind": "synthetic-controlled",
        "cases": len(cases),
        "risk_cases": risk_cases,
        "clean_cases": len(cases) - risk_cases,
        "repositories": len(repositories),
    }


def _evaluation_report_summary(
    report: Dict[str, Any], report_id: str,
) -> Optional[Dict[str, Any]]:
    """Return a small UI-safe summary only when a report is complete."""
    if int(report.get("schema_version", 0) or 0) < 2:
        return None
    evaluation = report.get("evaluation")
    result = report.get("result")
    if not isinstance(evaluation, dict) or not isinstance(result, dict):
        return None
    dataset = result.get("dataset")
    metrics = result.get("metrics")
    if not isinstance(dataset, dict) or not isinstance(metrics, dict):
        return None
    expected = int(evaluation.get("dataset_cases", 0) or 0)
    scored = int(metrics.get("cases", 0) or 0)
    dataset_cases = int(dataset.get("cases", 0) or 0)
    if expected < 1 or scored != expected or dataset_cases != expected:
        return None
    return {
        "status": "complete",
        "schema_version": report.get("schema_version"),
        "report_id": report_id,
        "evaluation": evaluation,
        "dataset": dataset,
        "result": metrics,
        "limitations": report.get("limitations", []),
    }


def _latest_evaluation_summary() -> Dict[str, Any]:
    candidates = []
    try:
        with os.scandir(EVALUATION_OUTPUT_ROOT) as entries:
            for entry in entries:
                if not entry.is_dir():
                    continue
                path = os.path.join(entry.path, "llm-evaluation-report.json")
                if os.path.isfile(path):
                    candidates.append((os.path.getmtime(path), path, entry.name))
    except OSError:
        candidates = []
    for _modified_at, path, report_id in sorted(candidates, reverse=True):
        try:
            with open(path, encoding="utf-8") as handle:
                report = json.load(handle)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(report, dict):
            summary = _evaluation_report_summary(report, report_id)
            if summary is not None:
                return summary
    return {
        "status": "not_run",
        "dataset": _controlled_dataset_summary(),
        "result": {},
        "limitations": ["尚未生成完整的 LLM 评测报告。"],
    }


def create_app(
    settings: Optional[Settings] = None,
    service: Optional["ReviewService"] = None,
    evaluation_provider=None,
) -> FastAPI:
    resolved_settings = settings or Settings.from_env()
    owns_service = service is None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if app.state.service is None:
            from .service import ReviewService

            app.state.service = ReviewService(resolved_settings)
        try:
            yield
        finally:
            if owns_service and app.state.service is not None:
                app.state.service.close()

    application = FastAPI(
        title="CapyReview",
        version="1.0.0",
        description="Risk-routed pull-request review Agent Runtime Harness.",
        lifespan=lifespan,
    )
    application.state.settings = resolved_settings
    application.state.service = service
    application.state.evaluation_provider = evaluation_provider
    application.mount("/assets", StaticFiles(directory=WEB_ROOT), name="assets")

    def current_service(request: Request):
        value = request.app.state.service
        if value is None:
            raise HTTPException(status_code=503, detail="service is starting")
        return value

    def llm_status(request: Request) -> Dict[str, Any]:
        """Describe configuration without forcing the lazy LLM runtime to start."""
        configured = request.app.state.settings
        api_key = str(getattr(configured, "deepseek_api_key", "")).strip()
        model = str(
            getattr(
                configured,
                "deepseek_model",
                getattr(configured, "llm_model", "deepseek-chat"),
            )
        ).strip() or "deepseek-chat"
        return {
            "enabled": bool(api_key),
            "provider": "deepseek" if api_key else "",
            "model": model,
        }

    def runtime_identity(active) -> Dict[str, str]:
        reviewer = getattr(active, "reviewer", None)
        harness = getattr(active, "harness", None)
        return {
            "reviewer": getattr(reviewer, "name", "not-initialized"),
            "runtime": getattr(harness, "name", "capyreview-runtime"),
        }

    @application.exception_handler(ValueError)
    async def value_error_handler(_request: Request, exc: ValueError):
        error = _http_error(exc)
        return JSONResponse(
            status_code=error.status_code, content={"detail": error.detail}
        )

    @application.exception_handler(PermissionError)
    async def permission_error_handler(_request: Request, exc: PermissionError):
        return JSONResponse(status_code=403, content={"detail": str(exc)})

    @application.get("/", include_in_schema=False)
    def console():
        return FileResponse(os.path.join(WEB_ROOT, "index.html"))

    @application.get("/health")
    def health(request: Request):
        active = current_service(request)
        identity = runtime_identity(active)
        llm = llm_status(request)
        return {
            "status": "ok",
            "reviewer": identity["reviewer"],
            "runtime": identity["runtime"],
            "database": "postgresql",
            "queue": active.queue.backend,
            "llm_provider": llm["provider"],
            "llm_model": llm["model"],
        }

    @application.get("/api/dashboard")
    def dashboard(request: Request):
        active = current_service(request)
        identity = runtime_identity(active)
        return {
            "stats": active.store.dashboard_stats(),
            "tasks": active.store.list_tasks(10),
            "queue": active.queue.backend,
            "orchestrator": identity["reviewer"],
            "llm": llm_status(request),
        }

    @application.get("/api/tasks")
    def tasks(request: Request, limit: int = Query(default=50, ge=1, le=200)):
        active = current_service(request)
        return {"tasks": active.store.list_tasks(limit)}

    @application.get("/api/evaluation")
    def evaluation(request: Request):
        provider = request.app.state.evaluation_provider
        return provider() if provider is not None else _latest_evaluation_summary()

    @application.post("/v1/reviews", status_code=status.HTTP_201_CREATED)
    def create_review(
        payload: ReviewRequest,
        request: Request,
        async_review: bool = Query(default=False, alias="async"),
    ):
        active = current_service(request)
        if len(payload.diff.encode("utf-8")) > request.app.state.settings.max_diff_bytes:
            raise HTTPException(status_code=413, detail="diff exceeds maximum size")
        try:
            if async_review:
                result = active.enqueue_review(
                    payload.repository, payload.diff, payload.pull_request,
                )
                return JSONResponse(status_code=202, content=result)
            return active.create_review(
                payload.repository, payload.diff, payload.pull_request,
            )
        except Exception as exc:
            raise _http_error(exc) from exc

    @application.get("/v1/tasks/{task_id}")
    def task(task_id: str, request: Request):
        value = current_service(request).store.get(task_id)
        if not value:
            raise HTTPException(status_code=404, detail="task not found")
        return value

    @application.get("/v1/tasks/{task_id}/report", response_class=PlainTextResponse)
    def task_report(task_id: str, request: Request):
        value = current_service(request).store.get(task_id)
        if not value or not value.get("report"):
            raise HTTPException(status_code=404, detail="task or report not found")
        return PlainTextResponse(
            to_markdown(value["report"]), media_type="text/markdown; charset=utf-8"
        )

    @application.get("/v1/tasks/{task_id}/feedback")
    def task_feedback(task_id: str, request: Request):
        active = current_service(request)
        if not active.store.get(task_id):
            raise HTTPException(status_code=404, detail="task not found")
        return {
            "cases": active.store.list_task_failure_cases(task_id)
        }

    @application.post(
        "/v1/tasks/{task_id}/feedback", status_code=status.HTTP_201_CREATED
    )
    def record_feedback(task_id: str, payload: FeedbackRequest, request: Request):
        try:
            return current_service(request).record_feedback(
                task_id, payload.category, payload.finding, payload.note
            )
        except Exception as exc:
            raise _http_error(exc) from exc

    @application.post(
        "/v1/tasks/{task_id}/cancel", status_code=status.HTTP_202_ACCEPTED
    )
    def cancel_task(task_id: str, request: Request):
        ok = current_service(request).cancel_task(task_id)
        if not ok:
            raise HTTPException(status_code=404, detail="task not found")
        return {"cancel_requested": True}

    @application.post(
        "/v1/tasks/{task_id}/resume", status_code=status.HTTP_202_ACCEPTED
    )
    def resume_task(task_id: str, request: Request):
        try:
            return current_service(request).resume_task(task_id)
        except Exception as exc:
            raise _http_error(exc) from exc

    @application.post("/webhooks/github", status_code=status.HTTP_202_ACCEPTED)
    async def github_webhook(request: Request):
        if request.headers.get("X-GitHub-Event", "") != "pull_request":
            return {"ignored": True, "reason": "unsupported GitHub event"}
        configured = request.app.state.settings
        if not configured.github_webhook_secret:
            raise HTTPException(
                status_code=503, detail="GitHub webhook secret is not configured"
            )
        body = await request.body()
        if not body or len(body) > configured.max_diff_bytes + 256 * 1024:
            raise HTTPException(status_code=413, detail="webhook body is empty or too large")
        if not verify_signature(
            configured.github_webhook_secret,
            body,
            request.headers.get("X-Hub-Signature-256", ""),
        ):
            raise HTTPException(status_code=401, detail="invalid webhook signature")
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=400, detail="invalid JSON webhook body") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="webhook JSON root must be an object")
        updated_at = (payload.get("pull_request") or {}).get("updated_at")
        if updated_at:
            try:
                event_time = datetime.fromisoformat(str(updated_at).replace("Z", "+00:00"))
            except ValueError as exc:
                raise HTTPException(
                    status_code=400, detail="invalid pull_request.updated_at"
                ) from exc
            age = abs((datetime.now(timezone.utc) - event_time).total_seconds())
            if age > configured.webhook_max_age_seconds:
                raise HTTPException(status_code=409, detail="webhook is outside the replay window")
        delivery_id = request.headers.get("X-GitHub-Delivery", "")
        digest = hashlib.sha256(body).hexdigest()
        try:
            return current_service(request).handle_github_pull_request(
                payload, delivery_id, digest
            )
        except Exception as exc:
            raise _http_error(exc) from exc

    @application.get("/v1/evolution/status")
    def evolution_status(request: Request):
        active = current_service(request)
        value = active.evolution.status()
        llm = llm_status(request)
        value["provider"] = llm["provider"]
        value["model"] = llm["model"]
        return value

    @application.get("/v1/evolution/runs")
    def evolution_runs(request: Request, limit: int = Query(default=50, ge=1, le=200)):
        return {
            "runs": current_service(request).store.list_evolution_runs(limit)
        }

    @application.post(
        "/v1/evolution/auto", status_code=status.HTTP_201_CREATED
    )
    def auto_evolution(payload: EvolutionAutoRequest, request: Request):
        active = current_service(request)
        return active.evolution.auto_propose(payload.skill_name)

    @application.post(
        "/v1/evolution/propose", status_code=status.HTTP_201_CREATED
    )
    def propose_evolution(payload: PromptProposalRequest, request: Request):
        active = current_service(request)
        return active.evolution.propose(
            payload.skill_name, payload.prompt, payload.regression_score
        )

    @application.post("/v1/skills/{skill_name}/versions/{version}/activate")
    def activate_prompt_version(skill_name: str, version: int, request: Request):
        active = current_service(request)
        ok = active.evolution.rollback(skill_name, version)
        if not ok:
            raise HTTPException(status_code=404, detail="prompt version not found")
        return {"activated": True}

    @application.get("/v1/skills/{skill_name}/versions")
    def prompt_versions(skill_name: str, request: Request):
        return {
            "versions": current_service(request).store.list_skill_versions(skill_name)
        }

    return application


app = create_app()


def run() -> None:
    """Run the ASGI application with the configured Uvicorn server."""
    import uvicorn

    settings = Settings.from_env()
    uvicorn.run(create_app(settings=settings), host=settings.host, port=settings.port)
