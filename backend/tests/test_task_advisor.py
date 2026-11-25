"""Task advisor tests: the LLM proposes, the registry disposes."""

import json

from app.models import EventType, PersonEvent, RiskTier, Task
from app.services.task_advisor import (
    ADVISOR_DISABLED,
    ADVISOR_OK,
    ADVISOR_PARSE_FAILED,
    TaskAdvisor,
)


class FakeClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def complete(self, system, user):
        self.calls.append((system, user))
        return self.response


class ExplodingClient:
    def complete(self, system, user):
        raise RuntimeError("API down")


def make_event():
    return PersonEvent(
        id=3,
        event_type=EventType.ONBOARDING,
        person_name="Jordan Lee",
        requested_by="recruiting",
        created_at="2026-11-25T10:00:00+00:00",
        tasks=[
            Task(id="create_account", title="Create account", risk_tier=RiskTier.AUTO),
            Task(id="schedule_orientation", title="Orientation", risk_tier=RiskTier.AUTO),
        ],
    )


def test_disabled_when_no_client():
    advisor = TaskAdvisor(llm_client=None)

    result = advisor.suggest(make_event())

    assert result["status"] == ADVISOR_DISABLED
    assert result["suggestions"] == []


def test_registry_only_suggestions_survive():
    """LLM tries to invent a task; the invented id is dropped."""

    client = FakeClient(json.dumps({"suggestions": [
        {"id": "prepare_laptop", "rationale": "role requires travel"},
        {"id": "deploy_space_laser", "rationale": "trust me"},
    ]}))
    advisor = TaskAdvisor(llm_client=client)

    result = advisor.suggest(make_event())

    assert result["status"] == ADVISOR_OK
    ids = [s["id"] for s in result["suggestions"]]
    assert ids == ["prepare_laptop"]
    assert len(result["dropped"]) == 1


def test_already_present_tasks_not_resuggested():
    import json

    client = FakeClient(json.dumps({"suggestions": [
        {"id": "create_account", "rationale": "again"},
    ]}))
    advisor = TaskAdvisor(llm_client=client)

    result = advisor.suggest(make_event())

    assert result["suggestions"] == []
    assert len(result["dropped"]) == 1


def test_malformed_llm_response_yields_empty_not_crash():
    advisor = TaskAdvisor(llm_client=FakeClient("here are some tasks: laptop!!"))

    result = advisor.suggest(make_event())

    assert result["status"] == ADVISOR_PARSE_FAILED
    assert result["suggestions"] == []


def test_llm_outage_yields_empty_not_crash():
    advisor = TaskAdvisor(llm_client=ExplodingClient())

    result = advisor.suggest(make_event())

    assert result["status"] == ADVISOR_PARSE_FAILED
    assert result["suggestions"] == []


def test_acceptance_uses_registry_risk_tier_not_llm_words():
    import json

    class GenerousClient:
        def complete(self, system, user):
            return json.dumps({"suggestions": [
                {"id": "hand_off_credentials", "rationale": "remote hire",
                 "risk_tier": "auto", "depends_on": ["fake_step"]},
            ]})

    advisor = TaskAdvisor(llm_client=GenerousClient())
    event = make_event()

    result = advisor.suggest(event)
    accepted = advisor.accept(
        event, result["suggestions"][0]["id"], accepted_by="Priya"
    )

    assert accepted is True
    task = event.task_by_id("hand_off_credentials")
    # Registry wins: gated stays gated, junk fields ignored.
    assert task.risk_tier == RiskTier.APPROVAL_REQUIRED
    assert task.depends_on == ["create_account"]


def test_accept_rejects_unknown_and_duplicate():
    advisor = TaskAdvisor()
    event = make_event()

    assert advisor.accept(event, "deploy_space_laser", "Priya") is False
    assert advisor.accept(event, "create_account", "Priya") is False  # duplicate
    assert event.task_by_id("create_account") is not None  # untouched


def test_nothing_available_means_no_llm_call():
    """Complete checklist: the advisor doesn't even spend an API call."""

    class SpyClient:
        def __init__(self):
            self.called = False

        def complete(self, system, user):
            self.called = True
            return "{}"

    spy = SpyClient()
    advisor = TaskAdvisor(llm_client=spy)

    event = PersonEvent(
        id=4,
        event_type=EventType.ONBOARDING,
        person_name="X",
        requested_by="hr",
        created_at="2026-11-25T10:00:00+00:00",
        tasks=[
            Task(id=s.id, title=s.title, risk_tier=s.risk_tier)
            for s in __import__("app.services.checklist_builder", fromlist=["ONBOARDING_TEMPLATE"]).ONBOARDING_TEMPLATE
        ],
    )

    result = advisor.suggest(event)

    assert result["status"] == ADVISOR_OK
    assert spy.called is False
