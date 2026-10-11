"""Invocation-only recovery for missed GitHub schedules; no acquisition or SQL."""
import base64
from datetime import datetime, timedelta, timezone
import json
import re
from uuid import UUID, uuid4

import yaml

from ..remote.releases import github


WORKFLOW = "remote-data-update.yml"
CONCURRENCY_GROUP = "private-remote-source-account-owner"
TITLE_PREFIX = "KRX checkpoint continuation "
FLAGS = ("REMOTE_PIPELINE_ENABLED", "REMOTE_FREE_BUDGET_APPROVED",
         "REMOTE_IDENTITY_HANDOFF_READY", "REMOTE_DATA_PUBLIC_ALLOWED")
ACTIVE = {"queued", "in_progress", "waiting", "pending", "requested"}
CADENCE = timedelta(hours=2, minutes=15)


def _clock(value):
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        raise ValueError("remote_run_clock_unconfirmed") from None
    if result.tzinfo is None:
        raise ValueError("remote_run_clock_unconfirmed")
    return result.astimezone(timezone.utc)


def _request(value):
    try:
        if str(UUID(value)) != value:
            raise ValueError
    except (AttributeError, TypeError, ValueError):
        raise ValueError("canonical_dispatch_request_id_required") from None
    return value


def advance(state, view, dispatch, persist, *, now, request_id=None, budget_seconds=6000):
    """Reconcile durable intent, then admit at most one overdue continuation."""
    if now.tzinfo is None or type(budget_seconds) is not int or not 30 <= budget_seconds <= 6000:
        raise ValueError("aware_clock_and_bounded_batch_required")
    request_id = _request(request_id or str(uuid4()))
    now = now.astimezone(timezone.utc)
    state.update(checked_at=now.isoformat(), requires_user=False)

    def save(status, *, requires_user=False):
        state.update(status=status, requires_user=requires_user)
        persist(state)
        return state

    if view.get("enabled") is not True:
        return save("pipeline_disabled")
    if view.get("workflow_active") is not True:
        return save("workflow_inactive_requires_review", requires_user=True)
    runs = view.get("runs")
    if not isinstance(runs, list) or not runs:
        return save("queue_state_unconfirmed", requires_user=True)
    for run in runs:
        if (type(run.get("id")) is not int or run["id"] < 1 or
                run.get("status") not in ACTIVE | {"completed"}):
            raise ValueError("remote_run_identity_or_status_unconfirmed")
        if _clock(run.get("created_at")) > now + timedelta(minutes=1):
            raise ValueError("remote_run_clock_unconfirmed")
    runs = sorted(runs, key=lambda run: _clock(run["created_at"]), reverse=True)
    state["latest_run_id"] = runs[0]["id"]
    state["latest_run_created_at"] = runs[0]["created_at"]
    intent = state.get("intent")
    if intent:
        identity = _request(intent.get("request_id"))
        matches = [run for run in runs if run.get("display_title") == TITLE_PREFIX + identity]
        if len(matches) > 1:
            raise ValueError("duplicate_dispatch_receipts_require_review")
        if matches:
            intent.update(status="observed", observed_run_id=matches[0]["id"])
        elif intent.get("status") == "rejected":
            # Read access can succeed while dispatch permission remains denied.
            # Latch this permanent write failure across polls and restarts;
            # an operator must reconcile/reset it after correcting permission.
            return save("github_access_requires_action", requires_user=True)
        elif intent.get("status") not in {"observed", "rejected"}:
            elapsed = now - _clock(intent.get("created_at"))
            uncertain = elapsed > timedelta(minutes=15)
            return save("launch_uncertain" if uncertain else "waiting_for_dispatch_receipt",
                        requires_user=uncertain)
    if any(run["status"] in ACTIVE for run in runs):
        return save("waiting_for_existing_remote_run")
    latest = runs[0]
    # No guessing about schema/authentication/program failures or cancellations.
    if latest.get("conclusion") not in {"success", "timed_out"}:
        return save("remote_failure_requires_review", requires_user=True)
    if now - _clock(latest["created_at"]) < CADENCE:
        state["next_eligible_at"] = (_clock(latest["created_at"]) + CADENCE).isoformat()
        return save("waiting_for_schedule_window")
    if intent:
        state["previous_intent"] = dict(intent)
    state["intent"] = {"request_id": request_id, "created_at": now.isoformat(),
                       "status": "prepared", "budget_seconds": budget_seconds,
                       "prior_run_id": latest["id"]}
    # Persist before the network write. A crash between save and request is
    # intentionally uncertain and must never cause an automatic second request.
    save("dispatch_prepared")
    try:
        dispatch(request_id, budget_seconds)
    except Exception as exc:
        rejected = isinstance(exc, ValueError) and str(exc) == "github_access_requires_action"
        state["intent"].update(status="rejected" if rejected else "uncertain",
                               error_type=type(exc).__name__)
        return save("github_access_requires_action" if rejected else "waiting_for_dispatch_receipt",
                    requires_user=rejected)
    state["intent"]["status"] = "accepted"
    return save("waiting_for_dispatch_receipt")


