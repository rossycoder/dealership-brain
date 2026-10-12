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

    def vehicles_in_budget(self, budget, limit=5, body_type=None, colour=None):
        out = []
        for _, n in self._find("Vehicle"):
            p = n["props"]
            if p.get("status") != "available":
                continue
            if (p.get("price") or 0) > budget:
                continue
            if body_type and (p.get("body_type") or "").lower() != body_type.lower():
                continue
            if colour and (p.get("colour") or "").lower() != colour.lower():
                continue
            out.append({"stock_no": p.get("stock_no"), "make": p.get("make"),
                        "model": p.get("model"), "year": p.get("year"),
                        "price": p.get("price"), "colour": p.get("colour"),
                        "body_type": p.get("body_type")})
        out.sort(key=lambda v: v["price"] or 0, reverse=True)
        return out[:limit]

    # -- callbacks / promises / ticket stages (new features) ------------------

    def add_callback(self, customer_phone, customer_name, staff, topic, promised_at):
        cb_id = f"CB-{int(time.time() * 1000)}"
        self._add_node("Callback", {"id": cb_id, "customer_phone": customer_phone,
                                    "customer_name": customer_name, "staff": staff,
                                    "topic": topic, "promised_at": promised_at,
                                    "created_at": int(time.time() * 1000),
                                    "status": "pending"})
        return cb_id

    def list_callbacks(self):
        now = int(time.time() * 1000)
        out = []
        for _, n in self._find("Callback"):
            p = dict(n["props"])
            if p.get("status") == "pending" and (p.get("promised_at") or 0) < now:
                p["overdue"] = True
                p["overdue_ms"] = now - p["promised_at"]
            else:
                p["overdue"] = False
                p["due_in_ms"] = max(0, (p.get("promised_at") or 0) - now)
            out.append(p)
        out.sort(key=lambda c: (c["status"] != "pending", c.get("promised_at") or 0))
        return out

    def complete_callback(self, cb_id):
        found = self._find("Callback", id=cb_id)
        if not found:
            return None
        _, n = found[0]
        now = int(time.time() * 1000)
        n["props"]["status"] = "done"
        n["props"]["done_at"] = now
        n["props"]["done_on_time"] = now <= (n["props"].get("promised_at") or 0)
        return dict(n["props"])

    def callback_stats(self):
        cbs = self.list_callbacks()
        total = len(cbs)
        done = [c for c in cbs if c["status"] == "done"]
        on_time = [c for c in done if c.get("done_on_time")]
        overdue_now = [c for c in cbs if c.get("overdue")]
        return {"total": total, "done": len(done),
                "on_time": len(on_time),
                "compliance_pct": round(100 * len(on_time) / len(done), 1) if done else 100.0,
                "overdue_now": len(overdue_now)}

    def add_promise(self, customer_phone, customer_name, staff, promise, deadline_at):
        pr_id = f"PR-{int(time.time() * 1000)}"
        self._add_node("Promise", {"id": pr_id, "customer_phone": customer_phone,
                                   "customer_name": customer_name, "staff": staff,
                                   "promise": promise, "deadline_at": deadline_at,
                                   "created_at": int(time.time() * 1000),
                                   "status": "pending"})
        return pr_id

    def list_promises(self):
        now = int(time.time() * 1000)
        out = []
        for _, n in self._find("Promise"):
            p = dict(n["props"])
            if p.get("status") == "pending" and (p.get("deadline_at") or 0) < now:
                p["overdue"] = True
                p["overdue_ms"] = now - p["deadline_at"]
            else:
                p["overdue"] = False
                p["due_in_ms"] = max(0, (p.get("deadline_at") or 0) - now)
            out.append(p)
        out.sort(key=lambda c: (c["status"] != "pending", c.get("deadline_at") or 0))
        return out

    def complete_promise(self, pr_id):
        found = self._find("Promise", id=pr_id)
        if not found:
            return None
        _, n = found[0]
        n["props"]["status"] = "done"
        n["props"]["done_at"] = int(time.time() * 1000)
        return dict(n["props"])

    TICKET_STAGES = ["received", "diagnosing", "parts_ordered",
                     "in_progress", "quality_check", "ready"]

    STAGE_MESSAGES = {
        "received": "Hi {name}, we've received your car for: {issue}. Ticket {id}. We'll keep you posted at every step. — Velocity Motors",
        "diagnosing": "Hi {name}, our technicians are now diagnosing your car (Ticket {id}). We'll update you once we know more. — Velocity Motors",
        "parts_ordered": "Hi {name}, we've ordered the parts needed for your car (Ticket {id}). We'll notify you when they arrive. — Velocity Motors",
        "in_progress": "Hi {name}, repair work on your car has started (Ticket {id}). Our team is on it. — Velocity Motors",
        "quality_check": "Hi {name}, your car (Ticket {id}) is in final quality check. Almost ready! — Velocity Motors",
        "ready": "Hi {name}, great news! Your car is ready for pickup (Ticket {id}). Please visit us at your convenience. — Velocity Motors",
    }

    def list_tickets(self):
        out = []
        for nid, n in self._nodes.items():
            if "Ticket" not in n["labels"]:
                continue
            p = dict(n["props"])
            # find owning customer
            owner = {"name": "", "phone": ""}
            for r in self._rels:
                if r["type"] == "REPORTED" and r["to"] == nid:
                    cn = self._nodes.get(r["from"])
                    if cn:
                        owner = {"name": cn["props"].get("name", ""),
                                 "phone": cn["props"].get("phone", "")}
                        break
            p["customer_name"] = owner["name"]
            p["customer_phone"] = owner["phone"]
            p.setdefault("stage", "received")
            out.append(p)
        return out

    def advance_ticket(self, ticket_id):
        found = self._find("Ticket", id=ticket_id)
        if not found:
            return None
        nid, n = found[0]
        stages = self.TICKET_STAGES
        cur = n["props"].get("stage") or "received"
        try:
            idx = stages.index(cur)
        except ValueError:
            idx = 0
        nxt = stages[min(idx + 1, len(stages) - 1)]
        n["props"]["stage"] = nxt
        if nxt == "ready":
            n["props"]["status"] = "resolved"
        # customer name for the message
        name = "there"
        for r in self._rels:
            if r["type"] == "REPORTED" and r["to"] == nid:
                cn = self._nodes.get(r["from"])
                if cn:
                    name = cn["props"].get("name", "there")
                break
        msg = self.STAGE_MESSAGES[nxt].format(
            name=name, issue=n["props"].get("issue", ""), id=ticket_id)
        return {"id": ticket_id, "stage": nxt, "stages": stages,
                "customer_message": msg}

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
                                     "priority": priority, "stage": "received",
                                     "created": int(time.time() * 1000)})
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
            ("VM-101", "Toyota", "Corolla", 2023, 2850000, "available", "white", "sedan"),
            ("VM-102", "Honda", "Civic", 2024, 4200000, "available", "red", "sedan"),
            ("VM-103", "Kia", "Sportage", 2023, 4950000, "available", "black", "suv"),
            ("VM-104", "Suzuki", "Swift", 2022, 1950000, "available", "silver", "hatchback"),
            ("VM-105", "Toyota", "Fortuner", 2024, 8900000, "available", "black", "suv"),
            ("VM-106", "Honda", "City", 2021, 2400000, "sold", "grey", "sedan"),
            ("VM-107", "Hyundai", "Tucson", 2023, 4800000, "available", "white", "suv"),
            ("VM-108", "Toyota", "Yaris", 2022, 2300000, "available", "blue", "sedan"),
        ]
        vids = {}
        for stock_no, make, model, year, price, status, colour, body_type in vehicles:
            vids[stock_no] = self._add_node("Vehicle", {"stock_no": stock_no, "make": make,
                                                       "model": model, "year": year,
                                                       "price": price, "status": status,
                                                       "colour": colour, "body_type": body_type})

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
                                       "stage": "diagnosing",
                                       "created": 1728500002000})
        self._add_rel(cids["+923004444444"], t1, "REPORTED")
        t2 = self._add_node("Ticket", {"id": "T-9002", "issue": "Scratch on delivered Swift bumper",
                                       "status": "open", "priority": "normal",
                                       "stage": "received",
                                       "created": 1728500003000})
        self._add_rel(cids["+923003333333"], t2, "REPORTED")

        # --- Callback promises (FICTIONAL): "nobody returns my calls" ----------
        now = int(time.time() * 1000)
        H = 3600 * 1000
        self._add_node("Callback", {"id": "CB-1", "customer_phone": "+923001111111",
                                    "customer_name": "Ahmed Raza", "staff": "Usman Tariq",
                                    "topic": "Service appointment confirmation",
                                    "promised_at": now - 2 * H, "created_at": now - 5 * H,
                                    "status": "pending"})
        self._add_node("Callback", {"id": "CB-2", "customer_phone": "+923002222222",
                                    "customer_name": "Fatima Noor", "staff": "Bilal Ahmed",
                                    "topic": "Test drive follow-up (Sportage)",
                                    "promised_at": now + 3 * H, "created_at": now - 1 * H,
                                    "status": "pending"})
        self._add_node("Callback", {"id": "CB-3", "customer_phone": "+923003333333",
                                    "customer_name": "Danish Ali", "staff": "Usman Tariq",
                                    "topic": "Warranty claim update",
                                    "promised_at": now - 26 * H, "created_at": now - 30 * H,
                                    "status": "done", "done_at": now - 25 * H,
                                    "done_on_time": True})

        # --- Staff promises (FICTIONAL): "promised a spare key, 24 days..." ---
        D = 24 * H
        self._add_node("Promise", {"id": "PR-1", "customer_phone": "+923004444444",
                                   "customer_name": "Ayesha Malik", "staff": "Bilal Ahmed",
                                   "promise": "Spare key will be couriered to customer",
                                   "deadline_at": now - 5 * D, "created_at": now - 10 * D,
                                   "status": "pending"})
        self._add_node("Promise", {"id": "PR-2", "customer_phone": "+923003333333",
                                   "customer_name": "Danish Ali", "staff": "Usman Tariq",
                                   "promise": "Bumper scratch repair completed by Friday",
                                   "deadline_at": now + 2 * D, "created_at": now - 1 * D,
                                   "status": "pending"})
