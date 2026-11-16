from agent_center.storage.database import is_execution_request


def test_information_questions_are_not_misclassified_as_execution() -> None:
    assert not is_execution_request("What is SQLite?")
    assert not is_execution_request("What is Agent 中控台?")
    assert not is_execution_request("What does the build tool do?")
    assert not is_execution_request("Explain how to implement a feature conceptually.")
    assert not is_execution_request("What is the difference between push and deploy?")
    assert not is_execution_request("解释一下 WAL 是什么")
    assert not is_execution_request("请解释如何实现更好的并发控制")
    assert not is_execution_request("什么是 Agent 中控台？")
    assert not is_execution_request("如何理解这个网站的工作原理？")


def test_file_and_tool_operations_are_execution_tasks() -> None:
    assert is_execution_request("Can you take a look at this project?")
    assert is_execution_request("Please add JSON output to the tool.")
    assert is_execution_request("What is the current tool, and then implement a JSON mode in the project?")
    assert is_execution_request("帮我在项目中做一个小工具")
    assert is_execution_request("修改这个页面并添加测试")
