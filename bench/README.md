# bench/

Grounding targets for the local-model bake-off (`python -m sts_cua.bench`, see the README's "Bake-off"). Generated on Raph's PC from his runs, so everything here except this file is in `.gitignore`.

- `targets.jsonl`: one target per line: id (`<run>-<step>`), image, instruction, x, y, radius (40 px at 1280x720), size, state_type, check, source.
- `images/`: the step screenshots the targets point into, copied from `runs/<run>/screenshots/`.
- `review/sheet-NN.png`: contact sheets from `bench review`, each target circled.
- `rejected.txt`: ids deleted with `bench review --delete`, which `bench extract` never adds back.
