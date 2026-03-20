import os
import random
from datetime import datetime

from dotenv import load_dotenv
import mercadopago

from flask import Flask, render_template, request, redirect, url_for, flash, session
from flask_sqlalchemy import SQLAlchemy
from flask_login import (
    LoginManager, UserMixin, login_user, login_required,
    logout_user, current_user
)
from werkzeug.security import generate_password_hash, check_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__)
app.config["SECRET_KEY"] = "cambia-esto-por-una-clave-larga"
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///" + os.path.join(BASE_DIR, "ciammaro.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

# ----------------------------
# ENV + MERCADO PAGO
# ----------------------------
load_dotenv(os.path.join(BASE_DIR, ".env"))

MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN", "").strip()
sdk = mercadopago.SDK(MP_ACCESS_TOKEN)

print("MP TOKEN CARGADO:", "SI" if MP_ACCESS_TOKEN else "NO")

db = SQLAlchemy(app)

login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message_category = "info"

# ----------------------------
# ENVÍO GRATIS DESDE
# ----------------------------
ENVIO_GRATIS_DESDE = 35000


# ----------------------------
# HELPERS
# ----------------------------
def fmt_ars(n: int) -> str:
    try:
        return f"{int(n):,}".replace(",", ".")
    except Exception:
        return str(n)


# ----------------------------
# MODELO USUARIO
# ----------------------------
class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(180), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


with app.app_context():
    db.create_all()


# ----------------------------
# "CATALOGO" TEMPORAL (después lo pasamos a DB)
# ----------------------------
PRODUCTOS = [
    {"id": 1, "nombre": "Remera Minimal White", "precio": 33000, "desc": "Blanco roto · Tacto suave", "img": "logo-ciammaro.png"},
    {"id": 2, "nombre": "Remera Edge Slim", "precio": 35500, "desc": "Fit slim · Terminación premium", "img": "logo-ciammaro.png"},
]


def get_producto(pid: int):
    return next((p for p in PRODUCTOS if p["id"] == pid), None)


def get_cart():
    return session.get("cart", {})


def save_cart(cart):
    session["cart"] = cart
    session.modified = True


def build_cart_items():
    cart = get_cart()
    items = []
    total = 0

    for pid_str, qty in cart.items():
        p = get_producto(int(pid_str))
        if not p:
            continue
        subtotal = p["precio"] * qty
        total += subtotal
        items.append({"p": p, "qty": qty, "subtotal": subtotal})

    return items, total


# ----------------------------
# PROMO BAR DINÁMICA (arriba)
# ----------------------------
@app.context_processor
def inject_promo_envio():
    promo_text = (
        f"🚚 Envío gratis en compras desde $ {fmt_ars(ENVIO_GRATIS_DESDE)} "
        f"· Envíos a todo el país"
    )

    return dict(
        ENVIO_GRATIS_DESDE=ENVIO_GRATIS_DESDE,
        promo_text=promo_text,
    )

# ----------------------------
# ENVIO (MVP por Código Postal) + envío gratis por monto
# ----------------------------
def calc_envio(cp: str, subtotal: int) -> int:
    subtotal = int(subtotal or 0)

    # envío gratis por monto
    if subtotal >= ENVIO_GRATIS_DESDE:
        return 0

    cp = (cp or "").strip()
    if len(cp) < 4:
        return 0

    # CABA / AMBA (aprox)
    if cp.startswith("1"):
        return 4500

    # Buenos Aires (aprox)
    if cp.startswith(("18", "19")) or cp.startswith("2"):
        return 5500

    # Interior
    return 7500


@app.post("/envio/cotizar")
def envio_cotizar():
    cp = (request.form.get("cp") or "").strip()
    _, subtotal = build_cart_items()

    costo = calc_envio(cp, subtotal)

    session["envio_cp"] = cp
    session["envio_costo"] = int(costo)
    session.modified = True

    if int(costo) == 0 and int(subtotal) >= ENVIO_GRATIS_DESDE:
        flash(f"¡Envío gratis por compras desde $ {fmt_ars(ENVIO_GRATIS_DESDE)}!", "success")
    else:
        flash("Envío calculado.", "success")

    return redirect(url_for("checkout"))


