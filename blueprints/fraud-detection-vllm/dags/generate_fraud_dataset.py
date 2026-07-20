"""
DAG: generate_fraud_dataset

Generate a synthetic fraud graph with SantanderAI/gen-fraud-graph (Apache-2.0) and load it
into PostgreSQL. The dataset can be huge at higher scale — that is exactly why this runs in
Airflow. `SCALE_FACTOR` (default 0.001) keeps the demo small (~10k accounts / ~90k tx).

Tables created: accounts, transactions (normal + fraud edges), fraud_cases (ground truth).

Thanks to SantanderAI/gen-fraud-graph — https://github.com/SantanderAI/gen-fraud-graph
"""
from __future__ import annotations

import glob
import tempfile
from pathlib import Path

import pendulum
from airflow.decorators import dag, task

from common import SCALE_FACTOR, pg_exec, pg_insert_rows


@dag(
    dag_id="generate_fraud_dataset",
    schedule=None,
    start_date=pendulum.datetime(2024, 1, 1, tz="UTC"),
    catchup=False,
    tags=["fraud", "generate"],
)
def generate_fraud_dataset():
    @task
    def generate() -> str:
        """Run gen-fraud-graph into a temp dir; return the output directory."""
        from gen_fraud_graph import Config, FraudGraphGenerator

        out = tempfile.mkdtemp(prefix="fraudgraph_")
        cfg = Config(
            scale_factor=SCALE_FACTOR,
            output_dir=out,
            output_format="csv",
            embedding_provider="fake",
            embedding_dim=8,
            workers=1,
        )
        FraudGraphGenerator(cfg).run()
        return out

    @task
    def load(out: str) -> dict:
        """Create tables and bulk-load accounts / transactions / fraud_cases into Postgres."""
        import pandas as pd

        # (Re)create the schema.
        pg_exec("DROP TABLE IF EXISTS accounts, transactions, fraud_cases, "
                "account_scores, flagged_accounts, model_metrics CASCADE")
        pg_exec("""
            CREATE TABLE accounts (
              account_id TEXT PRIMARY KEY, customer_name TEXT,
              balance DOUBLE PRECISION, risk_score DOUBLE PRECISION, creation_date TEXT)""")
        pg_exec("""
            CREATE TABLE transactions (
              tx_id TEXT, src_id TEXT, dst_id TEXT, amount DOUBLE PRECISION,
              timestamp TEXT, description TEXT, is_fraud_edge INT DEFAULT 0)""")
        pg_exec("""
            CREATE TABLE fraud_cases (
              pattern_id TEXT, start_acc_id TEXT, pattern_type TEXT,
              depth INT, involved_accounts TEXT)""")

        base = Path(out)

        def read_many(pattern, cols):
            files = sorted(glob.glob(str(base / pattern)))
            frames = []
            for fp in files:
                df = pd.read_csv(fp)
                keep = [c for c in cols if c in df.columns]
                frames.append(df[keep])
            return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=cols)

        # Accounts.
        acc = read_many("accounts/*.csv",
                        ["account_id", "customer_name", "balance", "risk_score", "creation_date"])
        pg_insert_rows("accounts",
                       ["account_id", "customer_name", "balance", "risk_score", "creation_date"],
                       list(acc.itertuples(index=False, name=None)))

        # Normal transactions. Note: the rows carry an extra is_fraud_edge value,
        # so the INSERT column list must include it too.
        tx_cols = ["tx_id", "src_id", "dst_id", "amount", "timestamp", "description"]
        tx_insert_cols = tx_cols + ["is_fraud_edge"]
        tx = read_many("transactions/*.csv", tx_cols)
        pg_insert_rows("transactions", tx_insert_cols,
                       [(*r, 0) for r in tx.itertuples(index=False, name=None)])

        # Fraud (labelled) transaction edges.
        ftx = read_many("fraud/transactions_fraud.csv", tx_cols)
        pg_insert_rows("transactions", tx_insert_cols,
                       [(*r, 1) for r in ftx.itertuples(index=False, name=None)])

        # Ground-truth fraud cases (laundering rings).
        fc_cols = ["pattern_id", "start_acc_id", "pattern_type", "depth", "involved_accounts"]
        fc = read_many("fraud/fraud_cases.csv", fc_cols)
        pg_insert_rows("fraud_cases", fc_cols,
                       list(fc.itertuples(index=False, name=None)))

        return {"accounts": len(acc), "transactions": len(tx) + len(ftx), "rings": len(fc)}

    load(generate())


generate_fraud_dataset()
