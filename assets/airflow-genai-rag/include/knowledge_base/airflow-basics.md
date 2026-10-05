# Apache Airflow Basics

Apache Airflow is an open-source platform to programmatically author, schedule, and
monitor workflows. Workflows are defined as Directed Acyclic Graphs (DAGs) written
in Python, where each node is a task and edges express dependencies between tasks.

A DAG describes *what* to run and *in what order*; it does not perform the work
itself. The Airflow scheduler parses DAG files, decides which task instances are
ready to run based on their dependencies and schedule, and hands them to an
executor. The metadata database records the state of every DAG run and task
instance, which is what powers the web UI's grid and graph views.

Tasks are created from operators. Common operators include the PythonOperator (run
a Python callable), the BashOperator (run a shell command), and the
KubernetesPodOperator (run a container as a pod). The TaskFlow API, using the
`@dag` and `@task` decorators, lets you write plain Python functions that Airflow
turns into tasks, passing return values between them as XComs automatically.

Scheduling is controlled by the `schedule` argument. Setting `schedule=None` makes a
DAG manual-trigger only, which is ideal for pipelines you kick off on demand. The
`catchup` flag controls whether Airflow backfills missed runs between the DAG's
start date and now; for demo pipelines it is usually set to `False`.
