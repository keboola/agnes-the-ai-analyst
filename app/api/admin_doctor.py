"""POST /api/admin/doctor/new-instance — the deployment-gate doctor.

Admin-only. Check logic lives in ``app/services/instance_doctor.py``; the
host-side siblings (COMPOSE_FILE↔instance.yaml consistency, TLS predicate
agreement) live in ``scripts/ops/post-deploy-smoke-test.sh``, which calls
this endpoint for the server-side half.
"""

from typing import Optional

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from app.auth.access import require_admin
from app.services.instance_doctor import run_new_instance_doctor

router = APIRouter(prefix="/api/admin/doctor", tags=["admin"])


class NewInstanceDoctorRequest(BaseModel):
    # When set, the email-delivery check sends a real test message to this
    # address through the same ``_send_mail`` path the login flows use —
    # the only honest answer to "can this instance send email", because the
    # send endpoints return 200 even when the relay drops the message.
    email_to: Optional[str] = None


@router.post("/new-instance")
async def doctor_new_instance(
    request: Request,
    body: Optional[NewInstanceDoctorRequest] = None,
    _user: dict = Depends(require_admin),
):
    """Run the new-instance deployment checks and return a verdict per check.

    Response: ``{status, checks: [{name, status, audience, detail}]}`` with
    ``status ∈ {ok, warning, error, info}`` per check (the ``agnes diagnose``
    vocabulary) and the headline aggregating the worst check. Checks:
    ``login-door``, ``email-delivery``, ``chat-grant``, ``agent-scope``,
    ``branding``. Optional body ``{"email_to": ...}`` makes the
    email-delivery check send a real test message.
    """
    email_to = body.email_to if body else None
    return await run_new_instance_doctor(request.app, email_to=email_to)
