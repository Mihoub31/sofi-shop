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
    return render_template("index.html", server_products=get_products(),
                           server_settings=get_settings())


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
    if admin_configured():
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
    if not all(isinstance(x, str) and _ID_RE.match(x) for x in (order_id, ref)):
        return jsonify({"error": "Identifiants invalides"}), 400
    if not isinstance(created_at, str) or len(created_at) > 100:
        return jsonify({"error": "Identifiants invalides"}), 400
    try:
        datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        return jsonify({"error": "Date invalide"}), 400

    shop = get_settings()
    delivery_cfg = shop.get("delivery") if isinstance(shop, dict) else None
    if not isinstance(delivery_cfg, dict) or not delivery_cfg:
        return jsonify({"error": "Configuration boutique indisponible"}), 503

    method = delivery["method"]
    wilaya = customer["wilaya"].strip()
    wilaya_cfg = delivery_cfg.get(wilaya)
    if not isinstance(wilaya_cfg, dict):
        return jsonify({"error": "Wilaya de livraison inconnue"}), 400
    if method == "pickup" and not wilaya_cfg.get("relayAvailable"):
        return jsonify({"error": "Retrait au bureau indisponible dans cette wilaya"}), 400
    communes = {}
    for name in wilaya_cfg.get("communes") or []:
        if isinstance(name, str) and name.strip():
            communes[name.strip().casefold()] = name.strip()
    commune = customer["commune"].strip()
    if communes:
        commune = communes.get(commune.casefold(), "")
        if not commune:
            return jsonify({"error": "Commune invalide pour cette wilaya"}), 400
    customer["wilaya"] = wilaya
    customer["commune"] = commune

    clean_items = []
    for item in items:
        if not isinstance(item, dict):
            return jsonify({"error": "Article invalide"}), 400
        try:
            qty = item["qty"]
            price = float(item["unitPrice"])
            total = float(item["total"])
            name = item["name"]
            product_id = item.get("productId", "")
            mode = item.get("mode", "retail")
            if isinstance(qty, bool) or not isinstance(qty, int) or not 1 <= qty <= 100:
                raise ValueError()
            if mode not in ("retail", "pack", "wholesale"):
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

        item["mode"] = mode
        clean_items.append((product_id, name.strip(), qty, price, total))

    if psycopg is None or not os.environ.get("DATABASE_URL"):
        return jsonify({"error": "Base de données non configurée"}), 503
    product_ids = sorted({it[0] for it in clean_items if it[0]})
    try:
        init_pg()
        with pg() as c:
            rows = c.execute(
                "SELECT id, (data->>'price')::numeric AS p FROM products WHERE id = ANY(%s)",
                (product_ids,)).fetchall() if product_ids else []
        prices = {r["id"]: float(r["p"]) for r in rows if r["p"] is not None}
    except Exception:
        app.logger.exception("Lecture des prix produits")
        return jsonify({"error": "Erreur serveur"}), 500

    subtotal = 0.0
    for pid, _name, qty, price, _total in clean_items:
        if not pid or pid not in prices:
            return jsonify({"error": "Produit indisponible"}), 409
        if abs(price - prices[pid]) > 0.02:
            return jsonify({"error": "Prix d'un produit modifié"}), 409
        subtotal += prices[pid] * qty
    for item in items:
        unit = prices[item["productId"]]
        item["unitPrice"] = unit
        item["total"] = _round2(unit * item["qty"])

    pack_qty, pack_sum, pack_max, pack_ids = 0, 0.0, 0.0, set()
    for item in items:
        if item["mode"] == "pack":
            pack_qty += item["qty"]
            pack_sum += item["unitPrice"] * item["qty"]
            pack_max = max(pack_max, item["unitPrice"])
            pack_ids.add(item["productId"])
    rate = 0.30 if pack_max <= 1000 else 0.25 if pack_max <= 5000 else 0.20
    pack_disc = _round2(pack_sum * rate) if pack_qty >= 3 and len(pack_ids) >= 3 else 0.0

    coupon_code = data.get("coupon") or ""
    if not isinstance(coupon_code, str) or len(coupon_code) > 40:
        return jsonify({"error": "Code de remise invalide"}), 400
    coupon_code = coupon_code.strip().upper()
    coupon_disc = 0.0
    coupon_base = subtotal - pack_disc
    if coupon_code:
        promo = next((p for p in (shop.get("promos") or [])
                      if isinstance(p, dict) and p.get("active")
                      and str(p.get("code") or "").upper() == coupon_code), None)
        try:
            minimum = float(promo.get("min") or 0) if promo else 0.0
        except (TypeError, ValueError):
            minimum = 0.0
        if promo is not None and coupon_base >= minimum:
            try:
                value = float(promo.get("value") or 0)
                cap = float(promo.get("maxDiscount") or 0)
            except (TypeError, ValueError):
                value, cap = 0.0, 0.0
            if math.isfinite(value) and math.isfinite(cap) and value >= 0:
                coupon_disc = coupon_base * value / 100 if promo.get("type") == "percent" else value
                if cap > 0:
                    coupon_disc = min(coupon_disc, cap)
                coupon_disc = _round2(min(max(coupon_disc, 0.0), coupon_base))
                coupon_code = str(promo.get("code") or coupon_code).upper()
            else:
                coupon_code = ""
        else:
            coupon_code = ""

    try:
        reported_fee = float(delivery.get("fee"))
        reported_coupon = float(totals.get("couponDiscount") or 0)
        reported_pack = float(totals.get("packDiscount") or 0)
        reported_total = float(totals.get("total"))
        if not all(math.isfinite(x) for x in (reported_fee, reported_coupon,
                                              reported_pack, reported_total)):
            raise ValueError()
        if reported_fee < 0 or reported_coupon < 0 or reported_pack < 0:
            raise ValueError()
    except (TypeError, ValueError, OverflowError):
        return jsonify({"error": "Montants invalides"}), 400

    if abs(reported_pack - pack_disc) > 0.02:
        return jsonify({"error": "Remise pack incorrecte"}), 400
    if abs(reported_coupon - coupon_disc) > 0.02:
        return jsonify({"error": "Remise incorrecte"}), 400

    after_discounts = coupon_base - coupon_disc
    fee = float(wilaya_cfg.get("relay" if method == "pickup" else "home") or 0)
    if after_discounts >= float(shop.get("freeShippingThreshold") or 0):
        fee = 0.0
    if abs(reported_fee - fee) > 0.02:
        return jsonify({"error": "Frais de livraison incorrects"}), 400
    expected_total = after_discounts + fee
    if abs(reported_total - expected_total) > 0.02:
        return jsonify({"error": "Total incohérent"}), 400

    delivery["fee"] = fee
    totals.update({
        "subtotal": _round2(subtotal), "packDiscount": pack_disc,
        "couponDiscount": coupon_disc, "shipping": fee,
        "total": _round2(expected_total),
    })
    order = {
        "id": order_id, "ref": ref, "createdAt": created_at, "status": "pending",
        "demo": False, "customer": customer, "delivery": delivery, "coupon": coupon_code,
        "payment": data["payment"], "items": items, "totals": totals,
    }
    if len(json.dumps(order, ensure_ascii=False)) > 60000:
        return jsonify({"error": "Commande trop volumineuse"}), 400
    try:
        init_pg()
        _ensure_orders()
        with pg() as c:
            c.execute(
                "INSERT INTO orders_v2 (id, ref, created_at, status, data) VALUES (%s, %s, %s, 'pending', %s)",
                (order_id, ref, created_at, Jsonb(order)))
            need = {}
            for pid, _n, qty, _p, _t in clean_items:
                if pid:
                    need[pid] = need.get(pid, 0) + qty
            for pid, qty in need.items():
                row = c.execute(
                    "SELECT (data->>'stock')::numeric AS s FROM products WHERE id = %s FOR UPDATE",
                    (pid,)).fetchone()
                if row is None:
                    continue
                if row["s"] is None or row["s"] < qty:
                    raise _OutOfStock()
                c.execute(
                    "UPDATE products SET data = jsonb_set(data, '{stock}', to_jsonb((data->>'stock')::int - %s)) WHERE id = %s",
                    (qty, pid))
        return jsonify({"ok": True, "id": order_id, "ref": ref}), 201
    except _OutOfStock:
        return jsonify({"error": "Stock insuffisant pour un des articles"}), 409
    except psycopg.errors.UniqueViolation:
        return jsonify({"error": "Commande déjà enregistrée"}), 409
    except Exception:
        app.logger.exception("Erreur enregistrement commande")
        return jsonify({"error": "Erreur serveur"}), 500


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
        c.execute("""CREATE TABLE IF NOT EXISTS shop_settings (
            id SMALLINT PRIMARY KEY CHECK (id = 1),
            data JSONB NOT NULL,
            updated_at TIMESTAMPTZ DEFAULT now())""")
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
        has_settings = c.execute("SELECT 1 FROM shop_settings WHERE id = 1").fetchone()
        if not has_settings:
            seed = clean_settings(_seed_settings())
            if seed is not None:
                c.execute("INSERT INTO shop_settings (id, data) VALUES (1, %s) ON CONFLICT DO NOTHING",
                          (Jsonb(seed),))
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


