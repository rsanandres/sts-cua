"""The local models we can serve on the 4090, with their files and server settings.

One entry per `--model` name. The serving code (download, start, VRAM check), the
adapters (prompt and coordinate format) and the bake-off all read this table, so a new
model is added here first. Sizes are bytes from the Hugging Face API, read 2026-10-26;
see docs/hosting.md ("Stronger options") for why each model is here.

Budgets (docs/hosting.md): about 13 GiB free for the model in co-op (two game copies),
about 20 GiB solo (one copy), and "max" when Raph isn't using the PC: no fixed budget,
only the VRAM measured free at start (capped at the card's 24 GiB). `fits` checks file sizes plus a fixed allowance for the
KV cache and compute buffers; the real number is measured on the PC.
"""

from __future__ import annotations

from dataclasses import dataclass, field

GIB = 2**30
COOP_BUDGET_GIB = 13.0
SOLO_BUDGET_GIB = 20.0
MAX_BUDGET_GIB = 24.0  # the whole RTX 4090; in max mode the free-VRAM check is the real limit


@dataclass(frozen=True)
class File:
    name: str  # path inside the repo
    size: int  # bytes


@dataclass(frozen=True)
class LocalModel:
    name: str  # --model value
    family: str  # adapter: "qwen3-vl" (cookbook computer_use), "qwen3.8", "holo4", "ui-venus-2"
    server: str  # "vllm" or "llama"
    repo: str  # Hugging Face repo
    weights: tuple[File, ...]
    mmproj: File | None = None  # llama.cpp vision projector
    served_name: str = ""  # name the server answers to (vLLM: the repo id)
    license: str = ""
    context: int = 8192
    overhead_gib: float = 1.5  # KV cache + compute buffers on top of the files, until measured
    thinking: bool = False  # supports a thinking mode the adapter can switch on
    notes: str = ""
    extra_args: tuple[str, ...] = field(default=())
    rank: int = 0  # higher is stronger, from published scores until the bake-off measures it

    @property
    def file_gib(self) -> float:
        return (sum(f.size for f in self.weights) + (self.mmproj.size if self.mmproj else 0)) / GIB

    @property
    def need_gib(self) -> float:
        return self.file_gib + self.overhead_gib

    def fits(self, budget_gib: float) -> bool:
        return self.need_gib <= budget_gib


MODELS: dict[str, LocalModel] = {m.name: m for m in (
    LocalModel(
        name="qwen3-vl-8b", family="qwen3-vl", server="vllm",
        repo="Qwen/Qwen3-VL-8B-Instruct-FP8", served_name="Qwen/Qwen3-VL-8B-Instruct-FP8",
        weights=(File("model.safetensors", 10_590_299_512),), license="apache-2.0",
        notes="First pick; too weak for Spire strategy. Kept as the baseline.", rank=10,
    ),
    LocalModel(
        name="qwen3.8-27b", family="qwen3.8", server="llama",
        repo="unsloth/Qwen3.8-27B-GGUF", served_name="qwen3.8-27b",
        weights=(File("Qwen3.8-27B-UD-Q4_K_XL.gguf", 17_559_178_144),),
        mmproj=File("mmproj-F16.gguf", 927_607_488), license="apache-2.0", thinking=True,
        notes="Solo pick. OSWorld-Verified 84.3 (model card). Thinking on by default upstream.", rank=80,
    ),
    LocalModel(
        name="qwen3.8-27b-iq3", family="qwen3.8", server="llama",
        repo="unsloth/Qwen3.8-27B-GGUF", served_name="qwen3.8-27b-iq3",
        weights=(File("Qwen3.8-27B-UD-IQ3_XXS.gguf", 10_934_860_704),),
        mmproj=File("mmproj-F16.gguf", 927_607_488), license="apache-2.0", thinking=True,
        notes="Co-op stretch: fits 13 GiB with little headroom; 3-bit clicking untested.", rank=50,
    ),
    LocalModel(
        name="qwen3.8-27b-q5", family="qwen3.8", server="llama",
        repo="unsloth/Qwen3.8-27B-GGUF", served_name="qwen3.8-27b-q5",
        weights=(File("Qwen3.8-27B-UD-Q5_K_S.gguf", 18_665_753_504),),
        mmproj=File("mmproj-F16.gguf", 927_607_488), license="apache-2.0", thinking=True, rank=85,
        notes="Max solo: 5-bit, about 19.7 GiB with overhead. Only when measured free VRAM allows.",
    ),
    LocalModel(
        name="holo4-27b", family="holo4", server="llama",
        repo="Hcompany/Holo4-27B-GGUF", served_name="holo4-27b",
        weights=(File("q4_k_m.gguf", 16_875_182_592),),
        mmproj=File("mmproj.f16.gguf", 927_606_816), license="cc-by-nc-4.0", thinking=True,
        notes="Solo A/B against qwen3.8-27b. Fine-tune of Qwen3.8-27B; OSWorld 2.0 61.7.", rank=90,
    ),
    LocalModel(
        name="ui-venus-2-9b", family="ui-venus-2", server="llama",
        repo="bartowski/UI-Venus-2-9B-GGUF", served_name="ui-venus-2-9b",
        weights=(File("UI-Venus-2-9B-Q8_0.gguf", 9_545_982_752),),
        mmproj=File("mmproj-UI-Venus-2-9B-f16.gguf", 918_165_664), license="pending", thinking=True,
        notes="Co-op pick. OSWorld-Verified 70.8 (model card). Upstream license pending.", rank=60,
    ),
)}


def budget(mode: str) -> float:
    return {"coop": COOP_BUDGET_GIB, "max": MAX_BUDGET_GIB}.get(mode, SOLO_BUDGET_GIB)


def best(free_gib: float, downloaded: set[str] | None = None) -> LocalModel | None:
    """The highest-ranked model whose need fits in free_gib (only downloaded ones, if given)."""
    fitting = [m for m in MODELS.values()
               if m.need_gib <= free_gib and (downloaded is None or m.name in downloaded)]
    return max(fitting, key=lambda m: m.rank, default=None)
