import os
from datetime import datetime
from functools import wraps
from flask import Flask, request, jsonify, session, send_from_directory
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash

ROOT = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, static_folder="static", static_url_path="/static")
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "CHANGE-ME-BEFORE-DEPLOYMENT")
db_url = os.environ.get("DATABASE_URL", "sqlite:///" + os.path.join(ROOT, "applehub.db"))
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = db_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("COOKIE_SECURE", "0") == "1"
db = SQLAlchemy(app)

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)

class Repair(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    number = db.Column(db.String(40), unique=True, nullable=False)
    client = db.Column(db.String(160), nullable=False)
    phone = db.Column(db.String(50), default="")
    device = db.Column(db.String(160), nullable=False)
    imei = db.Column(db.String(80), default="")
    problem = db.Column(db.Text, nullable=False)
    diagnosis = db.Column(db.Text, default="")
    technician = db.Column(db.String(120), default="")
    status = db.Column(db.String(40), default="Recibido")
    warranty_days = db.Column(db.Integer, default=30)
    estimated_price = db.Column(db.Float, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class InventoryItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(180), nullable=False)
    category = db.Column(db.String(80), default="Repuesto")
    compatibility = db.Column(db.String(180), default="")
    supplier = db.Column(db.String(160), default="")
    cost = db.Column(db.Float, default=0)
    price = db.Column(db.Float, default=0)
    stock = db.Column(db.Integer, default=0)
    low_stock_at = db.Column(db.Integer, default=2)

class Movement(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    kind = db.Column(db.String(30), nullable=False)
    concept = db.Column(db.String(220), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

def serial(obj):
    out = {}
    for c in obj.__table__.columns:
        v = getattr(obj, c.name)
        out[c.name] = v.isoformat() if isinstance(v, datetime) else v
    return out

def auth_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            return jsonify({"error": "Debes iniciar sesión."}), 401
        return fn(*args, **kwargs)
    return wrapper

@app.get("/")
def index():
    return send_from_directory("static", "index.html")

@app.get("/manifest.webmanifest")
def manifest():
    return send_from_directory("static", "manifest.webmanifest")

@app.get("/sw.js")
def sw():
    return send_from_directory("static", "sw.js", mimetype="application/javascript")

@app.post("/api/setup")
def setup():
    db.create_all()
    if User.query.first():
        return jsonify({"error": "La cuenta administradora ya existe. Inicia sesión."}), 409
    d = request.get_json(silent=True) or {}
    username, password = (d.get("username") or "").strip(), d.get("password") or ""
    if len(username) < 3 or len(password) < 12:
        return jsonify({"error": "Usuario mínimo de 3 caracteres y contraseña de 12 o más."}), 400
    u = User(username=username, password_hash=generate_password_hash(password))
    db.session.add(u); db.session.commit()
    session["user_id"] = u.id
    return jsonify({"ok": True})

@app.post("/api/login")
def login():
    db.create_all()
    d = request.get_json(silent=True) or {}
    u = User.query.filter_by(username=(d.get("username") or "").strip()).first()
    if not u or not check_password_hash(u.password_hash, d.get("password") or ""):
        return jsonify({"error": "Usuario o contraseña incorrectos."}), 401
    session.clear(); session["user_id"] = u.id
    return jsonify({"ok": True})

@app.post("/api/logout")
def logout():
    session.clear()
    return jsonify({"ok": True})

@app.get("/api/me")
def me():
    u = db.session.get(User, session["user_id"]) if session.get("user_id") else None
    return jsonify({"authenticated": bool(u), "username": u.username if u else None})

@app.get("/api/data")
@auth_required
def data():
    income = db.session.query(db.func.coalesce(db.func.sum(Movement.amount), 0)).filter(Movement.kind.in_(["ingreso", "venta"])).scalar()
    expenses = db.session.query(db.func.coalesce(db.func.sum(Movement.amount), 0)).filter(Movement.kind == "gasto").scalar()
    return jsonify({
        "repairs": [serial(x) for x in Repair.query.order_by(Repair.id.desc()).all()],
        "inventory": [serial(x) for x in InventoryItem.query.order_by(InventoryItem.name.asc()).all()],
        "movements": [serial(x) for x in Movement.query.order_by(Movement.id.desc()).all()],
        "summary": {
            "repairs": Repair.query.count(),
            "open_repairs": Repair.query.filter(Repair.status.notin_(["Entregado", "Cancelado"])).count(),
            "inventory_items": InventoryItem.query.count(),
            "low_stock": InventoryItem.query.filter(InventoryItem.stock <= InventoryItem.low_stock_at).count(),
            "income": income, "expenses": expenses
        }
    })

@app.post("/api/repairs")
@auth_required
def add_repair():
    d = request.get_json(silent=True) or {}
    if any(not str(d.get(k, "")).strip() for k in ("client", "device", "problem")):
        return jsonify({"error": "Cliente, equipo y falla son obligatorios."}), 400
    number = "AH-" + datetime.utcnow().strftime("%y%m%d%H%M%S") + "-" + str(Repair.query.count()+1).zfill(3)
    x = Repair(number=number, client=d["client"].strip(), phone=d.get("phone",""),
        device=d["device"].strip(), imei=d.get("imei",""), problem=d["problem"].strip(),
        diagnosis=d.get("diagnosis",""), technician=d.get("technician",""),
        status="Recibido", warranty_days=max(0,int(d.get("warranty_days",30) or 0)),
        estimated_price=max(0,float(d.get("estimated_price",0) or 0)))
    db.session.add(x); db.session.commit()
    return jsonify(serial(x)), 201

@app.patch("/api/repairs/<int:item_id>")
@auth_required
def update_repair(item_id):
    x = db.session.get(Repair, item_id)
    if not x: return jsonify({"error": "Orden no encontrada."}), 404
    d = request.get_json(silent=True) or {}
    for k in ("status", "diagnosis", "technician"):
        if k in d: setattr(x, k, d[k])
    if "estimated_price" in d: x.estimated_price = max(0, float(d["estimated_price"] or 0))
    db.session.commit()
    return jsonify(serial(x))

@app.post("/api/inventory")
@auth_required
def add_item():
    d = request.get_json(silent=True) or {}
    if not str(d.get("name","")).strip(): return jsonify({"error":"El nombre es obligatorio."}), 400
    x = InventoryItem(name=d["name"].strip(), category=d.get("category","Repuesto"),
        compatibility=d.get("compatibility",""), supplier=d.get("supplier",""),
        cost=max(0,float(d.get("cost",0) or 0)), price=max(0,float(d.get("price",0) or 0)),
        stock=max(0,int(d.get("stock",0) or 0)), low_stock_at=max(0,int(d.get("low_stock_at",2) or 0)))
    db.session.add(x); db.session.commit()
    return jsonify(serial(x)), 201

@app.patch("/api/inventory/<int:item_id>")
@auth_required
def update_item(item_id):
    x = db.session.get(InventoryItem, item_id)
    if not x: return jsonify({"error":"Artículo no encontrado."}), 404
    d = request.get_json(silent=True) or {}
    for k in ("name","category","compatibility","supplier"):
        if k in d: setattr(x,k,d[k])
    for k in ("cost","price"):
        if k in d: setattr(x,k,max(0,float(d[k] or 0)))
    for k in ("stock","low_stock_at"):
        if k in d: setattr(x,k,max(0,int(d[k] or 0)))
    db.session.commit()
    return jsonify(serial(x))

@app.post("/api/movements")
@auth_required
def add_movement():
    d = request.get_json(silent=True) or {}
    kind, concept = d.get("kind"), str(d.get("concept","")).strip()
    try: amount = float(d.get("amount",0) or 0)
    except (TypeError, ValueError): amount = 0
    if kind not in ("ingreso","venta","gasto") or not concept or amount <= 0:
        return jsonify({"error":"Tipo, concepto e importe positivo son obligatorios."}), 400
    x = Movement(kind=kind, concept=concept, amount=amount)
    db.session.add(x); db.session.commit()
    return jsonify(serial(x)), 201

@app.get("/api/health")
def health():
    return jsonify({"ok": True})

if __name__ == "__main__":
    with app.app_context(): db.create_all()
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 5000)), debug=False)
