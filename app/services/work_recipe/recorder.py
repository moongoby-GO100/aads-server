"""성공한 브라우저 조작을 승인 대기 WorkRecipe draft로 굳힌다."""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from app.services.work_recipe import approval_notifier, registration, store
from app.services.work_recipe.guard import INTERNAL_DOMAINS
from app.services.work_recipe.schema import RecipeInput, RecipeStep, WorkRecipe
from app.services.work_recipe.vault_reference import parse_scope, secret_fill_steps

_CREDENTIAL_HINT = re.compile(
    r"password|passwd|pwd|login|sign.?in|username|user.?id|email|otp|인증|비밀번호|아이디",
    re.IGNORECASE,
)


class WorkRecipeRecorder:
    """성공 경로를 수집하고 복원 가능한 상태와 승인 대기 레시피를 만든다."""

    def __init__(
        self,
        name: str,
        domain: str,
        tenant_id: Any = None,
        *,
        session_id: str = "",
        e2e_evidence: Mapping[str, Any] | None = None,
        verify: list[Mapping[str, Any]] | None = None,
        flow_metadata: Mapping[str, Any] | None = None,
        credential_bindings_required: bool = False,
    ) -> None:
        self.name = str(name or "").strip()
        self.domain = store.normalize_domain(domain)
        self.tenant_id = tenant_id
        self.session_id = str(session_id or "").strip()
        self.e2e_evidence = dict(e2e_evidence or {})
        self.verify = [dict(item) for item in verify or []]
        self.flow_metadata = dict(flow_metadata or {})
        self.credential_bindings_required = credential_bindings_required
        if not self.name or not self.domain:
            raise ValueError("레시피 name과 domain이 필요합니다")
        self.steps: list[RecipeStep] = []
        self.inputs: list[RecipeInput] = []
        self._finished = False
        if self.flow_metadata.get("credential_scope"):
            self.set_credential_scope(self.flow_metadata["credential_scope"])

    def record_step(
        self, payload: Mapping[str, Any], *, succeeded: bool = True
    ) -> RecipeStep | None:
        if self._finished:
            raise RuntimeError("이미 종료한 recording입니다")
        if not succeeded:
            return None
        raw = dict(payload)
        raw.pop("seq", None)
        if self._is_credential_step(raw):
            match = re.fullmatch(r"\{\{\s*([\w.]+)\s*\}\}", str(raw.get("value") or ""))
            scope = self.flow_metadata.get("credential_scope") or {}
            require_binding = self.credential_bindings_required or bool(scope)
            if not match and require_binding:
                raise ValueError("credential_plaintext_forbidden")
            # Automatic traces already discard secret values before this call.
            # Keep their existing unbound draft placeholders; manual recordings
            # require a replayable Vault binding before any secret is accepted.
            variable = (match.group(1) if match else str(
                raw.get("credential_variable") or f"credential_{len(self.inputs) + 1}"
            ))
            if require_binding and variable not in scope.get("inputs", {}):
                raise ValueError("credential_binding_missing")
            if raw.get("credential_variable", variable) != variable:
                raise ValueError("credential_binding_mismatch")
            raw.pop("credential_variable", None)
            raw.pop("credential", None)
            raw.pop("credential_ref", None)
            raw.pop("secret", None)
            raw["value"] = "{{" + variable + "}}"
            if variable not in {item.name for item in self.inputs}:
                self.inputs.append(
                    RecipeInput(
                        name=variable, secret=True, description="vault credential"
                    )
                )
            raw["risk"] = self._login_risk()
        elif (
            not self.steps
            and raw.get("action") == "navigate"
            and not str(raw.get("risk") or "").strip()
        ):
            # An unspecified first navigation may lead into an authentication
            # flow, so keep the conservative external-write default.  A caller
            # that just verified a public, read-only GET may explicitly record
            # ``risk=READ``; overwriting that declaration made harmless search
            # recipes require an approval that could not be resumed by the
            # chat entry point.
            raw["risk"] = self._login_risk()
        step = RecipeStep.from_dict(raw, seq=len(self.steps) + 1)
        secret_fill_steps(
            WorkRecipe(name=self.name, domain=self.domain, steps=[step]),
            frozenset(item.name for item in self.inputs if item.secret),
        )
        self.steps.append(step)
        return step

    async def finish_recording(self, *, created_by: str = "") -> dict[str, Any]:
        if self._finished:
            raise RuntimeError("이미 종료한 recording입니다")
        if not self.steps:
            raise ValueError("성공한 단계가 없어 레시피를 저장할 수 없습니다")
        verify_steps = [
            RecipeStep.from_dict(raw, seq=len(self.steps) + index)
            for index, raw in enumerate(self.verify, start=1)
        ]
        recipe = WorkRecipe(
            name=self.name,
            domain=self.domain,
            version=1,
            inputs=list(self.inputs),
            steps=list(self.steps),
            verify=verify_steps,
            metadata={
                "recorded": True,
                "source_chat_session_id": self.session_id,
                "screen_e2e": self.e2e_evidence,
                **self.flow_metadata,
            },
        )
        # FR-17: 성공했다고 즉시 실행 가능 레시피로 올리지 않는다. 단계·권한·
        # 위험·증거·변수 dry-run을 저장하고 B-scope 승인을 받은 뒤에만 등록한다.
        pending = await registration.request_registration(
            recipe, tenant_id=self.tenant_id, requested_by=created_by
        )
        self._finished = True
        await approval_notifier.notify_pending_created(pending)
        return pending

    def set_credential_scope(self, scope: Mapping[str, Any]) -> None:
        # Accept the manual {variable: reference} shorthand and the canonical
        # register_e2e contract; always persist the replayable canonical form.
        canonical = dict(scope)
        if "version" not in canonical:
            canonical = {
                "version": 1, "inputs": canonical,
                "allowed_domains": [self.domain], "allowed_recipes": [self.name],
                **({"work_key": self.flow_metadata["browser_work_key"]}
                   if self.flow_metadata.get("browser_work_key") else {}),
            }
        if not isinstance(canonical.get("inputs"), Mapping):
            raise ValueError("credential_scope_invalid")  # noqa: TRY004 — API validation error
        recipe = WorkRecipe(name=self.name, domain=self.domain,
                            metadata={"credential_scope": canonical,
                                      "work_key": self.flow_metadata.get("browser_work_key", "")},
                            inputs=[RecipeInput(name=name, secret=True) for name in canonical.get("inputs", {})])
        parse_scope(recipe)
        for name in canonical["inputs"]:
            RecipeInput.from_any({"name": name})
        previous = self.flow_metadata.get("credential_scope") or {}
        for item in self.inputs:
            if previous.get("inputs", {}).get(item.name) != canonical["inputs"].get(item.name):
                raise ValueError("credential_binding_mismatch")
        self.flow_metadata["credential_scope"] = canonical
        for name in canonical["inputs"]:
            if name not in {item.name for item in self.inputs}:
                self.inputs.append(RecipeInput(name=name, secret=True, description="vault credential"))

    def to_state(self) -> dict[str, Any]:
        return {
            "name": self.name, "domain": self.domain, "session_id": self.session_id,
            "e2e_evidence": self.e2e_evidence, "verify": self.verify,
            "flow_metadata": self.flow_metadata,
            "credential_bindings_required": self.credential_bindings_required,
            "steps": [step.to_dict() for step in self.steps],
            "inputs": [item.to_dict() for item in self.inputs],
        }

    @classmethod
    def from_state(cls, state: Mapping[str, Any], tenant_id: str) -> WorkRecipeRecorder:
        recording = cls(state["name"], state["domain"], tenant_id,
                        session_id=state.get("session_id", ""),
                        e2e_evidence=state.get("e2e_evidence"),
                        verify=state.get("verify"), flow_metadata=state.get("flow_metadata"),
                        credential_bindings_required=state.get("credential_bindings_required", True))
        recording.steps = [RecipeStep.from_dict(raw, seq=index)
                           for index, raw in enumerate(state["steps"], 1)]
        recording.inputs = [RecipeInput.from_any(raw) for raw in state["inputs"]]
        return recording

    def _is_credential_step(self, raw: Mapping[str, Any]) -> bool:
        match = re.fullmatch(r"\{\{\s*([\w.]+)\s*\}\}", str(raw.get("value") or ""))
        if match and match.group(1) in (self.flow_metadata.get("credential_scope") or {}).get("inputs", {}):
            return True
        if raw.get("credential") or raw.get("credential_ref") or raw.get("secret"):
            return True
        if str(raw.get("action") or "") != "fill":
            return False
        hints = " ".join(str(raw.get(key) or "") for key in ("selector", "description", "name"))
        return bool(_CREDENTIAL_HINT.search(hints))

    def _login_risk(self) -> str:
        internal = any(
            self.domain == host or self.domain.endswith("." + host)
            for host in INTERNAL_DOMAINS
        )
        return "WRITE_INTERNAL" if internal else "WRITE_EXTERNAL"


def start_recording(
    name: str,
    domain: str,
    tenant_id: Any = None,
    *,
    session_id: str = "",
    e2e_evidence: Mapping[str, Any] | None = None,
    verify: list[Mapping[str, Any]] | None = None,
    flow_metadata: Mapping[str, Any] | None = None,
    credential_bindings_required: bool = False,
) -> WorkRecipeRecorder:
    return WorkRecipeRecorder(
        name,
        domain,
        tenant_id,
        session_id=session_id,
        e2e_evidence=e2e_evidence,
        verify=verify,
        flow_metadata=flow_metadata,
        credential_bindings_required=credential_bindings_required,
    )


async def finish_recording(
    recording: WorkRecipeRecorder, *, created_by: str = ""
) -> dict[str, Any]:
    return await recording.finish_recording(created_by=created_by)
