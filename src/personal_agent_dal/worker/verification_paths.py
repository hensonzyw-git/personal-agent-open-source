"""Operator-declared read-only runtime dependencies, bound by the toolchain digest."""
from pathlib import Path
from personal_agent_dal.worker.supervisor import SupervisorRefusal


def runtime_read_roots(command):
    if type(command.get('backup_exclusion_metadata', False)) is not bool:
        raise SupervisorRefusal('WORKFLOW_CONFIG_INVALID')
    roots = command.get('runtime_read_roots', [])
    if not isinstance(roots, list) or len(roots) > 16:
        raise SupervisorRefusal('WORKFLOW_CONFIG_INVALID')
    result = []
    for value in roots:
        if not isinstance(value, str) or not value or '\0' in value:
            raise SupervisorRefusal('WORKFLOW_CONFIG_INVALID')
        path = Path(value)
        if not path.is_absolute() or not path.exists() or str(path.resolve()) != value or path == Path('/'):
            raise SupervisorRefusal('WORKFLOW_CONFIG_INVALID')
        result.append(path)
    if len(set(result)) != len(result):
        raise SupervisorRefusal('WORKFLOW_CONFIG_INVALID')
    return tuple(result)
