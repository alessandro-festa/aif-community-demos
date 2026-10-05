# Fraud/AML blueprint Airflow image — FROM the SUSE Application Collection base
# (hardened, ships without pip) with the DAG's Python deps baked in. The base is
# bootstrapped with ensurepip, then deps are installed into system site-packages
# so they import for the airflow user at runtime. gen-fraud-graph is installed
# from its release zip (no git needed).
FROM dp.apps.rancher.io/containers/apache-airflow:3.2.2-8.14
USER root
RUN python -m ensurepip --upgrade \
 && python -m pip install --no-cache-dir \
      pandas numpy networkx scikit-learn xgboost imbalanced-learn psycopg2-binary requests \
      "https://github.com/SantanderAI/gen-fraud-graph/archive/refs/tags/v0.1.0.zip" \
 && python -c "import pandas, numpy, networkx, sklearn, xgboost, imblearn, psycopg2, gen_fraud_graph; print('deps OK')"
USER 999
