"""The shipped examples must be valid and contain no secret."""
import os

import pytest

from censor_core.config import parse_settings
from censor_core.engine import Censor
from censor_core.rules import parse_rules

EX = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "examples")


def test_example_rules_are_valid_and_behave_as_documented():
    result = parse_rules(open(os.path.join(EX, "rules.txt"), encoding="utf-8").read())
    assert not result.issues
    censor = Censor.build(rules=result.rules)
    assert censor.apply("the fox and the wolf.") == "the animal and the ."
    assert censor.apply("see https://intranet.example.test/x") == "see https://[internal]/x"
    assert censor.apply(r"C:\Users\alice\doc") == r"[user-folder]\doc"
    assert censor.apply("#internal") == "[label]"


def test_example_config_parses_without_problem_and_selects_a_group():
    yaml = pytest.importorskip("yaml")
    raw = yaml.safe_load(open(os.path.join(EX, "config.yaml"), encoding="utf-8"))
    settings, problems = parse_settings(raw["plugins"]["entries"]["hermes-censor"]["settings"], home=os.getcwd())
    assert not problems, [p.message for p in problems]
    kp = settings.keepassxc
    assert kp is not None and kp.groups == ["Hermes"] and kp.unlock == "prompt"
    assert kp.include_history is False and kp.include_recycle_bin is False and kp.select_all is False
