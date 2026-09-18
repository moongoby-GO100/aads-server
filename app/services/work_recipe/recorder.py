"""성공한 브라우저 조작을 재생 가능한 WorkRecipe로 굳힌다."""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from app.services.work_recipe import store
from app.services.work_recipe.guard import INTERNAL_DOMAINS
from app.services.work_recipe.schema import RecipeInput, RecipeStep, WorkRecipe

_CREDENTIAL_HINT = re.compile(
    r"password|passwd|pwd|login|sign.?in|username|user.?id|email|otp|인증|비밀번호|아이디",
    re.IGNORECASE,
)


class WorkRecipeRecorder:
    """한 번의 성공 경로를 메모리에 모은 뒤 버전 레시피로 저장한다."""

    def __init__(self, name: str, domain: str, tenant_id: Any = None) -> None:
        self.name = str(name or "").strip()
        self.domain = store.normalize_domain(domain)
        self.tenant_id = tenant_id
        if not self.name or not self.domain:
            raise ValueError("레시피 name과 domain이 필요합니다")
        self.steps: list[RecipeStep] = []
        self.inputs: list[RecipeInput] = []
        self._finished = False

    def record_step(self, payload: Mapping[str, Any], *, succeeded: bool = True) -> RecipeStep | None:
        if self._finished:
            raise RuntimeError("이미 종료한 recording입니다")
        if not succeeded:
            return None
        raw = dict(payload)
        raw.pop("seq", None)
        if self._is_credential_step(raw):
            variable = str(raw.pop("credential_variable", "") or f"credential_{len(self.inputs) + 1}")
            raw.pop("credential", None)
            raw.pop("credential_ref", None)
            raw.pop("secret", None)
            raw["value"] = "{{" + variable + "}}"
            if variable not in {item.name for item in self.inputs}:
                self.inputs.append(RecipeInput(name=variable, secret=True, description="vault credential"))
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
        version = await store.next_version(
            name=self.name, domain=self.domain, tenant_id=self.tenant_id
        )
        recipe = WorkRecipe(
            name=self.name,
            domain=self.domain,
            version=version,
            inputs=list(self.inputs),
            steps=list(self.steps),
            metadata={"recorded": True, "credentials": "credential_scope"},
        )
        return await store.save_recipe(
            recipe, tenant_id=self.tenant_id, created_by=created_by
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


def start_recording(name: str, domain: str, tenant_id: Any = None) -> WorkRecipeRecorder:
    return WorkRecipeRecorder(name, domain, tenant_id)


async def finish_recording(recording: WorkRecipeRecorder, *, created_by: str = "") -> dict[str, Any]:
    return await recording.finish_recording(created_by=created_by)
