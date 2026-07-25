"""Effort resolution. Output tokens are the majority of spend, so this is the
main cost dial — and the one role we have evidence is worth paying for (risk)
must not get silently downgraded by a cheap cycle."""

from trading.config import AgentSettings


def test_unset_means_no_effort_field():
    """None must mean 'send nothing' so the API default is preserved — sending a
    guessed value would silently change behaviour on every call."""
    assert AgentSettings().effort_for("strategy", cycle="intraday") is None


def test_cycle_sets_effort():
    s = AgentSettings(effort_by_cycle={"intraday": "medium", "premarket": "high"})
    assert s.effort_for("strategy", cycle="intraday") == "medium"
    assert s.effort_for("strategy", cycle="premarket") == "high"


def test_role_beats_cycle():
    """The intraday step-down must not drag the risk review down with it."""
    s = AgentSettings(effort_by_cycle={"intraday": "medium"},
                      effort_by_role={"risk": "high"})
    assert s.effort_for("risk", cycle="intraday") == "high"
    assert s.effort_for("strategy", cycle="intraday") == "medium"


def test_global_default_is_the_fallback():
    s = AgentSettings(effort="low")
    assert s.effort_for("strategy") == "low"
    assert s.effort_for("strategy", cycle="unknown-cycle") == "low"


def test_shipped_config_does_not_downgrade_risk():
    """Guards the actual settings.yaml wiring, not just the model."""
    from trading.config import load_config
    s = load_config().settings.agents
    assert s.effort_for("risk", cycle="intraday") == "high"
    assert s.effort_for("redteam", cycle="intraday") == "high"
    assert s.effort_for("strategy", cycle="intraday") == "medium"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
    print("all effort-routing checks passed")