class GitHubSchedule:
    """Bounded official CLI calls; credentials stay in its existing directory."""
    def __init__(self, gh, repository):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ValueError("explicit_repository_required")
        self.gh, self.repository = gh, repository
        self.branch = None

    def api(self, suffix):
        return json.loads(github(self.gh, ["api", f"repos/{self.repository}/{suffix}"], timeout=30))

    def snapshot(self):
        repo = self.api("")
        self.branch = repo["default_branch"]
        permissions = self.api("actions/permissions")
        variables = self.api("actions/variables?per_page=100")
        flags = {v["name"]: v["value"] for v in variables["variables"] if v["name"] in FLAGS}
        enabled = (not repo.get("disabled") and not repo.get("archived") and not repo.get("fork")
                   and permissions.get("enabled") is True and all(flags.get(k) == "true" for k in FLAGS))
        workflow = self.api("actions/workflows/" + WORKFLOW)
        value = self.api("contents/.github/workflows/" + WORKFLOW)
        # BaseLoader does not construct objects and preserves the literal 'on'.
        document = yaml.load(base64.b64decode(value["content"]).decode("utf-8"), Loader=yaml.BaseLoader)
        fence = document.get("concurrency", {})
        dispatch_inputs = document.get("on", {}).get("workflow_dispatch", {}).get("inputs", {})
        if (fence.get("group") != CONCURRENCY_GROUP or fence.get("cancel-in-progress") != "false"
                or "continuation_request_id" not in dispatch_inputs
                or document.get("run-name") != TITLE_PREFIX + "${{ inputs.continuation_request_id || github.run_number }}"):
            raise ValueError("existing_workflow_fence_or_dispatch_receipt_unconfirmed")
        recent = self.api("actions/workflows/" + WORKFLOW + "/runs?per_page=100")["workflow_runs"]
        by_id = {run["id"]: run for run in recent}
        # Also inspect active lanes so a very old queued/blocked run cannot hide
        # outside the recent-history page and admit an unnecessary continuation.
        for status in sorted(ACTIVE):
            for run in self.api("actions/workflows/" + WORKFLOW + "/runs?per_page=100&status=" + status)["workflow_runs"]:
                by_id[run["id"]] = run
        return {"enabled": enabled, "workflow_active": workflow.get("state") == "active",
                "runs": [{k: run.get(k) for k in ("id", "status", "conclusion", "created_at", "display_title")}
                         for run in by_id.values()]}

    def dispatch(self, request_id, budget_seconds):
        _request(request_id)
        if not self.branch:
            raise ValueError("verified_default_branch_required")
        github(self.gh, ["workflow", "run", WORKFLOW, "--repo", self.repository,
                        "--ref", self.branch, "--raw-field", "budget_seconds=" + str(budget_seconds),
                        "--raw-field", "continuation_request_id=" + request_id], timeout=30)
