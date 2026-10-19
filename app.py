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
    return render_template("index.html")


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


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
