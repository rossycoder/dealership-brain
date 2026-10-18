"""Seed the graph with DEMO DATA.

IMPORTANT: Every node below is FICTIONAL demo data for the hackathon demo.
Dealership name "Velocity Motors", all customers, vehicles, tickets and
policies are invented for demonstration. The UI labels this clearly.
Run:  python -m app.seed
"""
from app.graph import get_graph, ensure_schema, wipe_graph

DEMO = True  # marker: this file only ever writes fictional demo data


def seed():
    graph, mode = get_graph(auto_seed=False)
    print(f"FalkorDB mode: {mode}")
    if mode == "memory":
        # MemoryGraph has no Cypher engine — reset natively via its own seeder
        # (which mirrors this file's FICTIONAL dataset, incl. service fields).
        graph._nodes.clear()
        graph._rels.clear()
        graph._next_id = 1
        graph._seed()
        print("Seeded (FICTIONAL demo data, memory mode):")
        for label, c in sorted(graph.stats().items()):
            print(f"  {label}: {c}")
        return
    wipe_graph(graph)
    ensure_schema(graph)

    # --- Staff ---
    graph.query("""
        CREATE (:Staff {name:'Sara Khan', role:'sales_manager'}),
               (:Staff {name:'Bilal Ahmed', role:'sales_agent'}),
               (:Staff {name:'Usman Tariq', role:'service_advisor'})
    """)

    # --- Policies (Company Brain knowledge) ---
    graph.query("""
        CREATE (:Policy {topic:'warranty', content:'All new vehicles include a 3-year / 60,000 km warranty. Extended warranty available up to 5 years.'}),
               (:Policy {topic:'return', content:'7-day exchange policy on certified pre-owned vehicles, no questions asked.'}),
               (:Policy {topic:'financing', content:'In-house financing available: 20% down payment, up to 5-year terms, approval within 2 hours.'}),
               (:Policy {topic:'test_drive', content:'Test drives are free and take 30 minutes. Bring your driving license.'})
    """)

    # --- Vehicles (fictional inventory) ---
    graph.query("""
        CREATE (:Vehicle {stock_no:'VM-101', make:'Toyota', model:'Corolla', year:2023, price:2850000, status:'available', colour:'white', body_type:'sedan'}),
               (:Vehicle {stock_no:'VM-102', make:'Honda', model:'Civic', year:2024, price:4200000, status:'available', colour:'red', body_type:'sedan'}),
               (:Vehicle {stock_no:'VM-103', make:'Kia', model:'Sportage', year:2023, price:4950000, status:'available', colour:'black', body_type:'suv'}),
               (:Vehicle {stock_no:'VM-104', make:'Suzuki', model:'Swift', year:2022, price:1950000, status:'available', colour:'silver', body_type:'hatchback'}),
               (:Vehicle {stock_no:'VM-105', make:'Toyota', model:'Fortuner', year:2024, price:8900000, status:'available', colour:'black', body_type:'suv'}),
               (:Vehicle {stock_no:'VM-106', make:'Honda', model:'City', year:2021, price:2400000, status:'sold', colour:'grey', body_type:'sedan'}),
               (:Vehicle {stock_no:'VM-107', make:'Hyundai', model:'Tucson', year:2023, price:4800000, status:'available', colour:'white', body_type:'suv'}),
               (:Vehicle {stock_no:'VM-108', make:'Toyota', model:'Yaris', year:2022, price:2300000, status:'available', colour:'blue', body_type:'sedan'})
    """)

    # --- Customers (fictional) ---
    graph.query("""
        CREATE (:Customer {name:'Ahmed Raza', phone:'+923001111111', budget:3000000, segment:'mid'}),
               (:Customer {name:'Fatima Noor', phone:'+923002222222', budget:5000000, segment:'premium'}),
               (:Customer {name:'Danish Ali', phone:'+923003333333', budget:2000000, segment:'budget'}),
               (:Customer {name:'Ayesha Malik', phone:'+923004444444', budget:9000000, segment:'premium'}),
               (:Customer {name:'Hassan Sheikh', phone:'+923005555555', budget:2500000, segment:'mid'})
    """)

    # --- Relationships: interests, ownership, history ---
    graph.query("""
        MATCH (c:Customer {phone:'+923001111111'}), (v:Vehicle {stock_no:'VM-101'})
        CREATE (c)-[:INTERESTED_IN]->(v)
        """)
    graph.query("""
        MATCH (c:Customer {phone:'+923002222222'}), (v1:Vehicle {stock_no:'VM-103'}), (v2:Vehicle {stock_no:'VM-107'})
        CREATE (c)-[:VIEWED]->(v1), (c)-[:VIEWED]->(v2)
        """)
    graph.query("""
        MATCH (c:Customer {phone:'+923004444444'}), (v:Vehicle {stock_no:'VM-106'})
        CREATE (c)-[:OWNS]->(v)
        """)

    # --- Past conversations (memory across sessions) ---
    graph.query("""
        MATCH (c:Customer {phone:'+923001111111'})
        CREATE (conv:Conversation {timestamp: 1728500000000, summary:'Asked about Corolla financing options', intent:'sales'})
        CREATE (c)-[:HAD]->(conv)
        """)
    graph.query("""
        MATCH (c:Customer {phone:'+923004444444'})
        CREATE (conv:Conversation {timestamp: 1728500001000, summary:'Complained about delayed service appointment', intent:'service'})
        CREATE (c)-[:HAD]->(conv)
        """)

    # --- Tickets (one high-priority open -> churn-risk demo) ---
    graph.query("""
        MATCH (c:Customer {phone:'+923004444444'})
        CREATE (t:Ticket {id:'T-9001', issue:'Service appointment delayed twice', status:'open', priority:'high', stage:'diagnosing', created: 1728500002000})
        CREATE (c)-[:REPORTED]->(t)
        """)
    graph.query("""
        MATCH (c:Customer {phone:'+923003333333'})
        CREATE (t:Ticket {id:'T-9002', issue:'Scratch on delivered Swift bumper', status:'open', priority:'normal', stage:'received', created: 1728500003000})
        CREATE (c)-[:REPORTED]->(t)
        """)

    # --- Callback promises (FICTIONAL): "nobody returns my calls" ---
    import time as _t
    _now = int(_t.time() * 1000)
    _H = 3600 * 1000
    graph.query("""
        CREATE (:Callback {id:'CB-1', customer_phone:'+923001111111', customer_name:'Ahmed Raza',
                           staff:'Usman Tariq', topic:'Service appointment confirmation',
                           promised_at: $p1, created_at: $c1, status:'pending'}),
               (:Callback {id:'CB-2', customer_phone:'+923002222222', customer_name:'Fatima Noor',
                           staff:'Bilal Ahmed', topic:'Test drive follow-up (Sportage)',
                           promised_at: $p2, created_at: $c2, status:'pending'}),
               (:Callback {id:'CB-3', customer_phone:'+923003333333', customer_name:'Danish Ali',
                           staff:'Usman Tariq', topic:'Warranty claim update',
                           promised_at: $p3, created_at: $c3, status:'done',
                           done_at: $d3, done_on_time: true})
        """, params={"p1": _now - 2 * _H, "c1": _now - 5 * _H,
                     "p2": _now + 3 * _H, "c2": _now - 1 * _H,
                     "p3": _now - 26 * _H, "c3": _now - 30 * _H, "d3": _now - 25 * _H})

    # --- Staff promises (FICTIONAL): "promised a spare key, 24 days..." ---
    _D = 24 * _H
    graph.query("""
        CREATE (:Promise {id:'PR-1', customer_phone:'+923004444444', customer_name:'Ayesha Malik',
                          staff:'Bilal Ahmed', promise:'Spare key will be couriered to customer',
                          deadline_at: $d1, created_at: $c1, status:'pending'}),
               (:Promise {id:'PR-2', customer_phone:'+923003333333', customer_name:'Danish Ali',
                          staff:'Usman Tariq', promise:'Bumper scratch repair completed by Friday',
                          deadline_at: $d2, created_at: $c2, status:'pending'})
        """, params={"d1": _now - 5 * _D, "c1": _now - 10 * _D,
                     "d2": _now + 2 * _D, "c2": _now - 1 * _D})

    # --- Service history (FICTIONAL): days since last service per vehicle ---
    import time as _t2
    _now2 = int(_t2.time() * 1000)
    _D2 = 24 * 3600 * 1000
    _svc = {"VM-101": 165, "VM-102": 40, "VM-103": 90, "VM-104": 20,
            "VM-105": 60, "VM-106": 210, "VM-107": 120, "VM-108": 10}
    graph.query("""
        UNWIND keys($svc) AS sn
        MATCH (v:Vehicle {stock_no: sn})
        SET v.last_service_date = $now - $svc[sn] * $day,
            v.service_interval_km = 5000
        """, params={"svc": _svc, "now": _now2, "day": _D2})

    # --- verify ---
    res = graph.query("MATCH (n) RETURN labels(n)[0] AS label, count(n) AS c ORDER BY label")
    print("Seeded (FICTIONAL demo data):")
    for row in res.result_set:
        print(f"  {row[0]}: {row[1]}")


