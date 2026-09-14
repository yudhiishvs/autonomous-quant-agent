"""Same-origin browser API for verified identity and owned strategy versions."""

import asyncio
import hmac
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

from adaptive_trader.public_product.approvals import RiskLimits
from adaptive_trader.public_product.strategies import StrategyDefinition
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError, ResponseValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

from aqa_public.approval_signing import ApprovalSigner
from aqa_public.approval_storage import ApprovalStore
from aqa_public.broker import AlpacaConnection, BrokerRevoked, BrokerUnavailable, PaperAccount
from aqa_public.broker_storage import BrokerStore, ConnectionConflict
from aqa_public.identity import IdentityProvider, IdentityUnavailable
from aqa_public.responses import (
    AccountsResponse,
    ApprovalResponse,
    ApprovalsResponse,
    DisconnectResponse,
    RefreshResponse,
    VersionResponse,
)
from aqa_public.settings import Settings
from aqa_public.storage import CustomerSession, Store, verifier


class VersionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    name: str = Field(min_length=1, max_length=80)
    definition: StrategyDefinition

    @field_validator("name")
    @classmethod
    def meaningful_name(cls, value: str) -> str:
        value = value.strip()
        if not value or any(ord(c) < 32 for c in value):
            raise ValueError("Name must contain visible text.")
        return value


class PaperConsent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    consent: Literal["paper_only"]


class DisconnectConsent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm: Literal["disconnect_without_cancelling_orders"]


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account_id: UUID
    version_id: UUID
    request_id: UUID
    limits: RiskLimits


class ApprovalConsent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm: Literal["approve_paper_strategy_with_reviewed_limits"]
    reviewed_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class RevokeConsent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm: Literal["revoke_without_cancelling_orders"]