def _seed_settings():
    f = Path(__file__).resolve().parent / "settings_initial.json"
    try:
        d = _json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        app.logger.exception("Lecture des réglages initiaux impossible")
        return None
    return d if isinstance(d, dict) else None


def _setting_num(v, lo, hi):
    if isinstance(v, bool):
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if not _math.isfinite(x) or x < lo or x > hi:
        return None
    return int(x) if x.is_integer() else round(x, 2)


def _round2(x):
    return _math.floor(x * 100 + 0.5) / 100


def clean_settings(d):
    if not isinstance(d, dict):
        return None
    threshold = _setting_num(d.get("freeShippingThreshold", 0), 0, 10_000_000)
    if threshold is None:
        return None
    delivery = d.get("delivery")
    if not isinstance(delivery, dict) or not delivery or len(delivery) > 120:
        return None
    clean_delivery = {}
    for code, info in delivery.items():
        if not isinstance(code, str) or not _re.match(r"^\d{1,2}$", code) or not isinstance(info, dict):
            return None
        home = _setting_num(info.get("home"), 0, 100_000)
        relay = _setting_num(info.get("relay"), 0, 100_000)
        communes = info.get("communes")
        if home is None or relay is None or not isinstance(communes, list) or len(communes) > 300:
            return None
        seen, clean_communes = set(), []
        for name in communes:
            if not isinstance(name, str):
                return None
            name = name.strip()
            if not name or len(name) > 120:
                return None
            key = name.casefold()
            if key not in seen:
                seen.add(key)
                clean_communes.append(name)
        if not clean_communes:
            return None
        clean_delivery[code] = {
            "home": home, "relay": relay,
            "relayAvailable": bool(info.get("relayAvailable")),
            "communes": clean_communes,
        }
    promos = d.get("promos", [])
    if not isinstance(promos, list) or len(promos) > 100:
        return None
    clean_promos, ids, codes = [], set(), set()
    for p in promos:
        if not isinstance(p, dict):
            return None
        pid, code = p.get("id"), p.get("code")
        if not isinstance(pid, str) or not _ID_RE.match(pid) or pid in ids:
            return None
        if not isinstance(code, str):
            return None
        code = code.strip().upper()
        if not _re.match(r"^[A-Z0-9_-]{2,40}$", code) or code in codes:
            return None
        if p.get("type") not in ("percent", "fixed"):
            return None
        value = _setting_num(p.get("value"), 0, 10_000_000)
        minimum = _setting_num(p.get("min", 0), 0, 10_000_000)
        cap = _setting_num(p.get("maxDiscount", 0), 0, 10_000_000)
        if value is None or minimum is None or cap is None:
            return None
        if p["type"] == "percent" and value > 100:
            return None
        ids.add(pid)
        codes.add(code)
        clean_promos.append({
            "id": pid, "code": code, "type": p["type"], "value": value,
            "min": minimum, "maxDiscount": cap, "active": bool(p.get("active")),
        })
    return {
        "customized": bool(d.get("customized")),
        "freeShippingThreshold": threshold,
        "delivery": clean_delivery,
        "promos": clean_promos,
    }