# ----------------------------
# RUTAS PRINCIPALES
# ----------------------------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/remeras")
def remeras():
    return render_template("remeras.html", productos=PRODUCTOS)


# ----------------------------
# CARRITO
# ----------------------------
@app.post("/cart/add/<int:pid>")
def cart_add(pid):
    p = get_producto(pid)
    if not p:
        flash("Producto no encontrado", "error")
        return redirect(url_for("remeras"))

    cart = get_cart()
    key = str(pid)
    cart[key] = cart.get(key, 0) + 1
    save_cart(cart)
    return redirect(url_for("carrito"))


@app.post("/cart/remove/<int:pid>")
def cart_remove(pid):
    cart = get_cart()
    key = str(pid)
    cart.pop(key, None)
    save_cart(cart)
    return redirect(url_for("carrito"))


@app.post("/cart/update/<int:pid>")
def cart_update(pid):
    qty_str = request.form.get("qty", "1")
    try:
        qty = int(qty_str)
    except ValueError:
        qty = 1

    cart = get_cart()
    key = str(pid)

    if qty <= 0:
        cart.pop(key, None)
    else:
        cart[key] = qty

    save_cart(cart)
    return redirect(url_for("carrito"))


@app.route("/carrito")
def carrito():
    items, total = build_cart_items()
    return render_template("carrito.html", items=items, total=total)


# ----------------------------
# CHECKOUT / PAGO
# ----------------------------
@app.route("/checkout")
def checkout():
    cart = get_cart()
    if not cart:
        return redirect(url_for("remeras"))

    items, subtotal = build_cart_items()

    envio_cp = session.get("envio_cp", "")
    envio_costo = int(session.get("envio_costo", 0) or 0)

    # Si supera el umbral, forzamos envío gratis aunque antes haya cotizado con costo
    if int(subtotal) >= ENVIO_GRATIS_DESDE:
        envio_costo = 0
        session["envio_costo"] = 0
        session.modified = True

    total_final = int(subtotal) + int(envio_costo)

    email_prefill = current_user.email if current_user.is_authenticated else ""
    return render_template(
        "checkout.html",
        items=items,
        subtotal=subtotal,
        envio_cp=envio_cp,
        envio_costo=envio_costo,
        total=total_final,
        email_prefill=email_prefill,
    )


@app.post("/checkout/crear-pedido")
def crear_pedido():
    nombre = request.form.get("nombre", "").strip()
    email = request.form.get("email", "").strip()
    direccion = request.form.get("direccion", "").strip()

    if not nombre or not email or not direccion:
        flash("Completá nombre, email y dirección", "error")
        return redirect(url_for("checkout"))

    session["checkout_nombre"] = nombre
    session["checkout_email"] = email
    session["checkout_direccion"] = direccion
    session.modified = True

    return redirect(url_for("pagar"))


@app.route("/pagar")
def pagar():
    return render_template("pagar.html")


