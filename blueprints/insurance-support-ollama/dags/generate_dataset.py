"""
DAG: generate_dataset

Generate a SYNTHETIC insurance customer-support dataset (stdlib only — no Faker) and
load it into Postgres: customers + households/family links, policies, accident types,
claims (with derived pay/within-policy decisions), and support tickets (with a
realistic status lifecycle). Ticket bodies are templated-then-varied per accident type
so the Milvus "similar case" search returns genuinely related precedents.

Uses only Python stdlib + psycopg2 (psycopg2 ships in the AppCo Airflow image, which
already uses Postgres for its own metadata) — so it runs on the STOCK apache-airflow
image, no custom image needed.

Volume is controlled by the N_TICKETS env (default 400). Re-runnable: drops + recreates
the tables each run. Trigger from the Airflow UI (or schedule).
"""
from __future__ import annotations

import random
from datetime import date, datetime, timedelta

import pendulum
from airflow.decorators import dag, task

from common import N_TICKETS, pg_exec, pg_insert_rows

SEED = 42
BASE = date(2026, 7, 1)  # fixed reference so runs are reproducible with SEED

PRODUCTS = ["auto", "home", "health", "travel"]

FIRST_NAMES = ["Marco", "Giulia", "Luca", "Sofia", "Andrea", "Elena", "Matteo", "Chiara",
               "Francesco", "Anna", "Davide", "Sara", "Alessandro", "Martina", "Paolo",
               "Valentina", "Simone", "Federica", "Giorgio", "Laura"]
LAST_NAMES = ["Rossi", "Bianchi", "Ferrari", "Russo", "Romano", "Gallo", "Costa", "Fontana",
              "Conti", "Esposito", "Ricci", "Bruno", "Greco", "Marino", "Rizzo", "Moretti"]
CITIES = ["Turin", "Milan", "Rome", "Naples", "Bologna", "Florence", "Genoa", "Verona",
          "Venice", "Palermo", "Bari", "Padua"]
STREETS = ["Via Roma", "Via Garibaldi", "Corso Italia", "Via Dante", "Viale Europa",
           "Via Marconi", "Piazza Duomo", "Via Verdi", "Corso Vittorio", "Via Milano"]

# accident_type -> (product, base_severity 1-5, covered_by_default)
ACCIDENT_TYPES = {
    "collision": ("auto", 4, True),
    "windshield": ("auto", 2, True),
    "theft": ("auto", 4, True),
    "vandalism": ("auto", 2, True),
    "water_damage": ("home", 4, True),
    "fire": ("home", 5, True),
    "burglary": ("home", 3, True),
    "storm_damage": ("home", 3, True),
    "hospitalization": ("health", 5, True),
    "outpatient": ("health", 2, True),
    "dental": ("health", 1, False),      # often excluded -> interesting denials
    "trip_cancellation": ("travel", 2, True),
    "lost_luggage": ("travel", 1, True),
    "medical_abroad": ("travel", 4, True),
}

BODY_TEMPLATES = {
    "collision": [
        "I was involved in a car accident at {place}. The front of my vehicle is damaged and I need to file a claim.",
        "Another driver hit my car at {place}. There is significant damage to the bumper and headlights.",
    ],
    "windshield": [
        "A stone cracked my windshield on the motorway near {place}. Can this be repaired under my policy?",
        "My windscreen chipped and the crack is spreading. I'd like to arrange a repair.",
    ],
    "theft": [
        "My car was stolen from {place} last night. I have already reported it to the police.",
        "Someone broke into my vehicle at {place} and stole belongings. What does my policy cover?",
    ],
    "vandalism": [
        "My car was keyed and a mirror broken while parked at {place}.",
        "Someone vandalised my vehicle overnight near {place}; the paintwork is scratched.",
    ],
    "water_damage": [
        "A burst pipe flooded my kitchen at {place}. Floor and cabinets are damaged.",
        "Heavy rain caused water to leak through the roof and damage the ceiling.",
    ],
    "fire": [
        "There was a small kitchen fire at my home in {place}; smoke damaged the walls.",
        "An electrical fault started a fire in the garage. I need to claim for the damage.",
    ],
    "burglary": [
        "My home in {place} was burgled and electronics were taken.",
        "Someone broke in through a window and stole jewellery and a laptop.",
    ],
    "storm_damage": [
        "A storm blew tiles off my roof in {place} and water is getting in.",
        "High winds knocked a tree onto my fence and shed.",
    ],
    "hospitalization": [
        "I was hospitalised for three days and want to claim the treatment costs.",
        "My spouse was admitted to hospital after an emergency; how do I file a claim?",
    ],
    "outpatient": [
        "I had an outpatient procedure and would like to be reimbursed.",
        "I visited a specialist and paid out of pocket; can I claim this back?",
    ],
    "dental": [
        "I had a dental treatment and would like to know if it is covered.",
        "My dentist bill was expensive; is dental included in my health policy?",
    ],
    "trip_cancellation": [
        "I had to cancel my trip to {place} due to illness and lost the flight cost.",
        "My holiday was cancelled and I want to claim the non-refundable bookings.",
    ],
    "lost_luggage": [
        "The airline lost my luggage on the way to {place}. How do I claim?",
        "My suitcase never arrived; I need to claim for the lost contents.",
    ],
    "medical_abroad": [
        "I fell ill while travelling in {place} and needed medical treatment abroad.",
        "I had an accident on holiday in {place} and paid for a hospital visit.",
    ],
}

