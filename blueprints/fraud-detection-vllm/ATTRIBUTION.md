# Attribution

This blueprint is **inspired by and builds on** the following open-source projects. Thank you!

## SantanderAI/gen-fraud-graph

The synthetic fraud-graph generator at the heart of this blueprint's Airflow data pipeline.

- Repository: https://github.com/SantanderAI/gen-fraud-graph
- License: **Apache License 2.0**
- Copyright (c) 2026 Santander Group
- Disclaimer (from the project): *this is not an official Banco Santander product or service.*

This blueprint installs `gen-fraud-graph` from source at Airflow runtime and calls its public
API to generate the synthetic dataset; we do not vendor or redistribute its source. We are
grateful to the **Santander AI Lab** for releasing it.

```bibtex
@software{gen_fraud_graph,
  title     = {gen_fraud_graph: Synthetic Fraud Graph Generator},
  author    = {Santander AI Lab},
  year      = {2026},
  url       = {https://github.com/SantanderAI/gen-fraud-graph},
  license   = {Apache-2.0}
}
```

## srinivas-gajulaa/genai-fraud-detection

Inspiration for the "ML score → LLM analyst explanation" interface pattern used by the demo UI.

- Repository: https://github.com/srinivas-gajulaa/genai-fraud-detection

The code in this blueprint is our own; these projects inspired the design.
