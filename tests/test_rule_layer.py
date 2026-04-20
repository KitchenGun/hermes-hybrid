from src.router.rule_layer import RuleLayer


def test_ping():
    r = RuleLayer().match("/ping")
    assert r is not None
    assert r.handler == "ping"
    assert r.response == "pong"


def test_help():
    r = RuleLayer().match("/help")
    assert r is not None
    assert r.handler == "help"
    assert "commands" in (r.response or "").lower()


def test_status_with_id():
    r = RuleLayer().match("/status abc-123")
    assert r is not None
    assert r.handler == "status"
    assert r.args["task_id"] == "abc-123"


def test_non_match():
    assert RuleLayer().match("hello world") is None
    assert RuleLayer().match("/unknown foo") is None
