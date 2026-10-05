"""
DAG: engineer_and_train

Feature-engineer the loaded transaction graph, train an XGBoost fraud classifier on the
ground-truth labels (SMOTE for the heavy class imbalance), batch-score every account, and
record model metrics. Results land in Postgres for the UI + the anomaly DAG.

Features per account (see common.FEATURES): out/in degree, out/in amount, mean/max amount,
count of high-value edges, membership in a high-value cycle (money-laundering ring signal),
balance and risk_score.
"""
from __future__ import annotations

import pendulum
from airflow.decorators import dag, task

from common import FEATURES, HIGH_VALUE, acc_num, pg_exec, pg_insert_rows, pg_query


@dag(
    dag_id="engineer_and_train",
    schedule=None,
    start_date=pendulum.datetime(2024, 1, 1, tz="UTC"),
    catchup=False,
    tags=["fraud", "train"],
)
def engineer_and_train():
    @task
    def features() -> str:
        """Build per-account features + labels; store them in the account_scores table."""
        import networkx as nx
        import pandas as pd

        acc = pd.DataFrame(pg_query("SELECT account_id, balance, risk_score FROM accounts"),
                           columns=["account_id", "balance", "risk_score"])
        tx = pd.DataFrame(
            pg_query("SELECT src_id, dst_id, amount FROM transactions"),
            columns=["src_id", "dst_id", "amount"])
        cases = pg_query("SELECT involved_accounts FROM fraud_cases")

        # Ground-truth fraud accounts (pipe-separated involved_accounts).
        fraud_accounts = set()
        for (involved,) in cases:
            if involved:
                fraud_accounts.update(str(involved).split("|"))

        # Aggregate transaction stats per account.
        out_deg = tx.groupby("src_id").size()
        in_deg = tx.groupby("dst_id").size()
        out_amt = tx.groupby("src_id")["amount"].sum()
        in_amt = tx.groupby("dst_id")["amount"].sum()
        mean_amt = tx.groupby("src_id")["amount"].mean()
        max_amt = tx.groupby("src_id")["amount"].max()
        hv = tx[tx["amount"] >= HIGH_VALUE]
        hv_edges = hv.groupby("src_id").size()

        # High-value cycle membership (the laundering-ring signal). Detect cycles only in
        # the high-value subgraph so this stays cheap; decoys make topology alone insufficient
        # (that is where the model + LLM add value).
        in_cycle: set[str] = set()
        if len(hv):
            g = nx.DiGraph()
            g.add_edges_from(hv[["src_id", "dst_id"]].itertuples(index=False, name=None))
            try:
                for cyc in nx.simple_cycles(g, length_bound=7):
                    in_cycle.update(cyc)
            except TypeError:  # older networkx without length_bound
                for cyc in nx.simple_cycles(g):
                    if len(cyc) <= 7:
                        in_cycle.update(cyc)

        rows = []
        for r in acc.itertuples(index=False):
            a = r.account_id
            feat = {
                "out_degree": float(out_deg.get(a, 0)),
                "in_degree": float(in_deg.get(a, 0)),
                "out_amount": float(out_amt.get(a, 0.0)),
                "in_amount": float(in_amt.get(a, 0.0)),
                "mean_amount": float(mean_amt.get(a, 0.0)),
                "max_amount": float(max_amt.get(a, 0.0)),
                "high_value_edges": float(hv_edges.get(a, 0)),
                "in_cycle": 1.0 if a in in_cycle else 0.0,
                "balance": float(r.balance or 0.0),
                "risk_score": float(r.risk_score or 0.0),
            }
            label = 1 if a in fraud_accounts else 0
            rows.append((a, *[feat[f] for f in FEATURES], label, 0.0))

        cols = ["account_id", *FEATURES, "is_labeled_fraud", "xgb_score"]
        coldefs = ", ".join(f"{c} DOUBLE PRECISION" for c in FEATURES)
        pg_exec("DROP TABLE IF EXISTS account_scores CASCADE")
        pg_exec(f"""CREATE TABLE account_scores (
            account_id TEXT PRIMARY KEY, {coldefs},
            is_labeled_fraud INT, xgb_score DOUBLE PRECISION)""")
        pg_insert_rows("account_scores", cols, rows)
        return "ok"

    @task
    def train(_prev: str) -> dict:
        """Train XGBoost (SMOTE), score all accounts, and store metrics."""
        import numpy as np
        import pandas as pd
        from imblearn.over_sampling import SMOTE
        from sklearn.metrics import (f1_score, precision_score, recall_score,
                                     roc_auc_score)
        from xgboost import XGBClassifier

        df = pd.DataFrame(
            pg_query(f"SELECT account_id, {', '.join(FEATURES)}, is_labeled_fraud "
                     f"FROM account_scores"),
            columns=["account_id", *FEATURES, "is_labeled_fraud"])
        X = df[FEATURES].to_numpy(dtype=float)
        y = df["is_labeled_fraud"].to_numpy(dtype=int)

        metrics = {"precision": 0.0, "recall": 0.0, "f1": 0.0, "auc": 0.0,
                   "n_fraud": int(y.sum()), "n_accounts": int(len(y))}
        if y.sum() >= 5 and y.sum() < len(y):
            k = min(5, int(y.sum()) - 1) or 1
            Xr, yr = SMOTE(random_state=42, k_neighbors=k).fit_resample(X, y)
            model = XGBClassifier(n_estimators=200, max_depth=5, learning_rate=0.1,
                                  eval_metric="logloss")
            model.fit(Xr, yr)
            proba = model.predict_proba(X)[:, 1]
            pred = (proba >= 0.5).astype(int)
            metrics.update({
                "precision": round(float(precision_score(y, pred, zero_division=0)), 4),
                "recall": round(float(recall_score(y, pred, zero_division=0)), 4),
                "f1": round(float(f1_score(y, pred, zero_division=0)), 4),
                "auc": round(float(roc_auc_score(y, proba)), 4) if len(set(y)) > 1 else 0.0,
            })
        else:
            # Not enough positives to train — fall back to the ring signal as the score.
            proba = df["in_cycle"].to_numpy(dtype=float) if "in_cycle" in df else np.zeros(len(df))

        for aid, s in zip(df["account_id"], proba):
            pg_exec("UPDATE account_scores SET xgb_score=%s WHERE account_id=%s",
                    (float(s), aid))

        pg_exec("DROP TABLE IF EXISTS model_metrics CASCADE")
        pg_exec("""CREATE TABLE model_metrics (
            precision DOUBLE PRECISION, recall DOUBLE PRECISION, f1 DOUBLE PRECISION,
            auc DOUBLE PRECISION, n_fraud INT, n_accounts INT)""")
        pg_insert_rows("model_metrics",
                       ["precision", "recall", "f1", "auc", "n_fraud", "n_accounts"],
                       [(metrics["precision"], metrics["recall"], metrics["f1"],
                         metrics["auc"], metrics["n_fraud"], metrics["n_accounts"])])
        return metrics

    train(features())


engineer_and_train()
