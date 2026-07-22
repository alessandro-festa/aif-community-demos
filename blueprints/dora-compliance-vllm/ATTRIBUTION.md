# Attribution

This blueprint is **inspired by and adapts** the following open-source project. Thank you!

## Chirag-Kathuria-009/DORA-Pipeline

The reference end-to-end DORA ICT-incident data pipeline whose **BaFin Article 18
classification rules** this blueprint faithfully ports.

- Repository: https://github.com/Chirag-Kathuria-009/DORA-Pipeline
- License: **MIT**

What we reused: the DORA severity thresholds and notification-deadline logic (CRITICAL /
MAJOR / MINOR; 4h / 72h), the incident field model, and the overall
simulate → classify → mart → alert shape of the pipeline.

What we changed: the reference stack (Apache Kafka + PySpark Structured Streaming + Apache
Iceberg + MinIO + dbt + Great Expectations + Apache Superset, orchestrated by Airflow with
DockerOperator) is replaced by **Airflow Python tasks + PostgreSQL + Milvus + a local FastAPI
UI**, all sourced from the **SUSE Application Collection**, to fit the Blueprint Marketplace's
all-SUSE pattern and run without a full lakehouse.

What we added: the reference pipeline has **no LLM**. This blueprint adds an LLM that
**explains** incidents and a tool-calling **compliance agent** that searches the data (through
Airflow's Postgres connection + Milvus) and drives the pipeline via the Airflow REST API.

The code in this blueprint is our own; the DORA-Pipeline project inspired the design and
provided the classification rules.
