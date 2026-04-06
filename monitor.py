#!/usr/bin/env python3
"""
Mango Outlet Monitor
Detecta artículos nuevos y bajadas de precio en la sección de hombre
que cumplen criterios de talla y descuento, y notifica por email.
"""

import os
import re
import json
import time
import smtplib
import requests
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime, timezone

# ── Configuración ──────────────────────────────────────────────────────────────
SEARCH_URL = (
    "https://www.mangooutlet.com/es/es/search/hombre"
    "?q=hombre&order=asc&filters=sizes%7EM_L_42_43_48_50_90_Talla%2B%C3%BAnica"
)
# Sección Premium (Boglioli, Selection, materiales nobles): sin filtro de talla
# para capturar todos los IDs y cruzarlos con el catálogo principal.
PREMIUM_URL  = "https://www.mangooutlet.com/es/es/c/hombre/premium/a634af37"
PRICES_URL   = "https://online-orchestrator.mangooutlet.com/v3/prices/products"
PRODUCTS_URL = "https://online-orchestrator.mangooutlet.com/v4/products"
IMAGE_BASE   = "https://media.mangooutlet.com"
PRODUCT_BASE = "https://www.mangooutlet.com"
STATE_FILE   = "state.json"

GMAIL_USER   = os.environ["GMAIL_USER"]
GMAIL_PASS   = os.environ["GMAIL_PASS"]
NOTIFY_EMAIL = os.environ["NOTIFY_EMAIL"]

# Tallas objetivo (el resto se ignora aunque aparezca en el HTML)
TARGET_SIZES = {"M", "L", "Talla única", "42", "43", "48", "50", "90"}

# Palabras clave que reducen el umbral de descuento a THRESHOLD_KEYWORD
KEYWORDS = ["selection", "boglioli", "merino", "cashmere", "lana virgen", "cuero", "piel"]

# Si el nombre contiene alguno de estos términos, se anula el match de keywords
# (p.ej. "efecto piel" es imitación, no cuero real)
KEYWORD_EXCLUSIONS = ["efecto piel", "similpiel"]

THRESHOLD_GENERAL    = 70.0   # % de descuento mínimo para artículos normales
THRESHOLD_KEYWORD    = 50.0   # % de descuento mínimo para artículos con keyword
PRICE_DROP_MIN_DELTA = 5.0    # mejora mínima (puntos) para volver a notificar un artículo ya notificado
API_DELAY_S          = 0.3    # pausa entre llamadas a la API de precios (para no saturar)
# ───────────────────────────────────────────────────────────────────────────────

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "es-ES,es;q=0.9",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.mangooutlet.com/",
}


# ── Extracción de datos ────────────────────────────────────────────────────────