def get_settings():
    if psycopg is None or not os.environ.get("DATABASE_URL"):
        return _seed_settings()
    try:
        init_pg()
        with pg() as c:
            row = c.execute("SELECT data FROM shop_settings WHERE id = 1").fetchone()
        if row and isinstance(row["data"], dict):
            return row["data"]
    except Exception:
        app.logger.exception("Lecture des réglages impossible")
    return _seed_settings()


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
        **({"credit": d["credit"].strip()[:120]} if isinstance(d.get("credit"), str) and d["credit"].strip() else {}),
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


@app.route("/api/settings", methods=["GET"])
def api_settings():
    shop = get_settings()
    if not isinstance(shop, dict):
        return jsonify({"error": "Configuration indisponible"}), 503
    return jsonify(shop)


@app.route("/api/admin/settings", methods=["PUT"])
def api_admin_settings_save():
    g = _admin_db_guard()
    if g:
        return g
    clean = clean_settings(request.get_json(silent=True))
    if clean is None:
        return jsonify({"error": "Réglages invalides"}), 400
    clean["customized"] = True
    try:
        init_pg()
        with pg() as c:
            c.execute(
                "INSERT INTO shop_settings (id, data) VALUES (1, %s) "
                "ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data, updated_at = now()",
                (Jsonb(clean),))
        return jsonify({"ok": True, "settings": clean})
    except Exception:
        app.logger.exception("Enregistrement des réglages")
        return jsonify({"error": "Erreur serveur"}), 500


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

