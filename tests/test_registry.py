from sts_cua.models.registry import COOP_BUDGET_GIB, MODELS, SOLO_BUDGET_GIB, best, budget


def test_every_model_has_files_and_a_known_server():
    for m in MODELS.values():
        assert m.weights and all(f.size > 0 for f in m.weights)
        assert m.server in ("vllm", "llama")
        assert m.server == "vllm" or m.mmproj is not None  # llama.cpp needs the vision projector


def test_budgets():
    assert budget("coop") == COOP_BUDGET_GIB and budget("solo") == SOLO_BUDGET_GIB
    assert MODELS["ui-venus-2-9b"].fits(COOP_BUDGET_GIB)
    assert MODELS["qwen3.8-27b"].fits(SOLO_BUDGET_GIB) and not MODELS["qwen3.8-27b"].fits(COOP_BUDGET_GIB)
    assert MODELS["holo4-27b"].fits(SOLO_BUDGET_GIB)


def test_best_picks_the_strongest_that_fits():
    assert best(COOP_BUDGET_GIB).name == "ui-venus-2-9b"
    assert best(SOLO_BUDGET_GIB).name == "holo4-27b"
    assert best(SOLO_BUDGET_GIB, downloaded={"qwen3.8-27b", "ui-venus-2-9b"}).name == "qwen3.8-27b"
    assert best(5.0) is None
