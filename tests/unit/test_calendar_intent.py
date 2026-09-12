from personal_agent.runtime.calendar_intent import is_calendar_create_request


def test_explicit_calendar_create_is_recognised() -> None:
    assert is_calendar_create_request(
        "明天上午 10 点，在日常安排创建一个名为“Personal Agent 验收”的 30 分钟日程"
    )


def test_calendar_query_is_not_a_create_request() -> None:
    assert not is_calendar_create_request("查询我今天和明天的日程")
