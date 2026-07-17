from fixture_vision.data import generate
from fixture_vision.model import FusionModel


def test_data_is_deterministic():
    config = {"seed": 42, "samples": 40, "input_dim": 4}
    assert (generate(config)["train"][0] == generate(config)["train"][0]).all()


def test_concat_has_more_parameters():
    add = FusionModel(4, 6, "addition", 42)
    concat = FusionModel(4, 6, "concatenation_projection", 42)
    assert concat.parameter_count > add.parameter_count
