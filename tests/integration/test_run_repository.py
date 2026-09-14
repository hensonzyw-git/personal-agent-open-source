from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import pytest
from sqlalchemy import select
from test_run_budget_store import setup
from test_agent_storage import SEALED
from personal_agent.runtime.run_repository import RunRepository
from personal_agent.runtime.run_store import RunStateError


def test_web_daily_reservation_is_atomic_with_run_budget(setup):
    budgets,new_run,factory=setup
    repo=RunRepository(factory,None)
    leases=[]
    for n in range(2):
        op=new_run(str(n));lease=repo.acquire(op,owner=str(n),now_ms=0,ttl_ms=50000)
        repo.reserve_prebind(op,[],now_ms=1,lease=lease)
        repo.bind(op,task_id=None,new_task_id='t'+str(n),now_ms=2,sealed_goal=SEALED,sealed_constraints=SEALED,lease=lease)
        leases.append(lease)
    barrier=Barrier(2)
    def reserve(lease):
        barrier.wait()
        try:repo.reserve_web(lease,'web',now_ms=3,daily_limit=1);return True
        except RunStateError:return False
    with ThreadPoolExecutor(max_workers=2) as pool:assert sorted(pool.map(reserve,leases))==[False,True]
    assert sum(repo.snapshot(l.operation_id)['web_used'] for l in leases)==1
    assert sum(repo.snapshot(l.operation_id)['read_used'] for l in leases)==1


def test_new_worker_can_take_expired_read_lease_without_resetting_budget(setup):
    budgets,new_run,factory=setup;repo=RunRepository(factory,None)
    op=new_run('restart');lease=repo.acquire(op,owner='old',now_ms=0,ttl_ms=10000)
    repo.reserve_prebind(op,[],now_ms=1,lease=lease)
    repo.bind(op,task_id=None,new_task_id='task',now_ms=2,sealed_goal=SEALED,sealed_constraints=SEALED,lease=lease)
    repo.recover(op,now_ms=11000)
    new=repo.acquire(op,owner='new',now_ms=11000,ttl_ms=30000)
    assert repo.snapshot(op)['llm_used']==1
    with pytest.raises(RunStateError):repo.check(lease,now_ms=11001)
    repo.check(new,now_ms=11001)
