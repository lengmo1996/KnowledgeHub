# Fixture Vision Project

Controlled, deterministic, CPU-only synthetic project for validating KnowledgeHub V3.
It compares addition fusion with concatenation plus projection. Results are fixture
evidence only and must never be presented as real research findings.

Run from this directory with:

```bash
python -m fixture_vision.train --config configs/fusion_add.yaml --output run.json
```

No dataset is downloaded and no external service is called.