import secrets as _secrets
from flask import Response

app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024


def _ensure_images():
    with pg() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS product_images (
            id TEXT PRIMARY KEY, mime TEXT NOT NULL, data BYTEA NOT NULL,
            created_at TIMESTAMPTZ DEFAULT now())""")


def _image_mime(b):
    if b[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if b[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        return "image/webp"
    return None


@app.route("/api/admin/upload", methods=["POST"])
def api_upload_image():
    g = _admin_db_guard()
    if g:
        return g
    f = request.files.get("file")
    data = f.read() if f else b""
    if not data or len(data) > 3 * 1024 * 1024:
        return jsonify({"error": "Image vide ou trop lourde (max 3 Mo)"}), 400
    mime = _image_mime(data)
    if not mime:
        return jsonify({"error": "Format non accepté (JPG, PNG ou WEBP)"}), 400
    iid = _secrets.token_urlsafe(12)
    try:
        _ensure_images()
        with pg() as c:
            c.execute("INSERT INTO product_images (id, mime, data) VALUES (%s, %s, %s)",
                      (iid, mime, data))
        return jsonify({"ok": True, "url": "/api/images/" + iid}), 201
    except Exception:
        app.logger.exception("Envoi image")
        return jsonify({"error": "Erreur serveur"}), 500


@app.route("/api/images/<iid>", methods=["GET"])
def api_get_image(iid):
    if not _ID_RE.match(iid) or psycopg is None or not os.environ.get("DATABASE_URL"):
        return "", 404
    try:
        _ensure_images()
        with pg() as c:
            row = c.execute("SELECT mime, data FROM product_images WHERE id = %s", (iid,)).fetchone()
    except Exception:
        app.logger.exception("Lecture image")
        return "", 500
    if not row:
        return "", 404
    return Response(bytes(row["data"]), mimetype=row["mime"],
                    headers={"Cache-Control": "public, max-age=31536000, immutable"})


_STATUSES = ("pending", "confirmed", "prepared", "shipped", "delivered", "cancelled")


def _ensure_orders():
    with pg() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS orders_v2 (
            id TEXT PRIMARY KEY, ref TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', data JSONB NOT NULL,
            received_at TIMESTAMPTZ DEFAULT now())""")


@app.route("/api/admin/orders", methods=["GET"])
def api_admin_orders():
    g = _admin_db_guard()
    if g:
        return g
    try:
        _ensure_orders()
        with pg() as c:
            rows = c.execute("SELECT data, status FROM orders_v2 ORDER BY created_at DESC LIMIT 1000").fetchall()
        out = []
        for r in rows:
            o = r["data"]
            o["status"] = r["status"]
            out.append(o)
        return jsonify(out)
    except Exception:
        app.logger.exception("Lecture commandes")
        return jsonify({"error": "Erreur serveur"}), 500


@app.route("/api/admin/orders/<oid>/status", methods=["PATCH"])
def api_admin_order_status(oid):
    g = _admin_db_guard()
    if g:
        return g
    body = request.get_json(silent=True)
    st = body.get("status") if isinstance(body, dict) else None
    if st not in _STATUSES or not _ID_RE.match(oid):
        return jsonify({"error": "Statut invalide"}), 400
    try:
        _ensure_orders()
        with pg() as c:
            cur = c.execute("UPDATE orders_v2 SET status = %s WHERE id = %s", (st, oid))
            if cur.rowcount == 0:
                return jsonify({"error": "Commande introuvable"}), 404
        return jsonify({"ok": True})
    except Exception:
        app.logger.exception("Statut commande")
        return jsonify({"error": "Erreur serveur"}), 500

class _OutOfStock(Exception):
    pass



@app.route("/api/admin/usage", methods=["GET"])
def api_admin_usage():
    g = _admin_db_guard()
    if g:
        return g
    try:
        init_pg()
        _ensure_images()
        _ensure_orders()
        with pg() as c:
            db = c.execute("SELECT pg_database_size(current_database()) AS b").fetchone()["b"]
            pr = c.execute("SELECT count(*) AS n FROM products").fetchone()["n"]
            im = c.execute("SELECT count(*) AS n, COALESCE(sum(length(data)), 0) AS b FROM product_images").fetchone()
            od = c.execute("SELECT count(*) AS n FROM orders_v2").fetchone()["n"]
        return jsonify({
            "dbBytes": int(db), "limitBytes": 512 * 1024 * 1024,
            "products": int(pr), "images": int(im["n"]),
            "imageBytes": int(im["b"]), "orders": int(od),
        })
    except Exception:
        app.logger.exception("Lecture de l'espace utilisé")
        return jsonify({"error": "Erreur serveur"}), 500


