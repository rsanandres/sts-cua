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


def test_max_mode_is_limited_only_by_free_vram():
    from sts_cua.models.registry import MAX_BUDGET_GIB
    from sts_cua.serve import choose

    assert budget("max") == MAX_BUDGET_GIB
    every = set(MODELS)
    m, reason = choose("best", "max", 20.0, every)
    assert m.name == "holo4-27b" and "max budget" in reason
    m, _ = choose("qwen3.8-27b-q5", "max", 20.0, every)  # 19.7 GiB: decided by free VRAM alone
    assert m.name == "qwen3.8-27b-q5"
