"""In-memory graph fallback for serverless (Vercel).

When redislite/FalkorDB cannot run (e.g. Vercel serverless blocks the
redis-server binary), the app falls back to this pure-Python store so the
demo still works. Same data model, same helper-function interface.

All data is FICTIONAL demo data (mirrors app/seed.py).
"""
import time


class MemoryGraph:
    """Pure-Python property graph. Nodes are dicts: {id, labels, props}.
    Relationships are dicts: {from_id, to_id, type}."""

    def __init__(self):
        self._nodes = {}  # id -> {labels: [...], props: {...}}
        self._rels = []   # [{from, to, type}]
        self._next_id = 1
        self._seed()

    def _add_node(self, labels, props):
        nid = self._next_id
        self._next_id += 1
        self._nodes[nid] = {"labels": labels if isinstance(labels, list) else [labels],
                            "props": dict(props)}
        return nid

    def _add_rel(self, from_id, to_id, rel_type):
        self._rels.append({"from": from_id, "to": to_id, "type": rel_type})

    def _find(self, label=None, **props):
        out = []
        for nid, n in self._nodes.items():
            if label and label not in n["labels"]:
                continue
            if all(n["props"].get(k) == v for k, v in props.items()):
                out.append((nid, n))
        return out

    def _related(self, nid, rel_type, direction="out", label=None):
        out = []
        for r in self._rels:
            if direction == "out" and r["from"] != nid:
                continue
            if direction == "in" and r["to"] != nid:
                continue
            if r["type"] != rel_type:
                continue
            other = r["to"] if direction == "out" else r["from"]
            n = self._nodes.get(other)
            if n and (not label or label in n["labels"]):
                out.append((other, n))
        return out

    # -- public API used by graph.py helpers ---------------------------------

    def find_customer_by_phone(self, phone):
        found = self._find("Customer", phone=phone)
        if not found:
            return None
        _, n = found[0]
        p = n["props"]
        return {"name": p.get("name"), "budget": p.get("budget"),
                "segment": p.get("segment"), "phone": phone}

    def customer_history(self, phone, limit=5):
        found = self._find("Customer", phone=phone)
        if not found:
            return {"conversations": [], "tickets": []}
        nid, _ = found[0]
        convs = []
        for _, n in self._related(nid, "HAD", "out", "Conversation"):
            p = n["props"]
            convs.append({"timestamp": p.get("timestamp"), "summary": p.get("summary"),
                          "intent": p.get("intent")})
        convs.sort(key=lambda c: c["timestamp"] or 0, reverse=True)
        tickets = []
        for _, n in self._related(nid, "REPORTED", "out", "Ticket"):
            p = n["props"]
            tickets.append({"id": p.get("id"), "issue": p.get("issue"),
                            "status": p.get("status"), "priority": p.get("priority")})
        return {"conversations": convs[:limit], "tickets": tickets[:limit]}

    def vehicles_in_budget(self, budget, limit=5):
        out = []
        for _, n in self._find("Vehicle"):
            p = n["props"]
            if p.get("status") == "available" and (p.get("price") or 0) <= budget:
                out.append({"stock_no": p.get("stock_no"), "make": p.get("make"),
                            "model": p.get("model"), "year": p.get("year"),
                            "price": p.get("price")})
        out.sort(key=lambda v: v["price"] or 0, reverse=True)
        return out[:limit]

    def find_policy(self, keyword):
        kw = (keyword or "").lower()
        out = []
        for _, n in self._find("Policy"):
            p = n["props"]
            topic = str(p.get("topic", "")).lower()
            content = str(p.get("content", "")).lower()
            if kw in topic or kw in content:
                out.append({"topic": p.get("topic"), "content": p.get("content")})
        return out[:3]

    def churn_risk_customers(self, limit=5):
        out = []
        for nid, n in self._find("Customer"):
            p = n["props"]
            open_high = 0
            for _, t in self._related(nid, "REPORTED", "out", "Ticket"):
                tp = t["props"]
                if tp.get("status") != "resolved" and tp.get("priority") == "high":
                    open_high += 1
            if open_high > 0:
                out.append({"name": p.get("name"), "phone": p.get("phone"),
                            "segment": p.get("segment"), "open_tickets": open_high})
        out.sort(key=lambda c: c["open_tickets"], reverse=True)
        return out[:limit]

    def record_conversation(self, phone, name, summary, intent):
        found = self._find("Customer", phone=phone)
        if found:
            nid, _ = found[0]
        else:
            nid = self._add_node("Customer", {"phone": phone, "name": name, "segment": "new"})
        cid = self._add_node("Conversation", {"timestamp": int(time.time() * 1000),
                                              "summary": summary, "intent": intent})
        self._add_rel(nid, cid, "HAD")

    def create_ticket(self, phone, issue, priority="normal"):
        found = self._find("Customer", phone=phone)
        if not found:
            return None
        nid, _ = found[0]
        tid = f"T-{int(time.time() * 1000)}"
        t = self._add_node("Ticket", {"id": tid, "issue": issue, "status": "open",
                                     "priority": priority, "created": int(time.time() * 1000)})
        self._add_rel(nid, t, "REPORTED")
        return tid

    def stats(self):
        counts = {}
        for _, n in self._nodes.items():
            label = n["labels"][0] if n["labels"] else "?"
            counts[label] = counts.get(label, 0) + 1
        return counts

    def count(self):
        return len(self._nodes)

    # -- seed (FICTIONAL demo data, mirrors app/seed.py) -----------------------

    def _seed(self):
        self._add_node("Staff", {"name": "Sara Khan", "role": "sales_manager"})
        self._add_node("Staff", {"name": "Bilal Ahmed", "role": "sales_agent"})
        self._add_node("Staff", {"name": "Usman Tariq", "role": "service_advisor"})

        for topic, content in [
            ("warranty", "All new vehicles include a 3-year / 60,000 km warranty. Extended warranty available up to 5 years."),
            ("return", "7-day exchange policy on certified pre-owned vehicles, no questions asked."),
            ("financing", "In-house financing available: 20% down payment, up to 5-year terms, approval within 2 hours."),
            ("test_drive", "Test drives are free and take 30 minutes. Bring your driving license."),
        ]:
            self._add_node("Policy", {"topic": topic, "content": content})

        vehicles = [
            ("VM-101", "Toyota", "Corolla", 2023, 2850000, "available"),
            ("VM-102", "Honda", "Civic", 2024, 4200000, "available"),
            ("VM-103", "Kia", "Sportage", 2023, 5100000, "available"),
            ("VM-104", "Suzuki", "Swift", 2022, 1950000, "available"),
            ("VM-105", "Toyota", "Fortuner", 2024, 8900000, "available"),
            ("VM-106", "Honda", "City", 2021, 2400000, "sold"),
            ("VM-107", "Hyundai", "Tucson", 2023, 4800000, "available"),
            ("VM-108", "Toyota", "Yaris", 2022, 2300000, "available"),
        ]
        vids = {}
        for stock_no, make, model, year, price, status in vehicles:
            vids[stock_no] = self._add_node("Vehicle", {"stock_no": stock_no, "make": make,
                                                       "model": model, "year": year,
                                                       "price": price, "status": status})

        customers = [
            ("Ahmed Raza", "+923001111111", 3000000, "mid"),
            ("Fatima Noor", "+923002222222", 5000000, "premium"),
            ("Danish Ali", "+923003333333", 2000000, "budget"),
            ("Ayesha Malik", "+923004444444", 9000000, "premium"),
            ("Hassan Sheikh", "+923005555555", 2500000, "mid"),
        ]
        cids = {}
        for name, phone, budget, segment in customers:
            cids[phone] = self._add_node("Customer", {"name": name, "phone": phone,
                                                     "budget": budget, "segment": segment})

        self._add_rel(cids["+923001111111"], vids["VM-101"], "INTERESTED_IN")
        self._add_rel(cids["+923002222222"], vids["VM-103"], "VIEWED")
        self._add_rel(cids["+923002222222"], vids["VM-107"], "VIEWED")
        self._add_rel(cids["+923004444444"], vids["VM-106"], "OWNS")

        c1 = self._add_node("Conversation", {"timestamp": 1728500000000,
                                             "summary": "Asked about Corolla financing options",
                                             "intent": "sales"})
        self._add_rel(cids["+923001111111"], c1, "HAD")
        c2 = self._add_node("Conversation", {"timestamp": 1728500001000,
                                             "summary": "Complained about delayed service appointment",
                                             "intent": "service"})
        self._add_rel(cids["+923004444444"], c2, "HAD")

        t1 = self._add_node("Ticket", {"id": "T-9001", "issue": "Service appointment delayed twice",
                                       "status": "open", "priority": "high",
                                       "created": 1728500002000})
        self._add_rel(cids["+923004444444"], t1, "REPORTED")
        t2 = self._add_node("Ticket", {"id": "T-9002", "issue": "Scratch on delivered Swift bumper",
                                       "status": "open", "priority": "normal",
                                       "created": 1728500003000})
        self._add_rel(cids["+923003333333"], t2, "REPORTED")
