# SUSE AI Factory and the Application Collection

SUSE AI Factory is the AI platform management layer built into Rancher. It lets
platform teams discover AI applications, compose them into validated stacks,
publish those stacks as reusable blueprints, and deploy and monitor AI workloads on
any Rancher-managed Kubernetes cluster.

A Blueprint is a cluster-scoped custom resource that lists the components — Helm
charts — that make up an AI stack, along with the values each chart should use.
Importing a Blueprint (for example with `kubectl apply`) adds it to the AI Factory
catalog. To actually run it, a user creates an AIWorkload from the Blueprint, which
tells the operator to deploy the components into a target namespace.

Each Blueprint component references a chart by name, by the Rancher ClusterRepo it
comes from, and by version. The SUSE Application Collection is the primary source:
an OCI registry at dp.apps.rancher.io that publishes hardened Helm charts and
container images, including Apache Airflow, Ollama, and Milvus. Charts pull images
from the same registry and use a shared image-pull secret, which keeps deployments
consistent and air-gap friendly.

This blueprint composes Apache Airflow, Ollama, and Milvus from the Application
Collection into a small GenAI pipeline: Airflow orchestrates knowledge-base
ingestion and model customization, Ollama serves the language and embedding models,
and Milvus stores the retrieval index.
