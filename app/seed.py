"""Seed the graph with DEMO DATA.

IMPORTANT: Every node below is FICTIONAL demo data for the hackathon demo.
Dealership name "Velocity Motors", all customers, vehicles, tickets and
policies are invented for demonstration. The UI labels this clearly.
Run:  python -m app.seed
"""
from app.graph import get_graph, ensure_schema, wipe_graph

DEMO = True  # marker: this file only ever writes fictional demo data


def seed():
    graph, mode = get_graph()
    print(f"FalkorDB mode: {mode}")
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
        CREATE (:Vehicle {stock_no:'VM-101', make:'Toyota', model:'Corolla', year:2023, price:2850000, status:'available'}),
               (:Vehicle {stock_no:'VM-102', make:'Honda', model:'Civic', year:2024, price:4200000, status:'available'}),
               (:Vehicle {stock_no:'VM-103', make:'Kia', model:'Sportage', year:2023, price:5100000, status:'available'}),
               (:Vehicle {stock_no:'VM-104', make:'Suzuki', model:'Swift', year:2022, price:1950000, status:'available'}),
               (:Vehicle {stock_no:'VM-105', make:'Toyota', model:'Fortuner', year:2024, price:8900000, status:'available'}),
               (:Vehicle {stock_no:'VM-106', make:'Honda', model:'City', year:2021, price:2400000, status:'sold'}),
               (:Vehicle {stock_no:'VM-107', make:'Hyundai', model:'Tucson', year:2023, price:4800000, status:'available'}),
               (:Vehicle {stock_no:'VM-108', make:'Toyota', model:'Yaris', year:2022, price:2300000, status:'available'})
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
        CREATE (t:Ticket {id:'T-9001', issue:'Service appointment delayed twice', status:'open', priority:'high', created: 1728500002000})
        CREATE (c)-[:REPORTED]->(t)
        """)
    graph.query("""
        MATCH (c:Customer {phone:'+923003333333'})
        CREATE (t:Ticket {id:'T-9002', issue:'Scratch on delivered Swift bumper', status:'open', priority:'normal', created: 1728500003000})
        CREATE (c)-[:REPORTED]->(t)
        """)

    # --- verify ---
    res = graph.query("MATCH (n) RETURN labels(n)[0] AS label, count(n) AS c ORDER BY label")
    print("Seeded (FICTIONAL demo data):")
    for row in res.result_set:
        print(f"  {row[0]}: {row[1]}")


if __name__ == "__main__":
    seed()