def fetch_catalog() -> list[dict]:
    """Descarga la página de búsqueda y extrae todos los productos del JSON incrustado."""
    resp = requests.get(SEARCH_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    html = resp.text

    # Los productos están incrustados en el HTML como JSON doblemente escapado:
    # {\"productId\":\"12345678\",\"colorGroup\":\"14\",\"sizes\":[\"M\",\"L\"],\"price\":27.99,\"onSale\":true,...}
    pattern = (
        r'\{\\"productId\\":\\"(\d+)\\"'
        r'(?:,\\"colorGroup\\":\\"[^"]*\\")?'
        r',\\"sizes\\":\[([^\]]*)\]'
        r',\\"price\\":(\d+\.?\d*)'
        r',\\"onSale\\":(true|false)'
        r'[^}]*\}'
    )

    seen = set()
    products = []
    for m in re.finditer(pattern, html):
        pid = m.group(1)
        if pid in seen:
            continue
        seen.add(pid)

        raw = m.group(2)
        # Extraer tallas limpiando las comillas escapadas
        sizes = [s.strip().strip('\\"') for s in raw.split(',') if s.strip()]
        price   = float(m.group(3))
        on_sale = m.group(4) == "true"

        products.append({"productId": pid, "sizes": sizes, "price": price, "onSale": on_sale})

    if not products:
        raise ValueError("No se encontraron productos en el HTML. ¿Ha cambiado la estructura de la página?")

    print(f"  -> {len(products)} productos extraídos del HTML")
    return products


def fetch_premium_ids() -> set[str]:
    """Descarga la sección Premium y devuelve el set de productIds que aparecen en ella.
    Devuelve un set vacío si la petición falla o la estructura ha cambiado."""
    try:
        resp = requests.get(PREMIUM_URL, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        ids = {m.group(1) for m in re.finditer(r'\\"productId\\":\\"(\d+)\\"', resp.text)}
        print(f"  -> {len(ids)} productos en sección Premium")
        if not ids:
            print("     Aviso: ningún productId encontrado en Premium. "
                  "¿Ha cambiado la estructura de la página?")
        return ids
    except Exception as e:
        print(f"  -> Aviso: no se pudo cargar la sección Premium: {e}")
        return set()


def filter_by_sizes(products: list[dict]) -> list[dict]:
    """Mantiene solo los productos que tienen al menos una talla objetivo."""
    return [p for p in products if any(s in TARGET_SIZES for s in p["sizes"])]


def fetch_price(product_id: str) -> dict | None:
    """Obtiene discountRate y precios detallados de un producto desde la API."""
    try:
        resp = requests.get(
            PRICES_URL,
            params={"channelId": "outlet", "countryIso": "ES", "productId": product_id},
            headers=HEADERS,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        if not data:
            return None
        # La respuesta es {colorId: {price, previousPrices, discountRate, ...}}
        # Nos quedamos con el color que tenga mayor descuento
        return max(data.values(), key=lambda x: x.get("discountRate", 0.0))
    except Exception as e:
        print(f"     Aviso: no se pudo obtener precio de {product_id}: {e}")
        return None


def fetch_product_details(product_id: str) -> dict | None:
    """Obtiene nombre, URL e imagen de un producto desde la API de detalles."""
    try:
        resp = requests.get(
            PRODUCTS_URL,
            params={
                "channelId": "outlet",
                "countryIso": "ES",
                "languageIso": "es",
                "productId": product_id,
            },
            headers=HEADERS,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()

        name = data.get("name", product_id)
        url  = PRODUCT_BASE + data.get("url", "")

        # Buscar la primera imagen frontal disponible
        image_url = None
        for color in data.get("colors", []):
            for look in color.get("looks", {}).values():
                for img_key in ("F", "052", "B", "051"):
                    img = look.get("images", {}).get(img_key)
                    if img and isinstance(img, dict) and "img" in img:
                        image_url = f"{IMAGE_BASE}{img['img']}?wid=240&fit=crop,1"
                        break
                if image_url:
                    break
            if image_url:
                break

        return {"name": name, "url": url, "image_url": image_url}
    except Exception as e:
        print(f"     Aviso: no se pudieron obtener detalles de {product_id}: {e}")
        return None


# ── Lógica de umbrales y keywords ──────────────────────────────────────────────

# Set de productIds de la sección Premium; se rellena en main() antes del bucle.
_premium_ids: set[str] = set()


def get_keyword(name: str, product_id: str = "") -> str | None:
    """Devuelve el keyword/motivo especial del artículo, o None si no aplica.
    Prioridad: 1) exclusiones anulan todo, 2) ID en sección Premium, 3) keyword en nombre."""
    name_lower = name.lower()
    if any(excl in name_lower for excl in KEYWORD_EXCLUSIONS):
        return None
    if product_id and product_id in _premium_ids:
        return "premium"
    for kw in KEYWORDS:
        if kw.lower() in name_lower:
            return kw
    return None


def applicable_threshold(name: str, product_id: str = "") -> float:
    return THRESHOLD_KEYWORD if get_keyword(name, product_id) else THRESHOLD_GENERAL


def meets_threshold(discount_rate: float, name: str, product_id: str = "") -> bool:
    return discount_rate >= applicable_threshold(name, product_id)


# ── Estado ─────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {"products": {}}
    with open(STATE_FILE) as f:
        data = json.load(f)
    return data if "products" in data else {"products": {}}


def save_state(state: dict):
    state["last_updated"] = datetime.now(timezone.utc).isoformat()
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


# ── Email ──────────────────────────────────────────────────────────────────────

def build_card(item: dict) -> str:
    kw     = get_keyword(item.get("name", ""), item.get("productId", ""))
    is_hot = kw is not None

    border = "#C9972B" if is_hot else "#e0e0e0"
    bg     = "#FFFBF0" if is_hot else "#ffffff"
    badge  = (
        f'<span style="background:#C9972B;color:#fff;font-size:11px;padding:2px 8px;'
        f'border-radius:10px;font-weight:bold;vertical-align:middle;margin-left:6px;">'
        f'★ {kw.upper()}</span>'
    ) if is_hot else ""

    label = "🔻 BAJADA DE PRECIO" if item["change_type"] == "DROP" else "🆕 NUEVO"
    image = (
        f'<img src="{item["image_url"]}" width="90" height="120" '
        f'style="object-fit:cover;border-radius:4px;display:block;" />'
    ) if item.get("image_url") else (
        '<div style="width:90px;height:120px;background:#f0f0f0;border-radius:4px;"></div>'
    )

    return f"""
    <tr>
      <td style="padding:12px;background:{bg};border:1.5px solid {border};
                 border-radius:8px;">
        <table width="100%" cellpadding="0" cellspacing="0"><tr>
          <td width="100" valign="top">{image}</td>
          <td valign="top" style="padding-left:14px;">
            <div style="font-size:12px;color:#888;margin-bottom:4px;">{label}</div>
            <div style="font-size:15px;font-weight:bold;color:#1a1a1a;line-height:1.3;">
              <a href="{item['url']}" style="color:#1a1a1a;text-decoration:none;">{item['name']}</a>
              {badge}
            </div>
            <div style="margin-top:10px;">
              <span style="font-size:26px;font-weight:900;color:#c0392b;">
                -{item['discountRate']:.0f}%
              </span>
              <span style="font-size:19px;font-weight:bold;color:#c0392b;margin-left:8px;">
                {item['price']:.2f}€
              </span>
              <span style="font-size:13px;color:#aaa;text-decoration:line-through;margin-left:8px;">
                {item['originalPrice']:.2f}€
              </span>
            </div>
          </td>
        </tr></table>
      </td>
    </tr>
    <tr><td height="8"></td></tr>"""


def send_email(notify_items: list[dict]):
    now   = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
    cards = "".join(build_card(i) for i in notify_items)

    new_c  = sum(1 for i in notify_items if i["change_type"] == "NEW")
    drop_c = sum(1 for i in notify_items if i["change_type"] == "DROP")
    parts  = []
    if new_c:
        parts.append(f"{new_c} nuevo(s)")
    if drop_c:
        parts.append(f"{drop_c} bajada(s) de precio")
    subject = "Mango Outlet Hombre: " + " · ".join(parts)

    html_body = f"""
    <html><body style="font-family:Arial,sans-serif;max-width:620px;margin:auto;
        background:#f4f4f4;padding:20px;">
      <div style="background:#fff;border-radius:10px;padding:24px;">
        <h2 style="margin:0 0 4px;color:#1a1a1a;">Mango Outlet — Hombre</h2>
        <p style="margin:0 0 20px;color:#666;font-size:13px;">
          {len(notify_items)} artículo(s) · {now}
        </p>
        <table width="100%" cellpadding="0" cellspacing="0">{cards}</table>
        <p style="text-align:center;margin-top:24px;">
          <a href="{SEARCH_URL}"
             style="background:#1a1a1a;color:#fff;padding:12px 28px;
                    text-decoration:none;border-radius:6px;font-weight:bold;">
            Ver sección hombre
          </a>
        </p>
        <p style="text-align:center;font-size:11px;color:#ccc;margin-top:20px;">
          Mango Outlet Monitor · GitHub Actions
        </p>
      </div>
    </body></html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = GMAIL_USER
    msg["To"]      = NOTIFY_EMAIL
    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_USER, GMAIL_PASS)
        server.sendmail(GMAIL_USER, NOTIFY_EMAIL, msg.as_string())

    print(f"  -> Email enviado: {subject}")


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    print(f"[{datetime.now(timezone.utc).isoformat()}] Comprobando Mango Outlet...")

    # 1. Obtener catálogo, filtrar por talla, y cargar IDs de la sección Premium
    all_products = fetch_catalog()
    sized        = filter_by_sizes(all_products)
    print(f"  -> {len(sized)} productos con tallas objetivo")

    global _premium_ids
    _premium_ids = fetch_premium_ids()

    # 2. Cargar estado anterior
    state  = load_state()
    stored = state["products"]

    # 3. Evaluar cada producto
    notify_items = []
    new_stored   = {}
    price_calls  = 0

    for p in sized:
        pid        = p["productId"]
        html_price = p["price"]
        prev       = stored.get(pid)

        # ── Optimización: saltar si el precio del HTML no cambió ──────────────
        # El precio en el HTML es el rebajado actual. Si no cambió desde la última
        # ejecución, el discountRate tampoco ha cambiado → no hay nada nuevo.
        if (
            prev
            and abs(prev.get("html_price", -1) - html_price) < 0.001
            and "discountRate" in prev
        ):
            new_stored[pid] = prev
            continue

        # ── Llamada a la API de precios ───────────────────────────────────────
        price_data = fetch_price(pid)
        price_calls += 1
        time.sleep(API_DELAY_S)

        if not price_data:
            if prev:
                new_stored[pid] = prev
            continue

        discount_rate    = price_data.get("discountRate", 0.0)
        current_price    = price_data.get("price", html_price)
        original_price   = price_data.get("previousPrices", {}).get("originalShop", 0.0)
        prev_notified    = prev.get("notified_rate") if prev else None

        # Nombre e imagen: del estado si ya los teníamos
        name      = prev.get("name")      if prev else None
        image_url = prev.get("image_url") if prev else None
        url       = prev.get("url")       if prev else None

        # ── ¿Debemos notificar? ───────────────────────────────────────────────
        # Notificamos si:
        #   a) El descuento supera el umbral mínimo posible (50%), Y
        #   b) Es un producto nuevo O mejoró al menos PRICE_DROP_MIN_DELTA puntos
        #      respecto a la última vez que notificamos
        is_improvement = (
            prev_notified is None
            or discount_rate >= prev_notified + PRICE_DROP_MIN_DELTA
        )
        might_notify = is_improvement and discount_rate >= THRESHOLD_KEYWORD

        # Tipo de cambio: DROP solo si ya habíamos notificado antes
        change_type = "DROP" if (prev is not None and prev_notified is not None) else "NEW"

        # Obtener detalles si es candidato y no tenemos nombre
        if might_notify and name is None:
            details = fetch_product_details(pid)
            price_calls += 1
            time.sleep(API_DELAY_S)
            if details:
                name      = details["name"]
                image_url = details["image_url"]
                url       = details["url"]

        # Aplicar umbral definitivo (con nombre real o fallback al ID)
        should_notify = might_notify and meets_threshold(discount_rate, name or pid, pid)

        # ── Guardar en nuevo estado ───────────────────────────────────────────
        new_stored[pid] = {
            "html_price":    html_price,
            "discountRate":  discount_rate,
            "price":         current_price,
            "originalPrice": original_price,
            "notified_rate": discount_rate if should_notify else prev_notified,
            "name":          name,
            "image_url":     image_url,
            "url":           url,
        }

        if should_notify:
            notify_items.append({
                "productId":    pid,
                "name":         name or pid,
                "price":        current_price,
                "originalPrice":original_price,
                "discountRate": discount_rate,
                "image_url":    image_url,
                "url":          url or "#",
                "change_type":  change_type,
            })

    print(f"  -> {price_calls} llamadas a la API de precios realizadas")

    # 4. Ordenar: keywords/premium primero (borde dorado), luego por descuento descendente
    notify_items.sort(
        key=lambda x: (0 if get_keyword(x.get("name", ""), x.get("productId", "")) else 1,
                       -x["discountRate"])
    )

    # 5. Guardar estado (solo productos que siguen en el catálogo)
    state["products"] = new_stored
    save_state(state)

    if notify_items:
        print(f"  -> {len(notify_items)} artículo(s) a notificar")
        send_email(notify_items)
    else:
        print("  -> Sin cambios relevantes.")


if __name__ == "__main__":
    main()