def seed_workspace(graph, mode, dealer_name, owner_name):
    """Starter kit for a NEW SaaS workspace: the owner as staff + the standard
    policy pack. Deliberately NO customers and NO vehicles — a real dealership
    adds its own data. (The fictional dataset lives only in seed(), demo-only.)
    """
    policies = [
        ("warranty", "All new vehicles include a 3-year / 60,000 km warranty. Extended warranty available up to 5 years."),
        ("return", "7-day exchange policy on certified pre-owned vehicles, no questions asked."),
        ("financing", "In-house financing available: 20% down payment, up to 5-year terms, approval within 2 hours."),
        ("test_drive", "Test drives are free and take 30 minutes. Bring your driving license."),
    ]
    if mode == "memory":
        graph._add_node("Staff", {"name": owner_name or "Owner", "role": "owner"})
        for topic, content in policies:
            graph._add_node("Policy", {"topic": topic, "content": content})
        return

    def esc(s):
        return (s or "").replace("'", "\\'")

    graph.query(f"CREATE (:Staff {{name:'{esc(owner_name or 'Owner')}', role:'owner'}})")
    creates = ",\n".join(
        f"(:Policy {{topic:'{t}', content:'{esc(c)}'}})" for t, c in policies)
    graph.query(f"CREATE {creates}")


if __name__ == "__main__":
    seed()