def create_app(
    settings: Settings,
    *,
    store: Store | None = None,
    identity: IdentityProvider | None = None,
    broker: AlpacaConnection | None = None,
) -> FastAPI:
    database = store or Store(settings.database_url.reveal())
    provider = identity or IdentityProvider(settings)
    connections = BrokerStore(database)
    brokerage = broker or (
        AlpacaConnection(settings.broker, settings.origin + "/broker/alpaca/callback")
        if settings.broker
        else None
    )
    signer = (
        ApprovalSigner(settings.approval_signing_key) if settings.approval_signing_key else None
    )
    approvals = ApprovalStore(database, signer)
    secure = not settings.development
    session_cookie = "aqa_session" if settings.development else "__Host-aqa_session"
    browser_cookie = "aqa_login" if settings.development else "__Host-aqa_login"
    csrf_cookie = "aqa_csrf" if settings.development else "__Host-aqa_csrf"

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        database.readiness()
        yield
        database.engine.dispose()

    app = FastAPI(
        title="Paper workspace", version="1.0.0", lifespan=lifespan, docs_url=None, redoc_url=None
    )
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=[str(urlsplit(settings.origin).hostname)]
    )

    def cookie(
        response: Response, name: str, value: str, seconds: int, *, httponly: bool = True
    ) -> None:
        response.set_cookie(
            name, value, max_age=seconds, secure=secure, httponly=httponly, samesite="lax", path="/"
        )

    @app.middleware("http")
    async def browser_boundary(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            if request.headers.get("origin") != settings.origin:
                return JSONResponse({"detail": "Request origin rejected."}, status_code=403)
            # Enforce body size even when Transfer-Encoding is chunked.
            chunks = bytearray()
            try:
                async with asyncio.timeout(10):
                    async for chunk in request.stream():
                        chunks.extend(chunk)
                        if len(chunks) > 16_384:
                            return JSONResponse(
                                {"detail": "Request exceeds 16 KiB."}, status_code=413
                            )
            except TimeoutError:
                return JSONResponse({"detail": "Request body deadline exceeded."}, status_code=408)
            request._body = bytes(chunks)
        response = await call_next(request)
        response.headers.update(
            {
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
                "X-Frame-Options": "DENY",
                "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
            }
        )
        if secure:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, error: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            {"detail": "Invalid request. Check the documented field limits."}, status_code=422
        )

    @app.exception_handler(ResponseValidationError)
    async def invalid_response(request: Request, error: ResponseValidationError) -> JSONResponse:
        # Validation errors can include the rejected internal record. Never serialize/log it.
        return JSONResponse(
            {"detail": "The saved response could not be verified. Try again shortly."},
            status_code=503,
        )

    @app.exception_handler(IdentityUnavailable)
    async def identity_failure(request: Request, error: IdentityUnavailable) -> JSONResponse:
        return JSONResponse({"detail": str(error)}, status_code=503)

    def customer(request: Request) -> CustomerSession:
        token = request.cookies.get(session_cookie, "")
        if not token or len(token) > 128:
            raise HTTPException(401, "Sign in to continue.")
        session = database.session(token)
        if session is None:
            raise HTTPException(401, "Session expired. Sign in again.")
        if not database.rate_limit("api:" + session.owner, limit=120, window=60):
            raise HTTPException(429, "Request limit reached. Try again in a minute.")
        if not provider.active(session.encrypted_token):
            database.sign_out(token)
            raise HTTPException(401, "Session revoked. Sign in again.")
        if request.method not in {"GET", "HEAD"} and not hmac.compare_digest(
            verifier(request.headers.get("x-csrf-token", "")), session.csrf_hash
        ):
            raise HTTPException(403, "Request verification failed. Reload and try again.")
        return session

    @app.exception_handler(BrokerUnavailable)
    async def broker_failure(request: Request, error: BrokerUnavailable) -> JSONResponse:
        return JSONResponse({"detail": str(error)}, status_code=503)

    @app.exception_handler(ConnectionConflict)
    async def connection_conflict(request: Request, error: ConnectionConflict) -> JSONResponse:
        return JSONResponse({"detail": str(error)}, status_code=409)

    def configured_broker() -> AlpacaConnection:
        if brokerage is None:
            raise HTTPException(503, "Alpaca connections are not configured on this installation.")
        return brokerage

    @app.get("/api/v1/accounts", response_model=AccountsResponse)
    def accounts(session: Annotated[CustomerSession, Depends(customer)]) -> dict[str, Any]:
        return {
            "connection_available": brokerage is not None,
            "accounts": connections.accounts(session.owner),
        }

    @app.post("/api/v1/accounts/connect")
    def connect_account(
        body: PaperConsent, request: Request, session: Annotated[CustomerSession, Depends(customer)]
    ) -> dict[str, str]:
        active_broker = configured_broker()
        state = secrets.token_urlsafe(32)
        connections.begin(session.owner, request.cookies[session_cookie], state)
        return {"authorization_url": active_broker.authorization_url(state)}

    @app.get("/broker/alpaca/callback")
    def broker_callback(
        request: Request,
        session: Annotated[CustomerSession, Depends(customer)],
        state: str = "",
        code: str = "",
    ) -> Response:
        if not 1 <= len(state) <= 128:
            raise HTTPException(400, "Paper connection state was invalid. Start again.")
        active_broker = configured_broker()
        token = request.cookies[session_cookie]
        generation = connections.consume(session.owner, token, state)
        if not 1 <= len(code) <= 4096:
            raise HTTPException(
                400,
                "Paper connection was denied or invalid. Return to the workspace and start again.",
            )
        access = active_broker.exchange(code)
        account = active_broker.account(access)
        connections.connect(
            owner=session.owner,
            session=token,
            generation=generation,
            account=account,
            encrypted_token=active_broker.encrypt(
                owner=session.owner, account_id=account.id, access=access
            ),
        )
        return RedirectResponse("/", status_code=303)

    @app.post("/api/v1/accounts/{account_id}/disconnect", response_model=DisconnectResponse)
    def disconnect_account(
        account_id: UUID,
        body: DisconnectConsent,
        session: Annotated[CustomerSession, Depends(customer)],
    ) -> dict[str, Any]:
        if not connections.disconnect(session.owner, account_id):
            raise HTTPException(404, "Paper account not found.")
        return {
            "disconnected": True,
            "broker_revocation_confirmed": False,
            "orders_cancelled": False,
        }

    def verified_account(owner: str, account_id: UUID) -> tuple[dict[str, Any], PaperAccount]:
        active_broker = configured_broker()
        credential = connections.credential(owner, account_id)
        if credential is None:
            raise HTTPException(404, "Connected paper account not found.")
        try:
            access = active_broker.decrypt(
                owner=owner,
                account_id=credential["broker_id"],
                ciphertext=credential["encrypted_token"],
            )
            account = active_broker.account(access)
        except BrokerRevoked:
            connections.disconnect(
                owner,
                account_id,
                state="reconnect_required",
                expected_revision=credential["revision"],
            )
            raise
        if account.id != credential["broker_id"]:
            connections.disconnect(
                owner,
                account_id,
                state="reconnect_required",
                expected_revision=credential["revision"],
            )
            raise BrokerUnavailable(
                "Alpaca account identity changed. Reconnect your paper account."
            )
        return credential, account

    @app.post("/api/v1/accounts/{account_id}/refresh", response_model=RefreshResponse)
    def refresh_account(
        account_id: UUID, session: Annotated[CustomerSession, Depends(customer)]
    ) -> dict[str, Any]:
        credential, account = verified_account(session.owner, account_id)
        if not connections.refresh(session.owner, account_id, credential["revision"], account):
            raise ConnectionConflict(
                "This connection changed during refresh. Reload the account list."
            )
        return {"accounts": connections.accounts(session.owner)}

    def approval_result(owner: str, approval_id: UUID) -> dict[str, Any]:
        result = next((row for row in approvals.list(owner) if row["id"] == approval_id), None)
        if result is None:
            raise HTTPException(404, "Approval not found.")
        return result

    @app.get("/api/v1/approvals", response_model=ApprovalsResponse)
    def list_approvals(session: Annotated[CustomerSession, Depends(customer)]) -> dict[str, Any]:
        return {
            "approval_available": signer is not None and brokerage is not None,
            "approvals": approvals.list(session.owner),
        }

    @app.post("/api/v1/approvals", response_model=ApprovalResponse, status_code=201)
    def review_approval(
        body: ApprovalRequest,
        request: Request,
        session: Annotated[CustomerSession, Depends(customer)],
    ) -> dict[str, Any]:
        if signer is None:
            raise HTTPException(503, "Approval signing is not configured.")
        credential, account = verified_account(session.owner, body.account_id)
        try:
            approval_id = approvals.draft(
                owner=session.owner,
                session=request.cookies[session_cookie],
                account_id=body.account_id,
                version_id=body.version_id,
                limits=body.limits,
                request_id=body.request_id,
                verified=account,
                revision=credential["revision"],
            )
        except ValueError:
            raise HTTPException(
                422, "Strategy targets and risk limits must fit the verified paper account equity."
            ) from None
        return approval_result(session.owner, approval_id)

    @app.post("/api/v1/approvals/{approval_id}/confirm", response_model=ApprovalResponse)
    def confirm_approval(
        approval_id: UUID,
        body: ApprovalConsent,
        request: Request,
        session: Annotated[CustomerSession, Depends(customer)],
    ) -> dict[str, Any]:
        if signer is None:
            raise HTTPException(503, "Approval signing is not configured.")
        previous = approval_result(session.owner, approval_id)
        credential, account = verified_account(session.owner, previous["account_id"])
        try:
            approvals.confirm(
                owner=session.owner,
                session=request.cookies[session_cookie],
                approval_id=approval_id,
                reviewed_hash=body.reviewed_hash,
                verified=account,
                revision=credential["revision"],
            )
        except ValueError:
            raise HTTPException(
                422, "Paper account or approval validation failed. Refresh and review again."
            ) from None
        return approval_result(session.owner, approval_id)

    @app.post("/api/v1/approvals/{approval_id}/revoke", response_model=ApprovalResponse)
    def revoke_approval(
        approval_id: UUID,
        body: RevokeConsent,
        session: Annotated[CustomerSession, Depends(customer)],
    ) -> dict[str, Any]:
        approval_result(session.owner, approval_id)
        approvals.revoke(session.owner, approval_id)
        return approval_result(session.owner, approval_id)

    @app.get("/health/live")
    def live() -> dict[str, str]:
        return {"status": "alive"}

    @app.get("/health/ready")
    def ready() -> dict[str, str]:
        database.readiness()
        return {"status": "ready"}

    @app.get("/auth/login")
    def login(request: Request) -> Response:
        address = request.client.host if request.client else "unknown"
        if not database.rate_limit("login:" + address, limit=20, window=60):
            raise HTTPException(429, "Sign-in limit reached. Try again in a minute.")
        browser, state, nonce, code_verifier = (secrets.token_urlsafe(32) for _ in range(4))
        database.begin_login(
            browser, state, nonce, provider.cipher.encrypt(code_verifier.encode()).decode()
        )
        response = RedirectResponse(
            provider.authorization_url(state=state, nonce=nonce, verifier=code_verifier),
            status_code=303,
        )
        cookie(response, browser_cookie, browser, 300)
        return response

    @app.get("/auth/callback")
    def callback(request: Request, state: str = "", code: str = "") -> Response:
        if not 1 <= len(state) <= 128 or not 1 <= len(code) <= 4096:
            raise HTTPException(400, "Sign-in response rejected. Start again.")
        attempt = database.consume_login(request.cookies.get(browser_cookie, ""), state)
        if attempt is None:
            raise HTTPException(400, "Sign-in expired or already used. Start again.")
        nonce, encrypted_verifier = attempt
        result = provider.exchange(
            code=code,
            nonce=nonce,
            verifier=provider.cipher.decrypt(encrypted_verifier.encode()).decode(),
        )
        old_token = request.cookies.get(session_cookie)
        if old_token:
            database.sign_out(old_token)
        token, csrf = database.sign_in(
            settings.issuer,
            result.subject,
            result.email,
            result.encrypted_access_token,
            result.expires_at,
        )
        response = RedirectResponse("/", status_code=303)
        response.delete_cookie(
            browser_cookie, path="/", secure=secure, httponly=True, samesite="lax"
        )
        cookie(response, session_cookie, token, 900)
        # CSRF nonce is not an authentication credential; the session stays HttpOnly.
        cookie(response, csrf_cookie, csrf, 900, httponly=False)
        return response

    @app.post("/auth/logout")
    def logout(request: Request) -> Response:
        # Local revocation must work even when the provider is unavailable.
        token = request.cookies.get(session_cookie, "")
        session = database.session(token)
        if session and not hmac.compare_digest(
            verifier(request.headers.get("x-csrf-token", "")), session.csrf_hash
        ):
            raise HTTPException(403, "Request verification failed.")
        database.sign_out(token)
        provider_revoked = True
        if session:
            try:
                provider.revoke(session.encrypted_token)
            except IdentityUnavailable:
                provider_revoked = False
        response = JSONResponse(
            {"signed_out": True, "provider_revocation_confirmed": provider_revoked}
        )
        for name in (session_cookie, csrf_cookie):
            response.delete_cookie(
                name, path="/", secure=secure, httponly=name == session_cookie, samesite="lax"
            )
        return response

    @app.get("/api/v1/me")
    def me(session: Annotated[CustomerSession, Depends(customer)]) -> dict[str, Any]:
        return {
            "user_id": session.owner,
            "paper_only": True,
            "limits": {"strategy_versions": 100},
            "execution_available": False,
        }

    @app.get("/api/v1/strategy-versions", response_model=list[VersionResponse])
    def versions(session: Annotated[CustomerSession, Depends(customer)]) -> list[dict[str, Any]]:
        return database.versions(session.owner)

    @app.get("/api/v1/strategy-versions/{version_id}", response_model=VersionResponse)
    def version(
        version_id: UUID, session: Annotated[CustomerSession, Depends(customer)]
    ) -> dict[str, Any]:
        result = database.version(session.owner, version_id)
        if result is None:
            raise HTTPException(404, "Strategy version not found.")
        return result

    @app.post("/api/v1/strategy-versions", status_code=201, response_model=VersionResponse)
    def save_version(
        body: VersionRequest, session: Annotated[CustomerSession, Depends(customer)]
    ) -> dict[str, Any]:
        try:
            return database.save_version(
                session.owner, body.name, body.definition, request_id=body.request_id
            )
        except ValueError as error:
            raise HTTPException(409, str(error)) from None

    return app


def configured_app() -> FastAPI:
    return create_app(Settings.from_environment())
