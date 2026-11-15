from agent_center.storage.database import is_execution_request


def test_default_requires_execution_except_clear_information_questions() -> None:
    assert not is_execution_request("What is SQLite?")
    assert not is_execution_request("解释一下 WAL 是什么")
    assert is_execution_request("Can you take a look at this project?")
    assert is_execution_request("帮我在项目中做一个小工具")
    assert is_execution_request("The goal is a trivial coding task")