import urllib.request as _ur
import urllib.parse as _up


def _obf_host_ok(u):
    try:
        p = _up.urlparse(u)
    except Exception:
        return False
    host = (p.hostname or "").lower()
    return p.scheme == "https" and (host == "openbeautyfacts.org" or host.endswith(".openbeautyfacts.org"))


class _ObfRedirect(_ur.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _obf_host_ok(newurl):
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_obf_opener = _ur.build_opener(_ObfRedirect)


def _obf_fetch(url, limit):
    req = _ur.Request(url, headers={"User-Agent": "SofiShop/1.0 (boutique en ligne, photos produits)"})
    with _obf_opener.open(req, timeout=12) as r:
        data = r.read(limit + 1)
    if len(data) > limit:
        raise ValueError("fichier trop volumineux")
    return data


@app.route("/api/admin/photo-search", methods=["GET"])
def api_photo_search():
    if not is_admin():
        return jsonify({"error": "Non autorisé"}), 401
    q = (request.args.get("q") or "").strip()[:80]
    if len(q) < 2:
        return jsonify([])
    url = "https://world.openbeautyfacts.org/cgi/search.pl?" + _up.urlencode({
        "search_terms": q, "search_simple": 1, "action": "process", "json": 1,
        "page_size": 24, "fields": "product_name,brands,image_front_url,image_front_small_url,code"})
    try:
        data = _json.loads(_obf_fetch(url, 2 * 1024 * 1024).decode("utf-8", "replace"))
    except Exception:
        app.logger.exception("Recherche de photos")
        return jsonify({"error": "Service de photos indisponible"}), 502
    out, seen = [], set()
    for p in (data.get("products") or []):
        img = p.get("image_front_url") or ""
        if not _obf_host_ok(img) or img in seen:
            continue
        seen.add(img)
        thumb = p.get("image_front_small_url") or img
        out.append({
            "name": str(p.get("product_name") or "")[:120],
            "brand": str(p.get("brands") or "")[:80],
            "image": img,
            "thumb": thumb if _obf_host_ok(thumb) else img,
        })
        if len(out) >= 6:
            break
    return jsonify(out)



@app.route("/api/admin/photo-suggestions", methods=["GET"])
def api_photo_suggestions():
    if not is_admin():
        return jsonify({"error": "Non autorisé"}), 401
    q = (request.args.get("q") or "").strip()[:80]
    if len(q) < 2:
        return jsonify([])
    url = "https://world.openbeautyfacts.org/cgi/search.pl?" + _up.urlencode({
        "search_terms": q, "search_simple": 1, "action": "process",
        "json": 1, "page_size": 20,
        "fields": "product_name,brands"
    })
    try:
        data = _json.loads(
            _obf_fetch(url, 1024 * 1024).decode("utf-8", "replace")
        )
    except Exception:
        app.logger.exception("Suggestions de produits")
        return jsonify({"error": "Suggestions indisponibles"}), 502
    out, seen = [], set()
    for p in data.get("products") or []:
        name = str(p.get("product_name") or "").strip()[:120]
        brand = str(p.get("brands") or "").strip()[:80]
        key = (name.casefold(), brand.casefold())
        if not name or key in seen:
            continue
        seen.add(key)
        out.append({"name": name, "brand": brand})
        if len(out) >= 10:
            break
    return jsonify(out)


@app.route("/api/admin/photo-import", methods=["POST"])
def api_photo_import():
    g = _admin_db_guard()
    if g:
        return g
    body = request.get_json(silent=True)
    u = body.get("url") if isinstance(body, dict) else None
    if not isinstance(u, str) or len(u) > 500 or not _obf_host_ok(u):
        return jsonify({"error": "Adresse d'image non autorisée"}), 400
    try:
        data = _obf_fetch(u, 3 * 1024 * 1024)
    except Exception:
        app.logger.exception("Import de photo")
        return jsonify({"error": "Téléchargement impossible"}), 502
    mime = _image_mime(data)
    if not mime:
        return jsonify({"error": "Format d'image non accepté"}), 400
    iid = _secrets.token_urlsafe(12)
    try:
        _ensure_images()
        with pg() as c:
            c.execute("INSERT INTO product_images (id, mime, data) VALUES (%s, %s, %s)", (iid, mime, data))
        return jsonify({"ok": True, "url": "/api/images/" + iid}), 201
    except Exception:
        app.logger.exception("Enregistrement de la photo importée")
        return jsonify({"error": "Erreur serveur"}), 500

# ===== Fin produits =====

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
