"""
DAG: flag_and_anomaly

Push per-account behavioural feature vectors into Milvus (COSINE) for embedding-based
anomaly detection, compute an anomaly score (distance to nearest neighbours), and write the
top flagged accounts (combining the XGBoost score, the anomaly score and ring membership)
into `flagged_accounts` for the investigator UI.
"""
from __future__ import annotations

import pendulum
from airflow.decorators import dag, task

from common import (ACCOUNTS_COLLECTION, FEATURE_DIM, FEATURES, acc_num,
                    milvus_create_collection, milvus_drop_collection, milvus_insert,
                    milvus_search, pg_exec, pg_insert_rows, pg_query)


@dag(
    dag_id="flag_and_anomaly",
    schedule=None,
    start_date=pendulum.datetime(2024, 1, 1, tz="UTC"),
    catchup=False,
    tags=["fraud", "anomaly"],
)
def flag_and_anomaly():
    @task
    def index() -> int:
        """Normalise feature vectors and (re)load them into Milvus."""
        import numpy as np

        rows = pg_query(
            f"SELECT account_id, {', '.join(FEATURES)}, is_labeled_fraud FROM account_scores")
        if not rows:
            raise RuntimeError("no account_scores — run engineer_and_train first")
        ids = [r[0] for r in rows]
        X = np.array([[float(v) for v in r[1:1 + FEATURE_DIM]] for r in rows], dtype=float)
        labels = [int(r[-1]) for r in rows]

        # Min-max normalise each feature to [0,1] so COSINE distance is meaningful.
        lo = X.min(axis=0)
        rng = np.where(X.max(axis=0) - lo == 0, 1.0, X.max(axis=0) - lo)
        Xn = (X - lo) / rng

        milvus_drop_collection(ACCOUNTS_COLLECTION)
        milvus_create_collection(FEATURE_DIM, ACCOUNTS_COLLECTION)
        data = [{"id": acc_num(a), "vector": Xn[i].tolist(),
                 "account_id": a, "is_fraud": labels[i]} for i, a in enumerate(ids)]
        milvus_insert(data, ACCOUNTS_COLLECTION)
        return len(ids)

    @task
    def flag(_n: int) -> dict:
        """Compute anomaly scores + write the top flagged accounts."""
        import numpy as np

        rows = pg_query(
            f"SELECT account_id, {', '.join(FEATURES)}, is_labeled_fraud, xgb_score "
            f"FROM account_scores")
        ids = [r[0] for r in rows]
        X = np.array([[float(v) for v in r[1:1 + FEATURE_DIM]] for r in rows], dtype=float)
        in_ring = {r[0]: int(r[1 + FEATURES.index("in_cycle")]) for r in rows}
        xgb = {r[0]: float(r[-1]) for r in rows}

        lo = X.min(axis=0)
        rng = np.where(X.max(axis=0) - lo == 0, 1.0, X.max(axis=0) - lo)
        Xn = (X - lo) / rng

        pg_exec("DROP TABLE IF EXISTS flagged_accounts CASCADE")
        pg_exec("""CREATE TABLE flagged_accounts (
            account_id TEXT PRIMARY KEY, xgb_score DOUBLE PRECISION,
            anomaly_score DOUBLE PRECISION, in_ring INT, rank INT)""")

        # Anomaly score = 1 - mean cosine similarity to the 6 nearest neighbours
        # (higher = more isolated / unusual).
        scored = []
        for i, a in enumerate(ids):
            hits = milvus_search(Xn[i].tolist(), 6, ACCOUNTS_COLLECTION)
            sims = [h.get("distance", 0.0) for h in hits][1:]  # skip self
            anomaly = round(1.0 - (sum(sims) / len(sims) if sims else 1.0), 4)
            combined = 0.7 * xgb[a] + 0.3 * anomaly
            scored.append((a, xgb[a], anomaly, in_ring.get(a, 0), combined))

        scored.sort(key=lambda t: t[4], reverse=True)
        top = scored[:50]
        pg_insert_rows(
            "flagged_accounts",
            ["account_id", "xgb_score", "anomaly_score", "in_ring", "rank"],
            [(a, xs, an, ir, rk + 1) for rk, (a, xs, an, ir, _c) in enumerate(top)])
        return {"flagged": len(top)}

    flag(index())


flag_and_anomaly()
