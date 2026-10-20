from flask import Flask, render_template, request, jsonify, session
import os
import hmac
from pathlib import Path

# Charger les variables locales depuis .env, sans ecraser l'environnement
_env_file = Path(__file__).resolve().parent / ".env"
if _env_file.is_file():
    for _line in _env_file.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        _key, _value = _line.split("=", 1)
        _key, _value = _key.strip(), _value.strip()
        if _key and _key not in os.environ:
            os.environ[_key] = _value

app = Flask(__name__)

@app.route("/")
def accueil():
    return render_template("index.html", server_products=get_products())


import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "instance" / "sofi_shop.db"

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def admin_configured():
    return all(os.environ.get(k) for k in (
        "SOFI_SECRET_KEY", "SOFI_ADMIN_EMAIL", "SOFI_ADMIN_PASSWORD"
    ))

app.secret_key = os.environ.get("SOFI_SECRET_KEY")
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("SOFI_COOKIE_SECURE") == "1",
)

@app.route("/api/admin/login", methods=["POST"])
def admin_login():
    if not admin_configured():
        return jsonify({"error": "Configuration administrateur absente"}), 503

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Requête invalide"}), 400

    email = data.get("email")
    password = data.get("password")
    if not isinstance(email, str) or not isinstance(password, str):
        return jsonify({"error": "Identifiants incorrects"}), 401

    ok_email = hmac.compare_digest(
        email.strip().lower(),
        os.environ["SOFI_ADMIN_EMAIL"].strip().lower()
    )
    ok_password = hmac.compare_digest(
        password, os.environ["SOFI_ADMIN_PASSWORD"]
    )
    if not (ok_email and ok_password):
        return jsonify({"error": "Identifiants incorrects"}), 401

    session.clear()
    session["is_admin"] = True
    return jsonify({"ok": True}), 200

@app.route("/api/admin/session", methods=["GET"])
def admin_session_status():
    return jsonify({
        "authenticated": bool(
            admin_configured() and session.get("is_admin") is True
        )
    })

@app.route("/api/admin/logout", methods=["POST"])
def admin_logout():
    session.clear()
    return jsonify({"ok": True}), 200

@app.route("/api/orders", methods=["POST"])
def save_order():
    import json
    import math
    from datetime import datetime

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "JSON invalide"}), 400

    required = ("id", "ref", "createdAt", "customer",
                "delivery", "payment", "totals", "items")
    if any(k not in data for k in required):
        return jsonify({"error": "Champs manquants"}), 400

    customer = data["customer"]
    delivery = data["delivery"]
    totals = data["totals"]
    items = data["items"]

    if not all(isinstance(x, dict) for x in (customer, delivery, totals)):
        return jsonify({"error": "Structure de commande invalide"}), 400
    if not isinstance(items, list) or not items or len(items) > 50:
        return jsonify({"error": "Nombre d'articles invalide"}), 400

    for field in ("name", "phone", "wilaya", "commune"):
        value = customer.get(field)
        if not isinstance(value, str) or not value.strip() or len(value) > 200:
            return jsonify({"error": "Informations client invalides"}), 400

    if data["payment"] != "cod":
        return jsonify({"error": "Mode de paiement non autorisé"}), 400
    if delivery.get("method") not in ("home", "pickup"):
        return jsonify({"error": "Mode de livraison invalide"}), 400

    order_id = data["id"]
    ref = data["ref"]
    created_at = data["createdAt"]
    if not all(isinstance(x, str) and 1 <= len(x) <= 100
               for x in (order_id, ref, created_at)):
        return jsonify({"error": "Identifiants invalides"}), 400
    try:
        datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        return jsonify({"error": "Date invalide"}), 400

    clean_items = []
    subtotal = 0.0
    for item in items:
        if not isinstance(item, dict):
            return jsonify({"error": "Article invalide"}), 400
        try:
            qty = item["qty"]
            price = float(item["unitPrice"])
            total = float(item["total"])
            name = item["name"]
            product_id = item.get("productId", "")
            if isinstance(qty, bool) or not isinstance(qty, int) or not 1 <= qty <= 100:
                raise ValueError()
            if not math.isfinite(price) or not math.isfinite(total) or price < 0 or total < 0:
                raise ValueError()
            if abs(total - price * qty) > 0.02:
                raise ValueError()
            if not isinstance(name, str) or not name.strip() or len(name) > 300:
                raise ValueError()
            if not isinstance(product_id, str) or len(product_id) > 100:
                raise ValueError()
        except (KeyError, TypeError, ValueError, OverflowError):
            return jsonify({"error": "Quantité, prix ou article invalide"}), 400

        subtotal += total
        clean_items.append((product_id, name.strip(), qty, price, total))

    try:
        fee = float(delivery.get("fee"))
        reported_total = float(totals.get("total"))
        if not math.isfinite(fee) or not math.isfinite(reported_total) or fee < 0:
            raise ValueError()
        if abs(reported_total - (subtotal + fee)) > 0.02:
            return jsonify({"error": "Total incohérent"}), 400
    except (TypeError, ValueError, OverflowError):
        return jsonify({"error": "Montants invalides"}), 400

    conn = None
    try:
        conn = get_db()
        with conn:
            conn.execute(
                """INSERT INTO orders
                (id, ref, created_at, status, customer_json,
                 delivery_json, payment, totals_json, source)
                VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, 'website')""",
                (order_id, ref, created_at,
                 json.dumps(customer, ensure_ascii=False),
                 json.dumps(delivery, ensure_ascii=False),
                 data["payment"],
                 json.dumps(totals, ensure_ascii=False))
            )
            for product_id, name, qty, price, total in clean_items:
                conn.execute(
                    """INSERT INTO order_items
                    (order_id, product_id, name, quantity, unit_price, total)
                    VALUES (?, ?, ?, ?, ?, ?)""",
                    (order_id, product_id, name, qty, price, total)
                )
        return jsonify({"ok": True, "id": order_id, "ref": ref}), 201
    except sqlite3.IntegrityError:
        return jsonify({"error": "Commande déjà enregistrée"}), 409
    except Exception:
        app.logger.exception("Erreur enregistrement commande")
        return jsonify({"error": "Erreur serveur"}), 500
    finally:
        if conn is not None:
            conn.close()


