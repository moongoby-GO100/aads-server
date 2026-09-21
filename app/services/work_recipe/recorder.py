"""성공한 브라우저 조작을 승인 대기 WorkRecipe draft로 굳힌다."""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from app.services.work_recipe import registration, store
from app.services.work_recipe.guard import INTERNAL_DOMAINS
from app.services.work_recipe.schema import RecipeInput, RecipeStep, WorkRecipe

_CREDENTIAL_HINT = re.compile(
    r"password|passwd|pwd|login|sign.?in|username|user.?id|email|otp|인증|비밀번호|아이디",
    re.IGNORECASE,
)


class WorkRecipeRecorder:
    """한 번의 성공 경로를 메모리에 모은 뒤 버전 레시피로 저장한다."""

    def __init__(
        self,
        name: str,
        domain: str,
        tenant_id: Any = None,
        *,
        session_id: str = "",
        e2e_evidence: Mapping[str, Any] | None = None,
    ) -> None:
        self.name = str(name or "").strip()
        self.domain = store.normalize_domain(domain)
        self.tenant_id = tenant_id
        self.session_id = str(session_id or "").strip()
        self.e2e_evidence = dict(e2e_evidence or {})
        if not self.name or not self.domain:
            raise ValueError("레시피 name과 domain이 필요합니다")
        self.steps: list[RecipeStep] = []
        self.inputs: list[RecipeInput] = []
        self._finished = False

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
            variable = str(
                raw.pop("credential_variable", "")
                or f"credential_{len(self.inputs) + 1}"
            )
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
        elif not self.steps and raw.get("action") == "navigate":
            raw["risk"] = self._login_risk()
        step = RecipeStep.from_dict(raw, seq=len(self.steps) + 1)
        self.steps.append(step)
        return step

    async def finish_recording(self, *, created_by: str = "") -> dict[str, Any]:
        if self._finished:
            raise RuntimeError("이미 종료한 recording입니다")
        if not self.steps:
            raise ValueError("성공한 단계가 없어 레시피를 저장할 수 없습니다")
        self._finished = True
        recipe = WorkRecipe(
            name=self.name,
            domain=self.domain,
            version=1,
            inputs=list(self.inputs),
            steps=list(self.steps),
            metadata={
                "recorded": True,
                "credentials": "credential_scope",
                "source_chat_session_id": self.session_id,
                "screen_e2e": self.e2e_evidence,
            },
        )
        # FR-17: 성공했다고 즉시 실행 가능 레시피로 올리지 않는다. 단계·권한·
        # 위험·증거·변수 dry-run을 저장하고 B-scope 승인을 받은 뒤에만 등록한다.
        return await registration.request_registration(
            recipe, tenant_id=self.tenant_id, requested_by=created_by
        )

    def _is_credential_step(self, raw: Mapping[str, Any]) -> bool:
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
) -> WorkRecipeRecorder:
    return WorkRecipeRecorder(
        name,
        domain,
        tenant_id,
        session_id=session_id,
        e2e_evidence=e2e_evidence,
    )


async def finish_recording(
    recording: WorkRecipeRecorder, *, created_by: str = ""
) -> dict[str, Any]:
    return await recording.finish_recording(created_by=created_by)
