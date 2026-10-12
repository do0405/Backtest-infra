"""Invocation-only recovery for missed GitHub schedules; no acquisition or SQL."""
import base64
from datetime import datetime, timedelta, timezone
from hashlib import sha256
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
REPAIR_PATHS = frozenset({
    "src/backtest_data/remote/codec.py", "src/backtest_data/remote/bundle.py",
    "src/backtest_data/remote/folder.py", "src/backtest_data/remote/worker.py",
    "src/backtest_data/remote/source_handlers.py",
    "src/backtest_data/prices/krx_quote_collection.py",
    "src/backtest_data/ingestion/http.py", "scripts/remote_release_cycle.py",
    ".github/workflows/remote-data-update.yml",
})


def review_digest(proof):
    """Canonical digest of an operator-reviewed, public evidence receipt."""
    return sha256(json.dumps(proof, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def valid_review(proof):
    """Bounded receipt schema; arbitrary cancellations never become retryable."""
    if not isinstance(proof, dict) or type(proof.get("version")) is not int or proof["version"] != 1:
        return False
    if (proof.get("workflow") != WORKFLOW or
            proof.get("failure_reason") != "verified_job_timeout_before_release" or
            proof.get("failed_conclusion") not in {"cancelled", "failure"} or
            not isinstance(proof.get("repository"), str) or
            not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", proof["repository"]) or
            not isinstance(proof.get("owner_id"), str) or
            not re.fullmatch(r"[a-z0-9-]{1,60}", proof["owner_id"])):
        return False
    for field in ("handoff_id", "manifest_id"):
        try:
            _request(proof.get(field))
        except ValueError:
            return False
    for field in ("failed_run_id", "failed_run_attempt"):
        if type(proof.get(field)) is not int or proof[field] < 1:
            return False
    for field, length in (("failed_head_sha", 40), ("repair_sha", 40),
                          ("continuation_sha", 40), ("cause_proof_sha256", 64)):
        if not isinstance(proof.get(field), str) or not re.fullmatch(r"[0-9a-f]{" + str(length) + "}", proof[field]):
            return False
    expected_ref = (f"krx-reviewed-{proof['failed_run_id']}-a{proof['failed_run_attempt']}-"
                    + proof["continuation_sha"][:12])
    if proof.get("continuation_ref") != expected_ref:
        return False
    files = proof.get("repair_files")
    if not isinstance(files, dict) or not REPAIR_PATHS <= files.keys() or len(files) > 100:
        return False
    for path, digest in files.items():
        if (not isinstance(path, str) or not re.fullmatch(r"[A-Za-z0-9_.\-/]{1,240}", path)
                or path.startswith("/") or any(part in {"", ".", ".."} for part in path.split("/"))
                or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{40}", digest)):
            return False
    return True


def _review_for_run(view, latest):
    proof = view.get("reviewed_failure")
    if not valid_review(proof) or view.get("cause_proof_verified") is not True:
        return None
    if (any(proof[field] != view.get(field) for field in ("repository", "workflow")) or
            any(proof[field] != view.get("ownership_binding", {}).get(field)
                for field in ("owner_id", "handoff_id", "manifest_id")) or
            any(proof[field] != latest.get(run_field) for field, run_field in
                (("failed_run_id", "id"), ("failed_run_attempt", "run_attempt"),
                 ("failed_head_sha", "head_sha"), ("failed_conclusion", "conclusion"))) or
            view.get("current_head_sha") != proof["continuation_sha"] or
            view.get("repair_ancestry_status") not in {"ahead", "identical"} or
            view.get("current_repair_blobs") != proof["repair_files"]):
        return None
    return review_digest(proof)


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
            if (intent.get("continuation_sha") is not None and
                    matches[0].get("head_sha") != intent["continuation_sha"]):
                return save("dispatch_code_identity_requires_review", requires_user=True)
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
    reviewed = None
    # No guessing about schema/authentication/program failures or cancellations.
    if latest.get("conclusion") not in {"success", "timed_out"}:
        reviewed = _review_for_run(view, latest)
        if reviewed is None or reviewed in state.get("reviewed_failure_uses", {}):
            return save("remote_failure_requires_review", requires_user=True)
    if now - _clock(latest["created_at"]) < CADENCE:
        state["next_eligible_at"] = (_clock(latest["created_at"]) + CADENCE).isoformat()
        return save("waiting_for_schedule_window")
    if intent:
        state["previous_intent"] = dict(intent)
    state["intent"] = {"request_id": request_id, "created_at": now.isoformat(),
                       "status": "prepared", "budget_seconds": budget_seconds,
                       "prior_run_id": latest["id"]}
    if reviewed:
        proof = view["reviewed_failure"]
        state["intent"].update(reviewed_failure_sha256=reviewed,
                               reviewed_failed_run_id=latest["id"],
                               continuation_ref=proof["continuation_ref"],
                               continuation_sha=proof["continuation_sha"])
        state.setdefault("reviewed_failure_uses", {})[reviewed] = request_id
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
        self.reviewed_ref = None

    def api(self, suffix):
        path = f"repos/{self.repository}" + ("/" + suffix if suffix else "")
        return json.loads(github(self.gh, ["api", path], timeout=30))

    @staticmethod
    def workflow_document(value):
        document = yaml.load(base64.b64decode(value["content"]).decode("utf-8"), Loader=yaml.BaseLoader)
        fence = document.get("concurrency", {})
        dispatch_inputs = document.get("on", {}).get("workflow_dispatch", {}).get("inputs", {})
        if (fence.get("group") != CONCURRENCY_GROUP or fence.get("cancel-in-progress") != "false"
                or "continuation_request_id" not in dispatch_inputs
                or document.get("run-name") != TITLE_PREFIX + "${{ inputs.continuation_request_id || github.run_number }}"):
            raise ValueError("existing_workflow_fence_or_dispatch_receipt_unconfirmed")
        return document

    def snapshot(self, *, reviewed_failure=None, ownership_binding=None, cause_proof_verified=False):
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
        self.workflow_document(value)
        recent = self.api("actions/workflows/" + WORKFLOW + "/runs?per_page=100")["workflow_runs"]
        by_id = {run["id"]: run for run in recent}
        # Also inspect active lanes so a very old queued/blocked run cannot hide
        # outside the recent-history page and admit an unnecessary continuation.
        for status in sorted(ACTIVE):
            for run in self.api("actions/workflows/" + WORKFLOW + "/runs?per_page=100&status=" + status)["workflow_runs"]:
                by_id[run["id"]] = run
        self.reviewed_ref = None
        result = {"enabled": enabled, "workflow_active": workflow.get("state") == "active",
                  "repository": self.repository, "workflow": WORKFLOW,
                  "ownership_binding": ownership_binding or {},
                  "runs": [{k: run.get(k) for k in ("id", "run_attempt", "head_sha", "status", "conclusion", "created_at", "display_title")}
                           for run in by_id.values()]}
        if reviewed_failure is not None:
            result.update(reviewed_failure=reviewed_failure, cause_proof_verified=cause_proof_verified)
            if valid_review(reviewed_failure):
                ref = self.api("git/ref/tags/" + reviewed_failure["continuation_ref"])
                target = ref.get("object", {})
                # A unique lightweight tag is created once by the reviewed
                # deployment, never changed by this invocation-only controller.
                if target.get("type") == "commit" and target.get("sha") == reviewed_failure["continuation_sha"]:
                    head = target["sha"]
                    result["current_head_sha"] = head
                    comparison = self.api("compare/" + reviewed_failure["repair_sha"] + "..." + head)
                    result["repair_ancestry_status"] = comparison.get("status")
                    tree = self.api("git/trees/" + head + "?recursive=1")
                    if tree.get("truncated") is not True:
                        result["current_repair_blobs"] = {
                            entry["path"]: entry["sha"] for entry in tree.get("tree", [])
                            if entry.get("type") == "blob" and entry.get("path") in reviewed_failure["repair_files"]}
                    tagged_workflow = self.api("contents/.github/workflows/" + WORKFLOW + "?ref=" + head)
                    tagged_document = self.workflow_document(tagged_workflow)
                    admission = tagged_document.get("jobs", {}).get("update", {}).get("if", "")
                    if not all(flag in admission for flag in FLAGS):
                        raise ValueError("tagged_workflow_ownership_gates_unconfirmed")
                    if by_id:
                        latest = max(by_id.values(), key=lambda run: _clock(run["created_at"]))
                        if _review_for_run(result, latest):
                            self.reviewed_ref = reviewed_failure["continuation_ref"]
        return result

    def dispatch(self, request_id, budget_seconds, *, ref=None):
        _request(request_id)
        if not self.branch:
            raise ValueError("verified_default_branch_required")
        selected_ref = ref or self.branch
        if selected_ref != self.branch and selected_ref != self.reviewed_ref:
            raise ValueError("verified_reviewed_dispatch_ref_required")
        github(self.gh, ["workflow", "run", WORKFLOW, "--repo", self.repository,
                        "--ref", selected_ref, "--raw-field", "budget_seconds=" + str(budget_seconds),
                        "--raw-field", "continuation_request_id=" + request_id], timeout=30)