# ===== Produits (PostgreSQL / Neon) =====
import json as _json
import math as _math
import re as _re
try:
    import psycopg
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb
except ImportError:
    psycopg = None

_pg_ready = False
_ID_RE = _re.compile(r"^[A-Za-z0-9_-]{1,60}$")


def pg():
    return psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row, connect_timeout=10)


def init_pg():
    global _pg_ready
    if _pg_ready:
        return
    with pg() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS products (
            id TEXT PRIMARY KEY,
            data JSONB NOT NULL,
            position BIGINT NOT NULL DEFAULT 0,
            updated_at TIMESTAMPTZ DEFAULT now())""")
        c.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        done = c.execute("SELECT 1 FROM meta WHERE key = 'catalogue_seeded'").fetchone()
        if not done:
            f = Path(__file__).resolve().parent / "catalogue_initial.json"
            if f.is_file():
                items = _json.loads(f.read_text(encoding="utf-8"))
                for i, p in enumerate(items):
                    c.execute(
                        "INSERT INTO products (id, data, position) VALUES (%s, %s, %s) ON CONFLICT (id) DO NOTHING",
                        (p["id"], Jsonb(p), i))
            c.execute("INSERT INTO meta (key, value) VALUES ('catalogue_seeded', '1') ON CONFLICT DO NOTHING")
    _pg_ready = True


def get_products():
    if psycopg is None or not os.environ.get("DATABASE_URL"):
        return None
    try:
        init_pg()
        with pg() as c:
            rows = c.execute("SELECT data FROM products ORDER BY position, id").fetchall()
        return [r["data"] for r in rows]
    except Exception:
        app.logger.exception("Lecture des produits impossible")
        return None


def is_admin():
    return admin_configured() and session.get("is_admin") is True


def clean_product(d, pid):
    if not isinstance(d, dict):
        return None

    def trio(v, n):
        if not isinstance(v, dict):
            return None
        out = {}
        for k in ("fr", "ar", "en"):
            x = v.get(k, "")
            if x is None:
                x = ""
            if not isinstance(x, str) or len(x) > n:
                return None
            out[k] = x.strip()
        return out

    name = trio(d.get("name"), 200)
    desc = trio(d.get("desc"), 2000)
    if not name or not name["fr"] or desc is None:
        return None
    brand = d.get("brand") or ""
    category = d.get("category") or ""
    image = d.get("image") or ""
    if not all(isinstance(x, str) for x in (brand, category, image)):
        return None
    if len(brand) > 100 or len(category) > 60 or len(image) > 2000:
        return None
    if image and not image.startswith(("https://", "http://", "/")):
        return None
    try:
        price = float(d.get("price"))
        compare = float(d.get("compareAt") or 0)
        stock = float(d.get("stock") or 0)
        rating = float(d.get("rating") or 0)
        reviews = float(d.get("reviews") or 0)
    except (TypeError, ValueError):
        return None
    vals = (price, compare, stock, rating, reviews)
    if not all(_math.isfinite(v) for v in vals) or min(vals) < 0 or rating > 5:
        return None

    def num(x):
        return int(x) if float(x).is_integer() else x

    return {
        "id": pid, "name": name, "brand": brand.strip(), "category": category,
        "desc": desc, "price": num(price), "compareAt": num(compare),
        "stock": int(stock), "image": image, "rating": rating,
        "reviews": int(reviews), "featured": bool(d.get("featured")),
        "isNew": bool(d.get("isNew")),
    }


def _admin_db_guard():
    if not is_admin():
        return jsonify({"error": "Non autorisé"}), 401
    if psycopg is None or not os.environ.get("DATABASE_URL"):
        return jsonify({"error": "Base de données non configurée"}), 503
    return None


@app.route("/api/products", methods=["GET"])
def api_products():
    items = get_products()
    if items is None:
        return jsonify({"error": "Base de données indisponible"}), 503
    return jsonify(items)


@app.route("/api/admin/products", methods=["POST"])
def api_product_create():
    g = _admin_db_guard()
    if g:
        return g
    data = request.get_json(silent=True)
    pid = data.get("id") if isinstance(data, dict) else None
    if not isinstance(pid, str) or not _ID_RE.match(pid):
        return jsonify({"error": "Identifiant invalide"}), 400
    p = clean_product(data, pid)
    if p is None:
        return jsonify({"error": "Produit invalide (nom, prix ou image)"}), 400
    try:
        init_pg()
        with pg() as c:
            row = c.execute("SELECT COALESCE(MIN(position), 0) - 1 AS pos FROM products").fetchone()
            c.execute("INSERT INTO products (id, data, position) VALUES (%s, %s, %s)",
                      (pid, Jsonb(p), row["pos"]))
        return jsonify({"ok": True, "product": p}), 201
    except psycopg.errors.UniqueViolation:
        return jsonify({"error": "Produit déjà existant"}), 409
    except Exception:
        app.logger.exception("Création produit")
        return jsonify({"error": "Erreur serveur"}), 500


@app.route("/api/admin/products/<pid>", methods=["PUT"])
def api_product_update(pid):
    g = _admin_db_guard()
    if g:
        return g
    if not _ID_RE.match(pid):
        return jsonify({"error": "Identifiant invalide"}), 400
    p = clean_product(request.get_json(silent=True), pid)
    if p is None:
        return jsonify({"error": "Produit invalide (nom, prix ou image)"}), 400
    try:
        init_pg()
        with pg() as c:
            cur = c.execute("UPDATE products SET data = %s, updated_at = now() WHERE id = %s",
                            (Jsonb(p), pid))
            if cur.rowcount == 0:
                return jsonify({"error": "Produit introuvable"}), 404
        return jsonify({"ok": True, "product": p})
    except Exception:
        app.logger.exception("Modification produit")
        return jsonify({"error": "Erreur serveur"}), 500


@app.route("/api/admin/products/<pid>", methods=["DELETE"])
def api_product_delete(pid):
    g = _admin_db_guard()
    if g:
        return g
    if not _ID_RE.match(pid):
        return jsonify({"error": "Identifiant invalide"}), 400
    try:
        init_pg()
        with pg() as c:
            cur = c.execute("DELETE FROM products WHERE id = %s", (pid,))
            if cur.rowcount == 0:
                return jsonify({"error": "Produit introuvable"}), 404
        return jsonify({"ok": True})
    except Exception:
        app.logger.exception("Suppression produit")
        return jsonify({"error": "Erreur serveur"}), 500
# ===== Fin produits =====

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
