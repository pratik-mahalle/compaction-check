# Contributing

This developer alpha focuses on explicitly registered constraints and compaction boundaries that fit the current context limit. The most useful feedback is a reproducible natural loss, a false alarm, a stale rule, or a completed agent action that violated a retained rule.

Use the alpha feedback issue template. Include the package/model versions, mode, expected behavior, actual outcome, and a minimal sanitized example. Keep credentials and private conversations out of public issues. The registry and result files can contain original user instructions.

## Local checks

```sh
python -m pip install -e . ruff==0.16.9 build twine
python -m unittest discover -s tests -v
ruff check .
ruff format --check .
python -m build --outdir release-dist
python -m twine check release-dist/*
python scripts/release_artifacts.py --tag v0.2.0a1 --directory release-dist
```

Offline tests do not use an API key. Live benchmarks are opt-in and incur TypeSafe API usage. Label injected losses as synthetic, preserve whole-session evaluation splits, and report task completion alongside violations and stops.

See [RELEASING.md](RELEASING.md) for the alpha release process.