TICKET_STATUSES = ["open", "in_progress", "pending", "resolved", "closed"]
PRIORITIES = ["low", "medium", "high", "urgent"]
RELATIONSHIPS = ["spouse", "child", "parent", "sibling"]


def _rand_date(start: date, end: date) -> date:
    span = max(0, (end - start).days)
    return start + timedelta(days=random.randint(0, span))


def _rand_dt(start: date, end: date) -> datetime:
    d = _rand_date(start, end)
    return datetime(d.year, d.month, d.day, random.randint(0, 23), random.randint(0, 59))


@dag(
    dag_id="generate_dataset",
    schedule=None,
    start_date=pendulum.datetime(2024, 1, 1, tz="UTC"),
    catchup=False,
    tags=["insurance-support", "synthetic-data"],
)
def generate_dataset():
    @task
    def create_schema():
        pg_exec("""
        DROP TABLE IF EXISTS support_tickets, claims, policies,
            family_relationships, customers, households, accident_types CASCADE;

        CREATE TABLE households (
            household_id  INT PRIMARY KEY,
            address       TEXT, city TEXT, postal_code TEXT
        );
        CREATE TABLE customers (
            customer_id   INT PRIMARY KEY,
            household_id  INT REFERENCES households(household_id),
            first_name    TEXT, last_name TEXT, dob DATE,
            email         TEXT, phone TEXT
        );
        CREATE TABLE family_relationships (
            customer_id         INT REFERENCES customers(customer_id),
            related_customer_id INT REFERENCES customers(customer_id),
            relationship_type   TEXT
        );
        CREATE TABLE accident_types (
            accident_type_id SERIAL PRIMARY KEY,
            name             TEXT UNIQUE, product_type TEXT,
            base_severity    INT, covered BOOLEAN
        );
        CREATE TABLE policies (
            policy_id      INT PRIMARY KEY,
            policy_number  TEXT UNIQUE,
            customer_id    INT REFERENCES customers(customer_id),
            product_type   TEXT, coverage_limit NUMERIC, deductible NUMERIC,
            premium        NUMERIC, start_date DATE, end_date DATE, status TEXT
        );
        CREATE TABLE claims (
            claim_id        INT PRIMARY KEY,
            policy_id       INT REFERENCES policies(policy_id),
            customer_id     INT REFERENCES customers(customer_id),
            accident_type   TEXT, incident_date DATE, filed_date DATE,
            description     TEXT, estimated_amount NUMERIC, paid_amount NUMERIC,
            was_paid        BOOLEAN, within_policy BOOLEAN, decision_reason TEXT
        );
        CREATE TABLE support_tickets (
            ticket_id       INT PRIMARY KEY,
            claim_id        INT REFERENCES claims(claim_id),
            customer_id     INT REFERENCES customers(customer_id),
            policy_id       INT REFERENCES policies(policy_id),
            subject         TEXT, body TEXT, channel TEXT, priority TEXT, status TEXT,
            assigned_agent  TEXT, created_at TIMESTAMP, updated_at TIMESTAMP,
            resolved_at     TIMESTAMP, closed_at TIMESTAMP, resolution_notes TEXT
        );
        """)
        pg_insert_rows("accident_types", ["name", "product_type", "base_severity", "covered"],
                       [(n, p, s, c) for n, (p, s, c) in ACCIDENT_TYPES.items()])
        return "schema ready"

    @task
    def generate(_prev: str):
        random.seed(SEED)
        n_tickets = max(20, N_TICKETS)
        n_households = max(5, n_tickets // 4)

        households, customers, rels = [], [], []
        cust_id = 0
        for hh in range(1, n_households + 1):
            households.append((hh, f"{random.randint(1, 200)} {random.choice(STREETS)}",
                               random.choice(CITIES), f"{random.randint(10000, 99999)}"))
            last = random.choice(LAST_NAMES)
            ids = []
            for _ in range(random.randint(1, 4)):
                cust_id += 1
                first = random.choice(FIRST_NAMES)
                dob = _rand_date(date(1945, 1, 1), date(2006, 1, 1))
                email = f"{first}.{last}{cust_id}@example.com".lower()
                phone = f"+39 0{random.randint(10, 99)} {random.randint(100, 999)} {random.randint(1000, 9999)}"
                customers.append((cust_id, hh, first, last, dob, email, phone))
                ids.append(cust_id)
            for i in range(len(ids)):
                for j in range(i + 1, len(ids)):
                    rels.append((ids[i], ids[j], random.choice(RELATIONSHIPS)))

        pg_insert_rows("households", ["household_id", "address", "city", "postal_code"], households)
        pg_insert_rows("customers",
                       ["customer_id", "household_id", "first_name", "last_name", "dob", "email", "phone"],
                       customers)
        pg_insert_rows("family_relationships",
                       ["customer_id", "related_customer_id", "relationship_type"], rels)

        policies = []
        pol_id = 0
        pol_by_customer: dict[int, list[tuple]] = {}
        for c in customers:
            cid = c[0]
            for _ in range(random.randint(1, 2)):
                pol_id += 1
                product = random.choice(PRODUCTS)
                start = _rand_date(BASE - timedelta(days=3 * 365), BASE - timedelta(days=365))
                end = _rand_date(BASE, BASE + timedelta(days=365))
                pol = (pol_id, f"POL-{pol_id:06d}", cid, product,
                       random.choice([5000, 10000, 25000, 50000, 100000]),
                       random.choice([0, 100, 250, 500, 1000]),
                       round(random.uniform(200, 2000), 2), start, end,
                       random.choices(["active", "lapsed"], weights=[85, 15])[0])
                policies.append(pol)
                pol_by_customer.setdefault(cid, []).append(pol)
        pg_insert_rows("policies",
                       ["policy_id", "policy_number", "customer_id", "product_type",
                        "coverage_limit", "deductible", "premium", "start_date", "end_date", "status"],
                       policies)

        claims, tickets = [], []
        claim_id = 0
        acc_names = list(ACCIDENT_TYPES.keys())
        for tid in range(1, n_tickets + 1):
            cust = random.choice(customers)
            cid = cust[0]
            cust_pols = pol_by_customer.get(cid, [])
            if not cust_pols:
                continue
            pol = random.choice(cust_pols)
            product = pol[3]
            candidates = [a for a in acc_names if ACCIDENT_TYPES[a][0] == product] or acc_names
            acc = random.choice(candidates)
            _, _severity, covered = ACCIDENT_TYPES[acc]
            place = random.choice(CITIES)

            incident = _rand_date(BASE - timedelta(days=365), BASE)
            estimated = round(random.uniform(200, float(pol[4]) * 1.2), 2)
            deductible = float(pol[5])
            limit = float(pol[4])
            in_dates = pol[7] <= incident <= pol[8]  # pol[7]=start_date, pol[8]=end_date
            within_policy = bool(covered and in_dates and estimated <= limit)
            was_paid = bool(within_policy and estimated > deductible and random.random() > 0.08)
            paid = round(min(estimated, limit) - deductible, 2) if was_paid else 0.0
            if within_policy and not was_paid:
                reason = "within policy but denied (documentation pending / fraud review)"
            elif not covered:
                reason = f"{acc} not covered by {product} policy"
            elif not in_dates:
                reason = "incident outside policy validity dates"
            elif estimated > limit:
                reason = "estimated amount exceeds coverage limit"
            else:
                reason = "approved and paid within coverage"

            body = random.choice(BODY_TEMPLATES[acc]).format(place=place)
            claim_id += 1
            claims.append((claim_id, pol[0], cid, acc, incident, incident, body,
                           estimated, paid, was_paid, within_policy, reason))

            status = random.choices(TICKET_STATUSES, weights=[20, 20, 10, 20, 30])[0]
            created = _rand_dt(BASE - timedelta(days=365), BASE)
            resolved = created if status in ("resolved", "closed") else None
            closed = created if status == "closed" else None
            res_notes = None
            if status in ("resolved", "closed"):
                res_notes = (f"Claim {'approved' if was_paid else 'denied'}: {reason}."
                             + (f" Paid {paid:.2f}." if was_paid else ""))
            subject = f"{acc.replace('_', ' ').title()} - {product} claim"
            agent = f"{random.choice(FIRST_NAMES)} {random.choice(LAST_NAMES)}"
            tickets.append((tid, claim_id, cid, pol[0], subject, body,
                            random.choice(["email", "phone", "web", "chat"]),
                            random.choices(PRIORITIES, weights=[30, 40, 20, 10])[0],
                            status, agent, created, created, resolved, closed, res_notes))

        pg_insert_rows("claims",
                       ["claim_id", "policy_id", "customer_id", "accident_type", "incident_date",
                        "filed_date", "description", "estimated_amount", "paid_amount",
                        "was_paid", "within_policy", "decision_reason"], claims)
        pg_insert_rows("support_tickets",
                       ["ticket_id", "claim_id", "customer_id", "policy_id", "subject", "body",
                        "channel", "priority", "status", "assigned_agent", "created_at",
                        "updated_at", "resolved_at", "closed_at", "resolution_notes"], tickets)
        return {"households": len(households), "customers": len(customers),
                "policies": len(policies), "claims": len(claims), "tickets": len(tickets)}

    generate(create_schema())


generate_dataset()