# ----------------------------
# MERCADO PAGO - CHECKOUT PRO
# ----------------------------
@app.post("/mp/crear-preferencia")
def mp_crear_preferencia():
    print("== MP: entro a crear-preferencia ==")

    if not MP_ACCESS_TOKEN:
        flash("Falta configurar MP_ACCESS_TOKEN en .env", "error")
        return redirect(url_for("pagar"))

    items, subtotal = build_cart_items()
    print("Items:", len(items), "Subtotal:", subtotal)

    if not items:
        flash("El carrito está vacío", "error")
        return redirect(url_for("remeras"))

    envio_costo = int(session.get("envio_costo", 0) or 0)
    if int(subtotal) >= ENVIO_GRATIS_DESDE:
        envio_costo = 0
        session["envio_costo"] = 0
        session.modified = True

    mp_items = [
        {
            "title": it["p"]["nombre"],
            "quantity": int(it["qty"]),
            "unit_price": float(it["p"]["precio"]),
            "currency_id": "ARS",
        }
        for it in items
    ]

    if envio_costo > 0:
        mp_items.append({
            "title": "Envío (Correo Argentino)",
            "quantity": 1,
            "unit_price": float(envio_costo),
            "currency_id": "ARS",
        })

    preference_data = {
        "items": mp_items,
        "back_urls": {
            "success": url_for("pago_success", _external=True),
            "pending": url_for("pago_pending", _external=True),
            "failure": url_for("pago_failure", _external=True),
        },
    }

    pref = sdk.preference().create(preference_data)

    init_point = pref.get("response", {}).get("init_point")
    print("init_point:", init_point)

    if not init_point:
        flash("No se pudo iniciar el pago (revisá credenciales)", "error")
        return redirect(url_for("pagar"))

    return redirect(init_point)


@app.get("/pago/success")
def pago_success():
    nombre = session.get("checkout_nombre", "")
    email = session.get("checkout_email", "")
    direccion = session.get("checkout_direccion", "")

    envio_cp = session.get("envio_cp", "")
    envio_costo = int(session.get("envio_costo", 0) or 0)

    items, subtotal = build_cart_items()
    if int(subtotal) >= ENVIO_GRATIS_DESDE:
        envio_costo = 0

    total_final = int(subtotal) + int(envio_costo)

    mp_payment_id = request.args.get("payment_id")
    mp_status = request.args.get("status")
    mp_preference_id = request.args.get("preference_id")

    orden_id = f"CIAM-{datetime.now().strftime('%Y%m%d')}-{random.randint(1000, 9999)}"

    session.pop("cart", None)
    session.pop("envio_cp", None)
    session.pop("envio_costo", None)
    session.pop("checkout_nombre", None)
    session.pop("checkout_email", None)
    session.pop("checkout_direccion", None)
    session.modified = True

    return render_template(
        "pago_resultado.html",
        estado="success",
        orden_id=orden_id,
        nombre=nombre,
        email=email,
        direccion=direccion,
        envio_cp=envio_cp,
        envio_costo=envio_costo,
        items=items,
        subtotal=subtotal,
        total=total_final,
        mp_payment_id=mp_payment_id,
        mp_status=mp_status,
        mp_preference_id=mp_preference_id,
    )


@app.get("/pago/pending")
def pago_pending():
    return render_template("pago_resultado.html", estado="pending")


@app.get("/pago/failure")
def pago_failure():
    return render_template("pago_resultado.html", estado="failure")


# ----------------------------
# AUTH
# ----------------------------
@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("account"))

    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        password2 = request.form.get("password2") or ""

        if not email or not password:
            flash("Completá email y contraseña.", "error")
            return render_template("register.html")

        if password != password2:
            flash("Las contraseñas no coinciden.", "error")
            return render_template("register.html")

        if User.query.filter_by(email=email).first():
            flash("Ese email ya está registrado.", "error")
            return render_template("register.html")

        user = User(email=email)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()

        login_user(user)
        flash("Cuenta creada. ¡Bienvenido!", "success")
        return redirect(url_for("account"))

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("account"))

    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""

        user = User.query.filter_by(email=email).first()
        if not user or not user.check_password(password):
            flash("Email o contraseña incorrectos.", "error")
            return render_template("login.html")

        login_user(user)
        flash("Sesión iniciada.", "success")
        return redirect(url_for("account"))

    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    flash("Sesión cerrada.", "info")
    return redirect(url_for("index"))


@app.route("/account")
@login_required
def account():
    return render_template("account.html")


if __name__ == "__main__":
    app.run(debug=True)
