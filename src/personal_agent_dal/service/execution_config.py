"""Public, explicit deployment pins; contains no PA bridge trust or defaults."""
import json
from typing import Annotated, Literal
from pydantic import Field, StrictInt, model_validator
from personal_agent_dal.machine.workflow_selection import Closed, Id, Digest, Role


class Profile(Closed):
    revision_id: Id
    profile: Literal['A', 'B']
    revision: Annotated[StrictInt, Field(ge=1)]
    roles: dict[Literal['planner', 'coder', 'reviewer'], Role]


class ApprovedInput(Closed):
    repository_id: Id
    base_sha: Annotated[str, Field(strict=True, pattern=r'^[0-9a-f]{40}$')]
    toolchain_ref: Id
    toolchain_manifest_sha256: Digest


class ExecutionConfig(Closed):
    schema_version: Literal['dal.execution-profiles/1.0']
    profiles: list[Profile]
    approved_inputs: list[ApprovedInput]

    @model_validator(mode='after')
    def unique(self):
        if len({p.revision_id for p in self.profiles}) != len(self.profiles):
            raise ValueError('DUPLICATE_PROFILE')
        if len({p.profile for p in self.profiles}) != len(self.profiles):
            raise ValueError('DUPLICATE_PROFILE')
        return self

    def check_input(self, body):
        pin = {k: getattr(body, k) for k in ApprovedInput.model_fields}
        if pin not in [p.model_dump() for p in self.approved_inputs]:
            raise ValueError('EXECUTION_INPUT_NOT_APPROVED')

    def check_profile(self, revision_id):
        if revision_id not in {p.revision_id for p in self.profiles}:
            raise ValueError('PROFILE_UNAVAILABLE')


def load_execution_config(path):
    # Same owner/mode/no-symlink boundary as existing trusted configuration.
    from personal_agent.api.dal_client import _read_bridge_file
    return ExecutionConfig.model_validate(json.loads(_read_bridge_file(path, kind='CONFIG')))
