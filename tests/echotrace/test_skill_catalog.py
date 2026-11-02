import json
from pathlib import Path

import pytest

from echotrace.agent.skill_catalog import AgentSkillCatalog


SKILLS = Path(__file__).parents[2] / "echotrace" / "skills"


def test_each_agent_sees_only_its_procedural_skills():
    catalog = AgentSkillCatalog(SKILLS)

    assert {item["name"] for item in catalog.index(role="orchestrator")} == set()
    assert {item["name"] for item in catalog.index(role="researcher")} == {
        "analyze-public-discourse",
        "ingest-and-curate-sources",
        "research-trace-node",
    }
    assert {item["name"] for item in catalog.index(role="verifier")} == {"review-trace-node"}
    assert {item["name"] for item in catalog.index(role="conclusion_formatter")} == {"format-conclusion-ir"}
    assert {item["name"] for item in catalog.index(role="report_composer")} == {
        "synthesize-investigation-report"
    }


def test_every_skill_has_a_matching_nonempty_eval_suite():
    catalog = AgentSkillCatalog(SKILLS)

    for skill in catalog.discover():
        eval_path = skill.path / "evals" / "evals.json"
        suite = json.loads(eval_path.read_text(encoding="utf-8"))
        assert suite["skill_name"] == skill.name
        assert suite["evals"]


def test_role_cannot_activate_a_skill_outside_its_catalog():
    catalog = AgentSkillCatalog(SKILLS)

    with pytest.raises(ValueError, match="not available to role researcher"):
        catalog.activate("synthesize-investigation-report", role="researcher")


def test_skill_metadata_requires_known_roles(tmp_path):
    package = tmp_path / "invalid"
    package.mkdir()
    (package / "SKILL.md").write_text(
        "---\nname: invalid\ndescription: invalid role\nroles: [not-a-project-role]\n---\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="invalid Agent Skill metadata"):
        AgentSkillCatalog(tmp_path).discover()
