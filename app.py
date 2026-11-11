# -*- coding: utf-8 -*-
"""
Criador de anúncios Shopee  -  serviço SEPARADO do painel agente-shopee.

Fluxo:
  1. Analisar: compara os anúncios ativos do Mercado Livre (conta Duplo Giro) com os SKUs da Shopee.
  2. Para cada SKU que falta, busca custo/peso/medidas no Bling da M3 e calcula o preço.
  3. Você marca os itens e envia de 50 em 50 (fotos e descrição vêm do Mercado Livre).

Modo simulação (DRY_RUN=1, padrão): não cria nada na Shopee, só mostra o que seria enviado.
"""
import os, json, time, hmac, hashlib, threading, math, re, sqlite3, base64, unicodedata
from datetime import datetime, timedelta
from urllib.parse import urlencode

import requests
from flask import Flask, request, redirect, jsonify, Response, render_template_string

app = Flask(__name__)

# ----------------------------------------------------------------------------
# Configuração (variáveis de ambiente no Render)
# ----------------------------------------------------------------------------
APP_BASE_URL = os.environ.get("APP_BASE_URL", "").rstrip("/")
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASS = os.environ.get("ADMIN_PASS", "")
SYNC_TOKEN = os.environ.get("SYNC_TOKEN", "")   # chave para o cron-job.org chamar /sync-estoque
LOCAL_DEV = os.environ.get("LOCAL_DEV") == "1"
DRY_RUN_ENV = os.environ.get("DRY_RUN", "1") != "0"

SHOPEE_HOST = os.environ.get("SHOPEE_HOST", "https://partner.shopeemobile.com")
SHOPEE_PID = int(os.environ.get("SHOPEE_PARTNER_ID", "0") or 0)
SHOPEE_KEY = os.environ.get("SHOPEE_PARTNER_KEY", "")

ML_ID = os.environ.get("ML_CLIENT_ID", "")
ML_SECRET = os.environ.get("ML_CLIENT_SECRET", "")

BLING_ID = os.environ.get("BLING_CLIENT_ID", "")      # app criado no Bling da M3
BLING_SECRET = os.environ.get("BLING_CLIENT_SECRET", "")

DATABASE_URL = os.environ.get("DATABASE_URL", "")
USE_PG = DATABASE_URL.startswith("postgres")
if USE_PG:
    import psycopg2

MAX_LOTE = 50

DEFAULTS = {
    "prefixos": "SM",            # só SKUs que começam com isso (vírgula separa; vazio = todos)
    "lucro": 20.0,               # % do valor anunciado
    "promo": 8.0,                # % da promoção semanal
    "imposto": 9.45,             # % sobre o preço pago (pior cenário) - ajuste aqui
    "piso": 29.99,               # nunca anunciar abaixo disso
    "categoria_id": "",          # vazio = Shopee sugere pelo título
    "peso_padrao": 0.3,          # kg, se o Bling não tiver
    "dim_padrao": [20, 15, 10],  # comprimento, largura, altura (cm)
    "criar_sem_estoque": False,
    "bling_loja_id": "",         # ID da loja Shopee no Bling M3 (vazio = descobrir sozinho)
    "dias_cobertura": 45,        # meta de cobertura do Full (1,5 mês)
    "alerta_acima_ml": 15.0,     # % acima do preço do ML que acende o alerta
    "atributos": {"bluetooth": "Não"},   # nome do atributo obrigatório -> valor
}

# Tabela de taxas Shopee 2026 pelo preço pago pelo cliente (já com desconto)
TIERS = [
    (0.00, 79.99, 0.20, 4.0),
    (80.00, 99.99, 0.14, 16.0),
    (100.00, 199.99, 0.14, 20.0),
    (200.00, 1e12, 0.14, 26.0),
]

# ----------------------------------------------------------------------------
# Banco (Neon/Postgres no Render; SQLite local para testes)
# ----------------------------------------------------------------------------
def _conn():
    if USE_PG:
        return psycopg2.connect(DATABASE_URL)
    return sqlite3.connect(os.environ.get("SQLITE_PATH", "criador.db"))


def run(sql, args=(), fetch=False):
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(sql.replace("?", "%s") if USE_PG else sql, args)
        rows = cur.fetchall() if fetch else None
        conn.commit()
        return rows
    finally:
        conn.close()


def init_db():
    run("CREATE TABLE IF NOT EXISTS cria_kv (k TEXT PRIMARY KEY, v TEXT)")
    run("CREATE TABLE IF NOT EXISTS cria_cand (sku TEXT PRIMARY KEY, data TEXT, status TEXT, msg TEXT, updated TEXT)")


def kv_get(k, default=None):
    rows = run("SELECT v FROM cria_kv WHERE k=?", (k,), True)
    return json.loads(rows[0][0]) if rows else default


def kv_set(k, v):
    run("DELETE FROM cria_kv WHERE k=?", (k,))
    run("INSERT INTO cria_kv (k, v) VALUES (?, ?)", (k, json.dumps(v)))


def settings():
    s = dict(DEFAULTS)
    s.update(kv_get("settings", {}) or {})
    return s


def now():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


def norm(t):
    t = unicodedata.normalize("NFKD", str(t or ""))
    return "".join(c for c in t if not unicodedata.combining(c)).strip().lower()


# ----------------------------------------------------------------------------
# Preço: preço anunciado P tal que
#   P - promo*P - taxa(Shopee sobre o preço pago) - imposto - custo = lucro*P
# ----------------------------------------------------------------------------
def calc_price(custo, s):
    lucro, promo, imp = s["lucro"] / 100.0, s["promo"] / 100.0, s["imposto"] / 100.0
    best = None
    for lo, hi, com, fixo in TIERS:
        den = (1 - promo) * (1 - com - imp) - lucro
        if den <= 0:
            continue
        p = (fixo + custo) / den
        pago = p * (1 - promo)
        if pago < lo:                       # cai abaixo da faixa: sobe até a faixa começar
            best = lo / (1 - promo)
            break
        if pago <= hi + 0.009:
            best = p
            break
    if best is None:
        return None
    best = max(best, float(s["piso"]))
    p99 = math.floor(best) + 0.99
    if p99 < best - 1e-9:
        p99 += 1
    return round(p99, 2)


def breakdown(preco, custo, s):
    promo, imp = s["promo"] / 100.0, s["imposto"] / 100.0
    pago = preco * (1 - promo)
    for lo, hi, com, fixo in TIERS:
        if pago <= hi + 0.009:
            taxa = pago * com + fixo
            break
    sobra = pago - taxa - pago * imp - custo
    return {"pago": round(pago, 2), "taxa": round(taxa, 2), "lucro": round(sobra, 2),
            "lucro_pct": round(sobra / preco * 100, 1) if preco else 0}


# ----------------------------------------------------------------------------
# Shopee
# ----------------------------------------------------------------------------
def sp_sign(path, ts, token="", shop=""):
    base = f"{SHOPEE_PID}{path}{ts}{token}{shop}"
    return hmac.new(SHOPEE_KEY.encode(), base.encode(), hashlib.sha256).hexdigest()


def sp_auth_url():
    path = "/api/v2/shop/auth_partner"
    ts = int(time.time())
    q = {"partner_id": SHOPEE_PID, "timestamp": ts, "sign": sp_sign(path, ts),
         "redirect": APP_BASE_URL + "/callback/shopee"}
    return SHOPEE_HOST + path + "?" + urlencode(q)


def sp_save_token(r, shop_id):
    kv_set("shopee_tok", {"access_token": r["access_token"], "refresh_token": r["refresh_token"],
                          "expires_at": time.time() + int(r.get("expire_in", 14400)), "shop_id": int(shop_id)})


def sp_exchange(code, shop_id):
    path = "/api/v2/auth/token/get"
    ts = int(time.time())
    r = requests.post(SHOPEE_HOST + path, params={"partner_id": SHOPEE_PID, "timestamp": ts, "sign": sp_sign(path, ts)},
                      json={"code": code, "shop_id": int(shop_id), "partner_id": SHOPEE_PID}, timeout=30).json()
    if r.get("error"):
        raise RuntimeError(f"Shopee: {r.get('error')} {r.get('message')}")
    sp_save_token(r, shop_id)


def sp_token():
    t = kv_get("shopee_tok")
    if not t:
        raise RuntimeError("Shopee não conectada")
    if t["expires_at"] - time.time() < 300:
        path = "/api/v2/auth/access_token/get"
        ts = int(time.time())
        r = requests.post(SHOPEE_HOST + path, params={"partner_id": SHOPEE_PID, "timestamp": ts, "sign": sp_sign(path, ts)},
                          json={"refresh_token": t["refresh_token"], "shop_id": t["shop_id"], "partner_id": SHOPEE_PID},
                          timeout=30).json()
        if r.get("error"):
            raise RuntimeError(f"Shopee (renovar token): {r.get('error')} {r.get('message')}")
        sp_save_token(r, t["shop_id"])
        t = kv_get("shopee_tok")
    return t


def sp(path, method="GET", params=None, body=None):
    t = sp_token()
    ts = int(time.time())
    q = {"partner_id": SHOPEE_PID, "timestamp": ts, "access_token": t["access_token"], "shop_id": t["shop_id"],
         "sign": sp_sign(path, ts, t["access_token"], t["shop_id"])}
    if params:
        q.update(params)
    if method == "GET":
        r = requests.get(SHOPEE_HOST + path, params=q, timeout=60)
    else:
        r = requests.post(SHOPEE_HOST + path, params=q, json=body, timeout=60)
    try:
        j = r.json()
    except Exception:
        raise RuntimeError(f"Shopee {path}: resposta inválida ({r.status_code})")
    if j.get("error"):
        raise RuntimeError(f"Shopee {path}: {j.get('error')} - {j.get('message')}")
    return j


def foto_jpeg(content):
    """Converte a foto do ML (muitas vezes webp) para JPEG que a Shopee aceita."""
    try:
        import io
        from PIL import Image
    except ImportError:
        return content
    im = Image.open(io.BytesIO(content))
    if im.mode != "RGB":
        im = im.convert("RGB")
    w, h = im.size
    if min(w, h) < 500:
        f = 500.0 / min(w, h)
        im = im.resize((int(w * f) + 1, int(h * f) + 1))
    if max(im.size) > 2000:
        im.thumbnail((2000, 2000))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=92)
    return buf.getvalue()


def sp_upload_image(content, name):
    path = "/api/v2/media_space/upload_image"
    ts = int(time.time())
    q = {"partner_id": SHOPEE_PID, "timestamp": ts, "sign": sp_sign(path, ts)}
    r = requests.post(SHOPEE_HOST + path, params=q, files={"image": (name, content)}, timeout=90).json()
    if r.get("error"):
        raise RuntimeError(f"upload de foto: {r.get('error')} - {r.get('message')}")
    return r["response"]["image_info"]["image_id"]


def shopee_skus():
    """Todos os SKUs (anúncio e variações) já existentes na Shopee."""
    ids = []
    for st in ("NORMAL", "UNLIST", "REVIEWING"):
        off = 0
        while True:
            r = sp("/api/v2/product/get_item_list", params={"offset": off, "page_size": 100, "item_status": st})
            rr = r.get("response") or {}
            ids += [i["item_id"] for i in rr.get("item", [])]
            if rr.get("has_next_page"):
                off = rr["next_offset"]
            else:
                break
    skus, com_modelo = set(), []
    for i in range(0, len(ids), 50):
        chunk = ids[i:i + 50]
        r = sp("/api/v2/product/get_item_base_info", params={"item_id_list": ",".join(map(str, chunk))})
        for it in (r.get("response") or {}).get("item_list", []):
            if it.get("item_sku"):
                skus.add(norm(it["item_sku"]))
            if it.get("has_model"):
                com_modelo.append(it["item_id"])
    for iid in com_modelo:
        r = sp("/api/v2/product/get_model_list", params={"item_id": iid})
        for m in (r.get("response") or {}).get("model", []):
            if m.get("model_sku"):
                skus.add(norm(m["model_sku"]))
    return skus, len(ids)


_attr_cache, _logi_cache = {}, {}


def _nm(x):
    """Nome de atributo/valor: aceita name, display_name ou multi_lang."""
    if not isinstance(x, dict):
        return ""
    n = x.get("display_name") or x.get("name") or x.get("display_attribute_name") or x.get("original_name") or ""
    if n:
        return str(n)
    for ml in x.get("multi_lang") or []:
        if ml.get("value"):
            return str(ml["value"])
    return ""


def sp_attributes(cat):
    """Atributos da categoria (get_attribute_tree), no formato que resolve_attrs espera."""
    if cat not in _attr_cache:
        r = sp("/api/v2/product/get_attribute_tree", params={"category_id_list": str(cat)})
        resp = r.get("response") or {}
        trees = []
        for it in resp.get("list") or []:
            trees += it.get("attribute_tree") or []
        trees += resp.get("attribute_tree") or []
        out = []
        for a in trees:
            vals = [{"value_id": v.get("value_id"), "display_value_name": _nm(v), "original_value_name": _nm(v)}
                    for v in (a.get("attribute_value_list") or []) if v.get("value_id") is not None]
            out.append({"attribute_id": a.get("attribute_id"),
                        "is_mandatory": bool(a.get("mandatory", a.get("is_mandatory"))),
                        "display_attribute_name": _nm(a), "attribute_value_list": vals})
        _attr_cache[cat] = out
    return _attr_cache[cat]


def sp_logistics():
    if "l" not in _logi_cache:
        r = sp("/api/v2/logistics/get_channel_list")
        ch = (r.get("response") or {}).get("logistics_channel_list", [])
        _logi_cache["l"] = [{"logistic_id": c["logistics_channel_id"], "enabled": True} for c in ch if c.get("enabled")]
    return _logi_cache["l"]


_cat_cache = {}


def sp_categories():
    if "c" not in _cat_cache:
        try:
            r = sp("/api/v2/product/get_category", params={"language": "pt-BR"})
        except Exception:
            r = sp("/api/v2/product/get_category")
        _cat_cache["c"] = (r.get("response") or {}).get("category_list", [])
    return _cat_cache["c"]


def sp_category(title, s):
    if str(s.get("categoria_id") or "").strip():
        return int(s["categoria_id"])
    r = sp("/api/v2/product/category_recommend", params={"item_name": title[:120]})
    ids = (r.get("response") or {}).get("category_id") or []
    if not ids:
        raise RuntimeError("Shopee não sugeriu categoria; defina o ID da categoria nas configurações")
    return int(ids[0])


NEG = {"nao", "no", "none", "sem", "nenhum", "nao possui", "nao tem"}


def resolve_attrs(cat, s, sku=""):
    """Preenche os atributos obrigatórios (ex.: Bluetooth). Retorna (lista, problemas)."""
    ov = {norm(k): v for k, v in (s.get("atributos") or {}).items()}
    out, problems = [], []
    for a in sp_attributes(cat):
        if not a.get("is_mandatory"):
            continue
        nome = a.get("display_attribute_name") or a.get("original_attribute_name") or str(a.get("attribute_id"))
        nn = norm(nome)
        want = ov.get(nn)
        if want is None:
            want = next((v for k, v in ov.items() if k and k in nn), None)
        if want is None and any(t in nn for t in ("part number", "numero da peca", "numero de peca", "codigo da peca")):
            want = sku
        if want is not None:
            want = str(want).replace("{sku}", sku)
        vals = a.get("attribute_value_list") or []
        if want is None:
            op = ", ".join((v.get("display_value_name") or v.get("original_value_name") or "?") for v in vals[:12])
            problems.append(f"atributo obrigatório '{nome}' sem valor definido" + (f" (opções: {op})" if op else ""))
            continue
        if not vals:   # campo de texto livre
            out.append({"attribute_id": a["attribute_id"],
                        "attribute_value_list": [{"value_id": 0, "original_value_name": str(want)}]})
            continue
        w = norm(want)
        match = None
        for v in vals:
            names = {norm(v.get("display_value_name")), norm(v.get("original_value_name"))}
            if w in names or (w in NEG and names & NEG):
                match = v
                break
        if not match:
            op = ", ".join((v.get("display_value_name") or v.get("original_value_name") or "?") for v in vals[:12])
            problems.append(f"atributo '{nome}': valor '{want}' não existe (opções: {op})")
            continue
        out.append({"attribute_id": a["attribute_id"],
                    "attribute_value_list": [{"value_id": match["value_id"],
                                              "original_value_name": match.get("original_value_name", "")}]})
    return out, problems


# ----------------------------------------------------------------------------
# Mercado Livre (conta Duplo Giro)
# ----------------------------------------------------------------------------
def ml_auth_url():
    return "https://auth.mercadolivre.com.br/authorization?" + urlencode(
        {"response_type": "code", "client_id": ML_ID, "redirect_uri": APP_BASE_URL + "/callback/ml"})


def ml_post_token(data):
    r = requests.post("https://api.mercadolibre.com/oauth/token", data=data, timeout=30).json()
    if "access_token" not in r:
        raise RuntimeError(f"Mercado Livre: {r.get('message') or r}")
    kv_set("ml_tok", {"access_token": r["access_token"], "refresh_token": r["refresh_token"],
                      "expires_at": time.time() + int(r.get("expires_in", 21600)), "user_id": r.get("user_id")})


def ml_exchange(code):
    ml_post_token({"grant_type": "authorization_code", "client_id": ML_ID, "client_secret": ML_SECRET,
                   "code": code, "redirect_uri": APP_BASE_URL + "/callback/ml"})


def ml_token():
    t = kv_get("ml_tok")
    if not t:
        raise RuntimeError("Mercado Livre não conectado")
    if t["expires_at"] - time.time() < 300:
        ml_post_token({"grant_type": "refresh_token", "client_id": ML_ID, "client_secret": ML_SECRET,
                       "refresh_token": t["refresh_token"]})
        t = kv_get("ml_tok")
    return t


def ml_get(path, params=None):
    t = ml_token()
    for tentativa in range(4):
        r = requests.get("https://api.mercadolibre.com" + path, params=params,
                         headers={"Authorization": "Bearer " + t["access_token"]}, timeout=60)
        if r.status_code == 429:
            time.sleep(2 + tentativa * 2)
            continue
        return r.status_code, r.json()
    raise RuntimeError("Mercado Livre limitou as consultas (429); tente de novo em alguns minutos")


def ml_items(prefixos, log):
    """Anúncios ativos do ML com SKU. Retorna {SKU_NORMALIZADO: dados}."""
    t = ml_token()
    ids, scroll = [], None
    while True:
        p = {"status": "active", "search_type": "scan", "limit": 100}
        if scroll:
            p["scroll_id"] = scroll
        code, r = ml_get(f"/users/{t['user_id']}/items/search", p)
        if code != 200:
            raise RuntimeError(f"ML lista de anúncios: {r.get('message') or code}")
        if not r.get("results"):
            break
        ids += r["results"]
        scroll = r.get("scroll_id")
        log(f"ML: {len(ids)} anúncios ativos lidos")
    pref = [norm(x) for x in re.split(r"[,;\s]+", prefixos or "") if x.strip()]
    out, sem_sku = {}, 0
    for i in range(0, len(ids), 20):
        code, r = ml_get("/items", {"ids": ",".join(ids[i:i + 20]),
                                   "attributes": "id,title,seller_custom_field,pictures,attributes,available_quantity,permalink,status,price"})
        if code != 200:
            continue
        for e in r:
            b = e.get("body") or {}
            sku = b.get("seller_custom_field") or next(
                (a.get("value_name") for a in b.get("attributes", []) if a.get("id") == "SELLER_SKU"), None)
            if not sku:
                sem_sku += 1
                continue
            k = norm(sku)
            if pref and not any(k.startswith(x) for x in pref):
                continue
            fotos = [p.get("secure_url") or p.get("url") for p in b.get("pictures", []) if p.get("secure_url") or p.get("url")]
            d = {"sku": str(sku).strip(), "ml_id": b.get("id"), "titulo": b.get("title", ""),
                 "qtd": int(b.get("available_quantity") or 0), "fotos": fotos, "link": b.get("permalink"),
                 "preco_ml": float(b.get("price") or 0)}
            if k not in out or len(fotos) > len(out[k]["fotos"]):
                out[k] = d
    log(f"ML: {len(out)} SKUs com o prefixo ({sem_sku} anúncios sem SKU ignorados)")
    return out


# ----------------------------------------------------------------------------
# Bling da M3 (custo, peso e medidas)
# ----------------------------------------------------------------------------
def bl_auth_url():
    return "https://www.bling.com.br/Api/v3/oauth/authorize?" + urlencode(
        {"response_type": "code", "client_id": BLING_ID, "state": "criador"})


def bl_post_token(data):
    basic = base64.b64encode(f"{BLING_ID}:{BLING_SECRET}".encode()).decode()
    r = requests.post("https://api.bling.com.br/Api/v3/oauth/token", data=data, timeout=30,
                      headers={"Authorization": "Basic " + basic, "Accept": "1.0"}).json()
    if "access_token" not in r:
        raise RuntimeError(f"Bling: {r.get('error', {}).get('description') or r}")
    kv_set("bling_tok", {"access_token": r["access_token"], "refresh_token": r["refresh_token"],
                         "expires_at": time.time() + int(r.get("expires_in", 21600))})


def bl_exchange(code):
    bl_post_token({"grant_type": "authorization_code", "code": code})


def bl_token():
    t = kv_get("bling_tok")
    if not t:
        raise RuntimeError("Bling M3 não conectado")
    if t["expires_at"] - time.time() < 300:
        bl_post_token({"grant_type": "refresh_token", "refresh_token": t["refresh_token"]})
        t = kv_get("bling_tok")
    return t


def bl_get(path, params=None):
    t = bl_token()
    for tentativa in range(5):
        time.sleep(0.4)   # limite do Bling: 3 consultas por segundo
        r = requests.get("https://api.bling.com.br/Api/v3" + path, params=params, timeout=60,
                         headers={"Authorization": "Bearer " + t["access_token"]})
        if r.status_code == 429:
            time.sleep(2 + tentativa)
            continue
        return r.status_code, r.json()
    raise RuntimeError("Bling limitou as consultas (429)")


BL_ULT_ERRO = {"msg": ""}


def bl_post(path, body, method="POST"):
    t = bl_token()
    time.sleep(0.4)
    r = requests.request(method, "https://api.bling.com.br/Api/v3" + path, json=body, timeout=60,
                         headers={"Authorization": "Bearer " + t["access_token"]})
    try:
        j = r.json()
    except Exception:
        j = {"raw": r.text[:200]}
    return r.status_code, j


def bling_loja_id():
    """ID da loja Shopee cadastrada no Bling M3 (o 'carrinho' do produto)."""
    cfg = str(settings().get("bling_loja_id") or "").strip()
    if cfg:
        return int(cfg)
    code, r = bl_get("/lojas")
    if code != 200:
        raise RuntimeError(f"Bling /lojas respondeu {code}: {str(r)[:160]}")
    lojas = [l for l in (r.get("data") or []) if "shopee" in norm(json.dumps(l, ensure_ascii=False))]
    pref = [l for l in lojas if "duplo" in norm(json.dumps(l, ensure_ascii=False))]
    achadas = pref or lojas
    if len(achadas) != 1:
        raise RuntimeError("não identifiquei a loja Shopee no Bling; informe o ID em Regras de preço")
    return int(achadas[0]["id"])


def bling_vincular(sku, item_id, preco):
    """Coloca o ID do item da Shopee em 'ID na loja' do produto no Bling M3 (vínculo de estoque e preço)."""
    code, r = bl_get("/produtos", {"codigos[]": sku})
    if code != 200 or not r.get("data"):
        raise RuntimeError(f"produto {sku} não achado no Bling")
    pid = r["data"][0]["id"]
    loja = bling_loja_id()
    code, r = bl_get("/produtos/lojas", {"idProduto": pid, "idLoja": loja})
    atual = (r.get("data") or []) if code == 200 else []
    body = {"codigo": str(item_id), "preco": preco, "precoPromocional": preco,
            "produto": {"id": pid}, "loja": {"id": loja}}
    if atual:
        code, r = bl_post(f"/produtos/lojas/{atual[0]['id']}", body, "PUT")
    else:
        code, r = bl_post("/produtos/lojas", body)
    if code not in (200, 201, 204):
        raise RuntimeError(f"Bling vínculo respondeu {code}: {str(r)[:200]}")


def bling_fiscal(skus):
    """SKU normalizado -> lista de dados fiscais que faltam no produto do Bling M3 (NCM e origem)."""
    ids = {}
    for i in range(0, len(skus), 50):
        code, r = bl_get("/produtos", {"codigos[]": skus[i:i + 50], "criterio": 5, "limite": 100})
        if code != 200:
            raise RuntimeError(f"Bling /produtos respondeu {code}: {str(r)[:160]}")
        for it in r.get("data") or []:
            ids[norm(it.get("codigo"))] = it["id"]
    out = {}
    for k, pid in ids.items():
        code, r = bl_get(f"/produtos/{pid}")
        if code != 200:
            continue
        t = (r.get("data") or {}).get("tributacao") or {}
        falta = []
        if not str(t.get("ncm") or "").strip():
            falta.append("NCM")
        if t.get("origem") in (None, ""):
            falta.append("Origem")
        out[k] = falta
    return out


def bling_empresa():
    """Nome da conta Bling conectada (para conferir se é a M3)."""
    code, r = bl_get("/empresas/me/dados-basicos")
    if code != 200:
        return f"não foi possível ler a empresa (resposta {code})"
    d = r.get("data") or {}
    return d.get("nome") or d.get("razaoSocial") or d.get("fantasia") or str(d)[:120]


def bling_dados(sku):
    """Custo, peso e medidas do produto no Bling da M3.
    Custo: fornecedor.precoCusto do produto; se faltar, usa o precoCusto da listagem."""
    code, r = bl_get("/produtos", {"codigos[]": sku})
    if code != 200:
        BL_ULT_ERRO["msg"] = f"Bling /produtos respondeu {code}: {str(r)[:200]}"
        return None
    if not r.get("data"):
        BL_ULT_ERRO["msg"] = f"SKU {sku} não encontrado no Bling conectado"
        return None
    item = r["data"][0]
    custo = float(item.get("precoCusto") or 0)
    peso, dim = 0.0, [0, 0, 0]
    est = (item.get("estoque") or {}).get("saldoVirtualTotal")
    code, r2 = bl_get(f"/produtos/{item['id']}")
    if code == 200:
        d = r2.get("data") or {}
        custo = float((d.get("fornecedor") or {}).get("precoCusto") or d.get("precoCusto") or custo or 0)
        dm = d.get("dimensoes") or {}
        peso = float(d.get("pesoBruto") or 0)
        dim = [dm.get("profundidade") or 0, dm.get("largura") or 0, dm.get("altura") or 0]
        e2 = (d.get("estoque") or {}).get("saldoVirtualTotal")
        est = e2 if e2 is not None else est
    else:
        BL_ULT_ERRO["msg"] = f"Bling /produtos/{item['id']} respondeu {code}: {str(r2)[:200]}"
    if not custo:
        BL_ULT_ERRO["msg"] = f"SKU {sku} achado no Bling, mas sem preço de custo"
    return {"custo": custo, "peso": peso, "dim": dim, "estoque": est}


def bling_estoques(skus):
    """Saldo virtual do Bling M3 para vários SKUs de uma vez. Produto inativo conta como zero."""
    out = {}
    for i in range(0, len(skus), 50):
        code, r = bl_get("/produtos", {"codigos[]": skus[i:i + 50], "criterio": 5, "limite": 100})
        if code != 200:
            raise RuntimeError(f"Bling /produtos respondeu {code}: {str(r)[:200]}")
        for it in r.get("data") or []:
            e = (it.get("estoque") or {}).get("saldoVirtualTotal")
            if it.get("situacao") not in (None, "A"):
                e = 0
            if e is not None:
                out[norm(it.get("codigo"))] = float(e)
    return out


# ----------------------------------------------------------------------------
# Tarefas em segundo plano
# ----------------------------------------------------------------------------
def _new_job():
    return {"running": False, "kind": "", "msg": "", "done": 0, "total": 0, "log": []}


JOBS = {"analisar": _new_job(), "enviar": _new_job(), "sync": _new_job(), "full": _new_job()}
JOB_LOCK = threading.Lock()
_tl = threading.local()


def J():
    return JOBS[getattr(_tl, "kind", "analisar")]


def jlog(m):
    j = J()
    j["msg"] = m
    j["log"] = (j["log"] + [f"{datetime.now().strftime('%H:%M:%S')} {m}"])[-60:]


def start_job(kind, fn, *a):
    with JOB_LOCK:
        j = JOBS[kind]
        if j["running"]:
            return False
        j.update(running=True, kind=kind, msg="Iniciando...", done=0, total=0, log=[])

    def wrap():
        _tl.kind = kind
        try:
            fn(*a)
        except Exception as e:
            jlog(f"ERRO: {e}")
        finally:
            j["running"] = False
    threading.Thread(target=wrap, daemon=True).start()
    return True


def set_cand(sku, data, status, msg=""):
    run("DELETE FROM cria_cand WHERE sku=?", (sku,))
    run("INSERT INTO cria_cand (sku, data, status, msg, updated) VALUES (?,?,?,?,?)",
        (sku, json.dumps(data), status, msg, now()))


def estoque_inicial(d):
    """Estoque do anúncio: saldo do Bling M3; se o Bling não informar, usa o do ML."""
    e = d.get("estoque_bling")
    return int(max(0, e)) if e is not None else int(d.get("qtd") or 0)


def aplica_regras(d, s):
    """Define situação e preço do candidato."""
    if not d.get("custo"):
        return "sem_custo", "sem custo no Bling da M3"
    if not d.get("fotos"):
        return "sem_foto", "anúncio do ML sem fotos"
    if estoque_inicial(d) <= 0 and not s["criar_sem_estoque"]:
        return "sem_estoque", "sem estoque no Bling M3"
    p = calc_price(d["custo"], s)
    if not p:
        return "erro_preco", "não foi possível calcular o preço"
    d["preco"] = p
    d["calc"] = breakdown(p, d["custo"], s)
    pm = d.get("preco_ml") or 0
    d["dif_ml"] = round((p / pm - 1) * 100, 1) if pm else None
    return "pendente", ""


def job_analisar():
    s = settings()
    jlog("Lendo SKUs da Shopee...")
    shp, n = shopee_skus()
    jlog(f"Shopee: {n} anúncios, {len(shp)} SKUs")
    ml = ml_items(s["prefixos"], jlog)
    faltam = {k: v for k, v in ml.items() if k not in shp}
    jlog(f"{len(faltam)} SKUs estão no ML e não estão na Shopee")
    antigos = {r[0]: r[1] for r in run("SELECT sku, status FROM cria_cand", (), True)}
    if JOBS["enviar"]["running"]:
        jlog("Há um envio em andamento: mantive a lista atual até ele terminar")
    else:
        run("DELETE FROM cria_cand WHERE status<>?", ("criado",))
        jlog("Lista anterior zerada (os itens já criados foram mantidos)")
    J()["total"] = len(faltam)
    try:
        jlog(f"Bling conectado: {bling_empresa()}")
    except Exception as e:
        jlog(f"Bling: não consegui identificar a conta ({e})")
    falhas = 0
    for sku_n, d in faltam.items():
        if antigos.get(d["sku"]) == "criado":
            continue
        BL_ULT_ERRO["msg"] = ""
        try:
            b = bling_dados(d["sku"])
        except Exception as e:
            BL_ULT_ERRO["msg"] = str(e)
            b = None
        if (not b or not b.get("custo")) and falhas < 5:
            jlog(f"Bling: {BL_ULT_ERRO['msg'] or 'sem custo'}")
            falhas += 1
        if b:
            d.update(custo=b["custo"], peso=b["peso"], dim=b["dim"], estoque_bling=b.get("estoque"))
        atual = run("SELECT status FROM cria_cand WHERE sku=?", (d["sku"],), True)
        if atual and atual[0][0] == "criado":
            continue
        st, msg = aplica_regras(d, s)
        set_cand(d["sku"], d, st, msg)
        J()["done"] += 1
        if J()["done"] % 10 == 0:
            jlog(f"Preparados {J()['done']} de {J()['total']}")
    # remove quem já existe na Shopee agora ou não está mais ativo no ML
    atuais = {d["sku"] for d in faltam.values()}
    for sku, st_antigo in antigos.items():
        if st_antigo != "criado" and sku not in atuais:
            run("DELETE FROM cria_cand WHERE sku=?", (sku,))
    jlog(f"Análise concluída: {J()['done']} itens preparados")


def dry_run_on():
    return DRY_RUN_ENV


def enviar_um(sku, s):
    row = run("SELECT data FROM cria_cand WHERE sku=?", (sku,), True)
    if not row:
        return "erro", "SKU não está na lista"
    d = json.loads(row[0][0])
    st, msg = aplica_regras(d, s)
    if st != "pendente":
        set_cand(sku, d, st, msg)
        return st, msg
    cat = sp_category(d["titulo"], s)
    attrs, problemas = resolve_attrs(cat, s, d["sku"])
    if problemas:
        msg = "; ".join(problemas)
        set_cand(sku, d, "bloqueado", msg)
        return "bloqueado", msg
    code, desc = ml_get(f"/items/{d['ml_id']}/description")
    texto = (desc.get("plain_text") or "").strip() if code == 200 else ""
    extra = "\n\nPeça para moto. Confira a compatibilidade com o seu modelo e ano no título do anúncio."
    texto = (texto or d["titulo"]) + (extra if len(texto) < 120 else "")
    texto = texto[:3000]
    peso = d.get("peso") or s["peso_padrao"]
    dm = d.get("dim") or []
    if len(dm) != 3 or not all(dm):
        dm = s["dim_padrao"]
    if dry_run_on():
        ids = [f"(simulação) foto {i + 1}" for i in range(min(9, len(d["fotos"])))]
    else:
        ids, falhas_foto = [], []
        for i, url in enumerate(d["fotos"][:9]):
            try:
                img = foto_jpeg(requests.get(url, timeout=60).content)
                ids.append(sp_upload_image(img, f"{sku}-{i}.jpg"))
            except Exception as e:
                falhas_foto.append(str(e))
        if not ids:
            raise RuntimeError("nenhuma foto aceita pela Shopee: " + (falhas_foto[0] if falhas_foto else "sem fotos"))
    body = {
        "original_price": d["preco"], "description": texto, "item_name": d["titulo"][:120],
        "item_status": "NORMAL", "item_sku": d["sku"], "condition": "NEW", "category_id": cat,
        "seller_stock": [{"stock": estoque_inicial(d)}], "weight": round(float(peso), 3),
        "dimension": {"package_length": int(dm[0]), "package_width": int(dm[1]), "package_height": int(dm[2])},
        "logistic_info": sp_logistics(), "image": {"image_id_list": ids},
        "brand": {"brand_id": 0, "original_brand_name": "NoBrand"}, "attribute_list": attrs,
    }
    if dry_run_on():
        d["previa"] = {"categoria": cat, "atributos": attrs, "fotos": len(ids), "preco": d["preco"]}
        set_cand(sku, d, "simulado", f"simulação ok (categoria {cat}, {len(attrs)} atributos obrigatórios preenchidos)")
        return "simulado", "ok"
    r = sp("/api/v2/product/add_item", "POST", body=body)
    item_id = (r.get("response") or {}).get("item_id")
    d["shopee_item_id"] = item_id
    d["estoque_shopee"] = estoque_inicial(d)
    try:
        bling_vincular(d["sku"], item_id, d["preco"])
        d["bling_vinculado"] = True
        extra = "; vinculado no Bling"
    except Exception as e:
        d["bling_vinculado"] = False
        extra = f"; vínculo no Bling falhou: {e}"
    set_cand(sku, d, "criado", f"item {item_id}{extra}")
    return "criado", str(item_id)


def job_sync_estoque():
    """Copia o saldo do Bling M3 para a Shopee nos anúncios criados por este app (zero = pausado)."""
    itens = []
    for sku, data in run("SELECT sku, data FROM cria_cand WHERE status=?", ("criado",), True):
        d = json.loads(data)
        if d.get("shopee_item_id"):
            itens.append((sku, d))
    J()["total"] = len(itens)
    if not itens:
        jlog("Nenhum anúncio criado por este app para sincronizar")
        return
    for sku, d in itens:
        if d.get("bling_vinculado"):
            continue
        if dry_run_on():
            jlog(f"{sku}: (simulação) seria vinculado no Bling com o item {d['shopee_item_id']}")
            continue
        try:
            bling_vincular(sku, d["shopee_item_id"], d.get("preco") or 0)
            d["bling_vinculado"] = True
            set_cand(sku, d, "criado", f"item {d['shopee_item_id']}; vinculado no Bling")
            jlog(f"{sku}: vinculado no Bling")
        except Exception as e:
            jlog(f"{sku}: vínculo no Bling falhou: {e}"[:220])
    saldos = bling_estoques([sku for sku, _ in itens])
    mudou = erro = 0
    for sku, d in itens:
        J()["done"] += 1
        k = norm(sku)
        if k not in saldos:
            jlog(f"{sku}: não achei no Bling M3, mantive o estoque")
            continue
        novo = max(0, int(saldos[k]))
        if d.get("estoque_shopee") == novo:
            continue
        if dry_run_on():
            jlog(f"{sku}: (simulação) estoque da Shopee iria para {novo}")
            continue
        try:
            sp("/api/v2/product/update_stock", "POST", body={
                "item_id": int(d["shopee_item_id"]),
                "stock_list": [{"model_id": 0, "seller_stock": [{"stock": novo}]}]})
            d["estoque_shopee"] = novo
            set_cand(sku, d, "criado", f"item {d['shopee_item_id']}; estoque {novo}")
            mudou += 1
            jlog(f"{sku}: estoque da Shopee agora é {novo}")
        except Exception as e:
            erro += 1
            jlog(f"{sku}: erro {e}"[:200])
        time.sleep(0.3)
    jlog(f"Sincronização concluída: {mudou} atualizados, {erro} com erro, {len(itens)} conferidos")


def ml_vendas_30d():
    """Unidades vendidas por anúncio nos últimos 30 dias (cancelados fora)."""
    t = ml_token()
    desde = (datetime.utcnow() - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S.000-00:00")
    vend, off = {}, 0
    while True:
        code, r = ml_get("/orders/search", {"seller": t["user_id"], "order.date_created.from": desde,
                                            "sort": "date_desc", "limit": 50, "offset": off})
        if code != 200:
            raise RuntimeError(f"ML vendas: {r.get('message') or code}")
        res = r.get("results") or []
        for o in res:
            if o.get("status") == "cancelled":
                continue
            for it in o.get("order_items") or []:
                iid = (it.get("item") or {}).get("id")
                if iid:
                    vend[iid] = vend.get(iid, 0) + int(it.get("quantity") or 0)
        off += 50
        total = (r.get("paging") or {}).get("total") or 0
        jlog(f"Vendas lidas: {min(off, total)} de {total}")
        if not res or off >= total:
            break
    return vend


ML_FISC = {"erro": ""}


def _tem_ncm(o):
    if isinstance(o, dict):
        if str(o.get("ncm") or "").strip():
            return True
        return any(_tem_ncm(v) for v in o.values())
    if isinstance(o, list):
        return any(_tem_ncm(v) for v in o)
    return False


def ml_fiscal_ok(mlb, sku):
    """True se o ML já tem NCM cadastrado (por anúncio ou por SKU); False se não tem; None se a consulta falhou."""
    erro = None
    for path in (f"/items/{mlb}/fiscal_information/detail", f"/items/fiscal_information/{sku}"):
        code, r = ml_get(path)
        if code == 200 and _tem_ncm(r):
            return True
        if code not in (200, 404):
            erro = f"{code} {str(r)[:120]}"
    if erro:
        ML_FISC["erro"] = erro
        return None
    return False


def job_full():
    """Vendas 30 dias x saldo no Full x meta de cobertura x saldo virtual do Bling M3, por SKU."""
    s = settings()
    dias = float(s.get("dias_cobertura") or 45)
    vend = ml_vendas_30d()
    manuais = ["MLB" + x for x in (kv_get("full_manual", []) or [])]
    ids = list(dict.fromkeys(list(vend) + manuais))
    jlog(f"{len(vend)} anúncios com venda e {len(manuais)} extras")
    itens = {}
    for i in range(0, len(ids), 20):
        code, r = ml_get("/items", {"ids": ",".join(ids[i:i + 20]),
                                   "attributes": "id,title,seller_custom_field,attributes,inventory_id,variations"})
        if code == 200:
            for e in r:
                b = e.get("body") or {}
                if b.get("id"):
                    itens[b["id"]] = b
    grupos = {}
    for iid in ids:
        b = itens.get(iid)
        if not b:
            jlog(f"{iid}: não encontrado no ML")
            continue
        sku = b.get("seller_custom_field") or next(
            (a.get("value_name") for a in b.get("attributes", []) if a.get("id") == "SELLER_SKU"), None) or iid
        g = grupos.setdefault(norm(sku), {"sku": str(sku).strip(), "titulo": b.get("title", ""), "mlbs": [],
                                          "vendas": 0, "invs": set(), "manual": False})
        g["mlbs"].append(iid)
        g["vendas"] += vend.get(iid, 0)
        for inv in [b.get("inventory_id")] + [v.get("inventory_id") for v in b.get("variations") or []]:
            if inv:
                g["invs"].add(inv)
        g["manual"] = g["manual"] or iid in manuais
    J()["total"] = len(grupos)
    cache, falhas = {}, 0
    for g in grupos.values():
        g["full"] = 0
        for inv in g["invs"]:
            if inv not in cache:
                code, r = ml_get(f"/inventories/{inv}/stock/fulfillment")
                if code != 200 and falhas < 3:
                    jlog(f"Full {inv}: resposta {code} {str(r)[:120]}")
                    falhas += 1
                cache[inv] = int(r.get("total") or 0) if code == 200 else 0
            g["full"] += cache[inv]
        J()["done"] += 1
    comps = sorted({c for g in grupos.values() for c in g["sku"].split("-") if c and not c.upper().startswith("MLB")})
    jlog(f"Conferindo {len(comps)} códigos no Bling M3...")
    saldos = bling_estoques(comps)
    linhas = []
    for g in grupos.values():
        alvo = math.ceil(g["vendas"] / 30.0 * dias)
        sug = max(0, alvo - g["full"])
        cs = [c for c in g["sku"].split("-") if c and not c.upper().startswith("MLB")]
        vals = [(c, saldos.get(norm(c))) for c in cs]
        bl = int(min(v for _, v in vals)) if cs and all(v is not None for _, v in vals) else None
        if sug == 0:
            st = "coberto"
        elif bl is None:
            st = "sem_bling"
        elif bl >= sug:
            st = "enviar"
        elif bl > 0:
            st = "parcial"
        else:
            st = "sem_estoque"
        linhas.append({"sku": g["sku"], "titulo": g["titulo"], "mlbs": " | ".join(m.replace("MLB", "") for m in g["mlbs"]),
                       "mlb": max(g["mlbs"], key=lambda m: vend.get(m, 0)).replace("MLB", ""),
                       "vendas": g["vendas"], "full": g["full"], "alvo": alvo, "sug": sug, "bl": bl,
                       "det": " · ".join(f"{c}: {int(v) if v is not None else '?'}" for c, v in vals),
                       "status": st, "manual": g["manual"],
                       "cs": {c: (int(v) if v is not None else None) for c, v in vals}})
    jlog("Conferindo dados fiscais no Mercado Livre...")
    ML_FISC["erro"] = ""
    n_ok = n_falta = 0
    for l in linhas:
        if l["sug"] <= 0:
            continue
        ok = ml_fiscal_ok("MLB" + l["mlb"], l["sku"])
        l["fiscal"] = [] if ok is True else (["Cadastrar no ML"] if ok is False else ["Consulta falhou"])
        n_ok += ok is True
        n_falta += ok is False
    jlog(f"Dados fiscais no ML: {n_ok} com NCM, {n_falta} sem cadastro")
    if ML_FISC["erro"]:
        jlog(f"Consulta fiscal no ML: {ML_FISC['erro']}")
    if n_ok == 0 and n_falta > 3:
        jlog("Aviso: nenhum item voltou com NCM. Se isso não bate com a realidade, a consulta pode não estar disponível para a sua conta")
    linhas.sort(key=lambda x: (-x["sug"], x["sku"]))
    kv_set("full_snap", {"quando": now(), "linhas": linhas})
    jlog(f"Análise do Full concluída: {len(linhas)} SKUs")


def job_enviar(skus):
    s = settings()
    J()["total"] = len(skus)
    res = {}
    for sku in skus:
        try:
            st, msg = enviar_um(sku, s)
        except Exception as e:
            st, msg = "erro", str(e)
            row = run("SELECT data FROM cria_cand WHERE sku=?", (sku,), True)
            if row:
                set_cand(sku, json.loads(row[0][0]), "erro", msg)
        res[st] = res.get(st, 0) + 1
        J()["done"] += 1
        jlog(f"{sku}: {st} {msg}"[:200])
        time.sleep(0.5)
    jlog("Lote concluído: " + ", ".join(f"{v} {k}" for k, v in res.items()))


# ----------------------------------------------------------------------------
# Rotas
# ----------------------------------------------------------------------------
@app.before_request
def protege():
    if request.path == "/health":
        return None
    if request.path == "/sync-estoque" and SYNC_TOKEN and request.args.get("token") == SYNC_TOKEN:
        return None
    if not ADMIN_PASS and not LOCAL_DEV:
        return Response("Defina ADMIN_PASS nas variáveis do Render.", 503)
    if LOCAL_DEV and not ADMIN_PASS:
        return None
    a = request.authorization
    if not a or a.username != ADMIN_USER or a.password != ADMIN_PASS:
        return Response("Login necessário", 401, {"WWW-Authenticate": 'Basic realm="Criador Shopee"'})


@app.route("/health")
def health():
    return "ok"


@app.route("/connect/<svc>")
def connect(svc):
    try:
        url = {"shopee": sp_auth_url, "ml": ml_auth_url, "bling": bl_auth_url}[svc]()
    except KeyError:
        return "serviço inválido", 404
    return redirect(url)


# O Bling devolve para /bling/callback (endereço cadastrado no app da M3);
# o ML e a Shopee usam /callback/<svc>. As duas rotas caem na mesma função.
@app.route("/callback/<svc>")
@app.route("/bling/callback", defaults={"svc": "bling"})
def callback(svc):
    try:
        code = request.args.get("code")
        if not code:
            return "Autorização sem código: " + json.dumps(dict(request.args)), 400
        if svc == "shopee":
            sp_exchange(code, request.args.get("shop_id"))
        elif svc == "ml":
            ml_exchange(code)
        elif svc == "bling":
            bl_exchange(code)
        else:
            return "serviço inválido", 404
    except Exception as e:
        return f"Erro ao conectar: {e}", 500
    return redirect("/")


@app.route("/api/state")
def api_state():
    rows = run("SELECT sku, data, status, msg FROM cria_cand ORDER BY status, sku", (), True)
    cands = []
    for sku, data, st, msg in rows:
        d = json.loads(data)
        cands.append({"sku": sku, "titulo": d.get("titulo"), "custo": d.get("custo"), "preco": d.get("preco"),
                      "lucro": (d.get("calc") or {}).get("lucro"), "qtd": estoque_inicial(d),
                      "fotos": len(d.get("fotos") or []), "status": st, "msg": msg, "link": d.get("link"),
                      "preco_ml": d.get("preco_ml"), "dif": d.get("dif_ml")})
    return jsonify({
        "conn": {"shopee": bool(kv_get("shopee_tok")), "ml": bool(kv_get("ml_tok")), "bling": bool(kv_get("bling_tok"))},
        "cfg_ok": {"shopee": bool(SHOPEE_PID and SHOPEE_KEY), "ml": bool(ML_ID and ML_SECRET), "bling": bool(BLING_ID and BLING_SECRET)},
        "settings": settings(), "dry": dry_run_on(), "job": JOBS["analisar"], "job_send": JOBS["enviar"], "job_sync": JOBS["sync"], "job_full": JOBS["full"], "full_quando": (kv_get("full_snap", {}) or {}).get("quando"), "cands": cands, "max_lote": MAX_LOTE})


@app.route("/api/settings", methods=["POST"])
def api_settings():
    j = request.get_json(force=True)
    s = settings()
    try:
        for k in ("lucro", "promo", "imposto", "piso", "peso_padrao", "alerta_acima_ml", "dias_cobertura"):
            s[k] = float(str(j.get(k, s[k])).replace(",", "."))
        s["prefixos"] = str(j.get("prefixos", s["prefixos"]))
        s["bling_loja_id"] = str(j.get("bling_loja_id", s["bling_loja_id"])).strip()
        s["categoria_id"] = str(j.get("categoria_id", s["categoria_id"])).strip()
        s["criar_sem_estoque"] = bool(j.get("criar_sem_estoque", s["criar_sem_estoque"]))
        dim = j.get("dim_padrao", s["dim_padrao"])
        s["dim_padrao"] = [int(x) for x in dim][:3]
        at = j.get("atributos", s["atributos"])
        s["atributos"] = json.loads(at) if isinstance(at, str) else at
    except Exception as e:
        return jsonify({"erro": f"Configuração inválida: {e}"}), 400
    kv_set("settings", s)
    # recalcula preços dos pendentes com as novas regras
    for sku, data, st, _ in run("SELECT sku, data, status, msg FROM cria_cand", (), True):
        if st in ("pendente", "sem_custo", "sem_foto", "sem_estoque", "erro_preco", "simulado", "bloqueado", "erro"):
            d = json.loads(data)
            ns, msg = aplica_regras(d, s)
            set_cand(sku, d, ns, msg)
    return jsonify({"ok": True})


@app.route("/api/bling-teste")
def api_bling_teste():
    """Diagnóstico: /api/bling-teste?sku=SM101001 mostra a conta conectada e o que o Bling devolve."""
    sku = request.args.get("sku", "").strip()
    out = {}
    try:
        out["conta"] = bling_empresa()
        if sku:
            BL_ULT_ERRO["msg"] = ""
            out["dados"] = bling_dados(sku)
            out["aviso"] = BL_ULT_ERRO["msg"]
    except Exception as e:
        out["erro"] = str(e)
    return jsonify(out)


@app.route("/api/categorias")
def api_categorias():
    """Busca categorias finais da Shopee pelo texto: /api/categorias?q=moto injecao"""
    try:
        q = [w for w in norm(request.args.get("q", "")).split() if w]
        cats = sp_categories()
        by = {c["category_id"]: c for c in cats}

        def nome(c):
            return c.get("display_category_name") or c.get("original_category_name") or str(c["category_id"])

        def caminho(c):
            p = []
            while c:
                p.append(nome(c))
                c = by.get(c.get("parent_category_id"))
            return " > ".join(reversed(p))
        out = []
        for c in cats:
            if c.get("has_children"):
                continue
            cam = caminho(c)
            if all(w in norm(cam) for w in q):
                out.append({"id": c["category_id"], "caminho": cam})
        return jsonify(out[:60])
    except Exception as e:
        return jsonify({"erro": str(e)})


@app.route("/sync-estoque")
def rota_sync():
    """Para o cron-job.org: /sync-estoque?token=SYNC_TOKEN (roda em segundo plano)."""
    ok = start_job("sync", job_sync_estoque)
    return jsonify({"ok": ok, "msg": "iniciado" if ok else "já em andamento"})


@app.route("/api/sync", methods=["POST"])
def api_sync():
    if not start_job("sync", job_sync_estoque):
        return jsonify({"erro": "A sincronização já está em andamento"}), 409
    return jsonify({"ok": True})


@app.route("/api/full")
def api_full():
    snap = kv_get("full_snap", {}) or {}
    return jsonify({"quando": snap.get("quando"), "linhas": snap.get("linhas", []),
                    "manual": kv_get("full_manual", []) or [], "pedidos": kv_get("full_pedidos", []) or [], "dias": settings().get("dias_cobertura")})


@app.route("/api/full/run", methods=["POST"])
def api_full_run():
    if not start_job("full", job_full):
        return jsonify({"erro": "A análise do Full já está em andamento"}), 409
    return jsonify({"ok": True})


@app.route("/api/full/pedido", methods=["POST"])
def api_full_pedido():
    j = request.get_json(force=True) or {}
    itens = {}
    for k, v in (j.get("itens") or {}).items():
        try:
            if int(v) > 0:
                itens[str(k)] = int(v)
        except Exception:
            pass
    if not itens:
        return jsonify({"erro": "Nenhum item com quantidade para enviar"}), 400
    ped = kv_get("full_pedidos", []) or []
    nome = str(j.get("nome") or "").strip() or f"Pedido {len(ped) + 1:02d}"
    ped.append({"nome": nome, "data": str(j.get("data") or ""), "criado": now(), "itens": itens, "total": sum(itens.values())})
    kv_set("full_pedidos", ped)
    return jsonify({"ok": True})


@app.route("/api/full/pedido/excluir", methods=["POST"])
def api_full_pedido_excluir():
    nome = str((request.get_json(force=True) or {}).get("nome", ""))
    kv_set("full_pedidos", [p for p in (kv_get("full_pedidos", []) or []) if p.get("nome") != nome])
    return jsonify({"ok": True})


@app.route("/api/full/manual", methods=["POST"])
def api_full_manual():
    txt = str((request.get_json(force=True) or {}).get("texto", ""))
    kv_set("full_manual", list(dict.fromkeys(re.findall(r"\d{6,}", txt))))
    return jsonify({"ok": True})


@app.route("/api/bling-vinculo")
def api_bling_vinculo():
    """Diagnóstico: /api/bling-vinculo?sku=SM105001 mostra as lojas do Bling e o vínculo que já funciona."""
    out = {}
    try:
        code, r = bl_get("/lojas")
        out["lojas"] = r if code == 200 else {"erro": code, "resp": str(r)[:200]}
        sku = request.args.get("sku", "").strip()
        if sku:
            code, r = bl_get("/produtos", {"codigos[]": sku})
            if code == 200 and r.get("data"):
                code, r2 = bl_get("/produtos/lojas", {"idProduto": r["data"][0]["id"]})
                out["vinculos"] = r2
    except Exception as e:
        out["erro"] = str(e)
    return jsonify(out)


@app.route("/api/analyze", methods=["POST"])
def api_analyze():
    if not start_job("analisar", job_analisar):
        return jsonify({"erro": "Já existe uma tarefa em andamento"}), 409
    return jsonify({"ok": True})


@app.route("/api/send", methods=["POST"])
def api_send():
    skus = (request.get_json(force=True) or {}).get("skus", [])[:MAX_LOTE]
    if not skus:
        return jsonify({"erro": "Nenhum item selecionado"}), 400
    if not start_job("enviar", job_enviar, skus):
        return jsonify({"erro": "Já existe uma tarefa em andamento"}), 409
    return jsonify({"ok": True, "enviando": len(skus)})


PAGE = r"""<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Criador de anúncios Shopee</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Sora:wght@500;600;700&display=swap" rel="stylesheet">
<style>
:root{--bg:#f2f4fa;--panel:#fff;--soft:#eceff8;--ink:#131a2c;--mut:#667290;--line:#dde2ef;--acc:#4d59f5;--acc2:#9a5cff;--on:#fff;--ok:#0b9a68;--warn:#b0731a;--bad:#d63d4b}
@media(prefers-color-scheme:dark){:root{--bg:#0b0f1a;--panel:#131928;--soft:#1a2236;--ink:#e8ecf6;--mut:#8e9ab6;--line:#252e48;--acc:#7c87ff;--acc2:#b08cff;--on:#0b0f1a;--ok:#3fd49b;--warn:#f0b955;--bad:#ff7a85}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
h1,h2,.num{font-family:Sora,system-ui,sans-serif;letter-spacing:-.01em}
header{position:sticky;top:0;z-index:5;background:color-mix(in srgb,var(--bg) 88%,transparent);backdrop-filter:blur(10px);border-bottom:1px solid var(--line)}
.top{max-width:1180px;margin:0 auto;padding:12px 16px;display:flex;gap:18px;align-items:center;flex-wrap:wrap}
.brand{font:700 18px Sora,system-ui;display:flex;align-items:center;gap:10px}
.logo{width:30px;height:30px;border-radius:9px;background:linear-gradient(135deg,var(--acc),var(--acc2))}
nav{display:flex;gap:4px;background:var(--soft);padding:4px;border-radius:12px}
nav button{border:0;background:none;color:var(--mut);font:600 14px inherit;padding:7px 14px;border-radius:9px;cursor:pointer}
nav button.on{background:var(--panel);color:var(--ink);box-shadow:0 1px 3px rgba(20,30,60,.15)}
.sp{flex:1}
.dots{display:flex;gap:10px;font-size:13px;color:var(--mut)}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;background:var(--bad);margin-right:5px}.dot.ok{background:var(--ok)}
.mode{font-size:12px;font-weight:600;padding:4px 10px;border-radius:99px}
.mode.sim{background:color-mix(in srgb,var(--warn) 18%,transparent);color:var(--warn)}.mode.live{background:color-mix(in srgb,var(--bad) 18%,transparent);color:var(--bad)}
main{max-width:1180px;margin:0 auto;padding:20px 16px 110px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:18px;margin-bottom:16px}
h2{font-size:16px;margin:0 0 12px}
button,.btn{font:inherit;font-weight:600;border:1px solid var(--line);background:var(--panel);color:var(--ink);border-radius:10px;padding:9px 14px;cursor:pointer;text-decoration:none;display:inline-block}
button.pri{background:linear-gradient(135deg,var(--acc),var(--acc2));color:#fff;border:0}
button:disabled{opacity:.45;cursor:not-allowed}
:focus-visible{outline:2px solid var(--acc);outline-offset:2px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin-bottom:16px}
.tile{text-align:left;border-radius:14px;padding:14px 16px;border:1px solid var(--line);background:var(--panel)}
.tile.on{border-color:var(--acc);box-shadow:0 0 0 3px color-mix(in srgb,var(--acc) 22%,transparent)}
.tile .num{display:block;font-size:30px;font-weight:700;line-height:1.1}.tile span:last-child{color:var(--mut);font-size:13px;font-weight:500}
.tile.ok .num{color:var(--ok)}.tile.bad .num{color:var(--bad)}.tile.warn .num{color:var(--warn)}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
.bar{height:6px;background:var(--soft);border-radius:4px;overflow:hidden;margin:12px 0 6px}.bar i{display:block;height:100%;width:0;background:linear-gradient(90deg,var(--acc),var(--acc2));transition:width .4s}
.mut{color:var(--mut);font-size:13px}
#alog,#slog{font:12px/1.6 ui-monospace,Menlo,Consolas,monospace;color:var(--mut);max-height:140px;overflow:auto;white-space:pre-wrap;margin-top:8px}
.tag{font-size:12px;font-weight:600;padding:2px 9px;border-radius:99px;background:var(--soft);color:var(--mut)}
.tag.ok{background:color-mix(in srgb,var(--ok) 16%,transparent);color:var(--ok)}.tag.bad{background:color-mix(in srgb,var(--bad) 16%,transparent);color:var(--bad)}.tag.warn{background:color-mix(in srgb,var(--warn) 18%,transparent);color:var(--warn)}
.tw{overflow-x:auto}table{border-collapse:collapse;width:100%;min-width:900px}
th,td{padding:9px 8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{font-size:12px;color:var(--mut);font-weight:600}td.n,th.n{text-align:right;font-variant-numeric:tabular-nums}
td a{color:var(--ink)}
.conn{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:14px 0;border-bottom:1px solid var(--line)}.conn:last-child{border:0}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px}
label{display:block;font-size:13px;color:var(--mut);margin-bottom:4px}
input[type=text],input[type=number],textarea,select{width:100%;font:inherit;padding:9px 11px;border:1px solid var(--line);border-radius:10px;background:var(--bg);color:var(--ink)}
.bottom{position:fixed;left:0;right:0;bottom:0;background:color-mix(in srgb,var(--panel) 92%,transparent);backdrop-filter:blur(10px);border-top:1px solid var(--line);padding:12px 16px;z-index:5}
.bottom .in{max-width:1180px;margin:0 auto;display:flex;gap:12px;align-items:center;flex-wrap:wrap}
[hidden]{display:none!important}
</style></head><body>
<header><div class="top">
<div class="brand"><span class="logo"></span>Criador de anúncios</div>
<nav><button id="t-anuncios" onclick="go('anuncios')">Anúncios</button><button id="t-conexoes" onclick="go('conexoes')">Conexões</button><button id="t-full" onclick="go('full')">Reposição Full</button><button id="t-regras" onclick="go('regras')">Regras de preço</button></nav>
<span class="sp"></span><div class="dots" id="dots"></div><span id="modo"></span>
</div></header>
<main>
<section id="p-anuncios">
<div class="tiles" id="tiles"></div>
<div class="card"><div class="row"><button class="pri" id="bAn" onclick="analisar()">Analisar Mercado Livre e Shopee</button><button id="bSync" onclick="sync()">Sincronizar estoque com o Bling M3</button><span id="amsg" class="mut"></span><span id="ymsg" class="mut"></span></div>
<div class="bar"><i id="aprog"></i></div>
<details><summary class="mut" style="cursor:pointer">Ver detalhes da análise</summary><div id="alog"></div></details></div>
<details class="card"><summary class="mut" style="cursor:pointer">Ver detalhes do envio</summary><div id="slog"></div></details>
<div class="card"><div class="row" style="margin-bottom:12px"><h2 style="margin:0;margin-right:auto">Anúncios para criar</h2>
<select id="filtro" onchange="setF(this.value)" style="width:auto"><option value="pendente">Prontos</option><option value="acima">Acima do ML</option><option value="problemas">Com pendência</option><option value="simulado">Simulados</option><option value="criado">Criados</option><option value="">Todos</option></select>
<button onclick="sel50()">Marcar 50</button><button onclick="selNone()">Limpar</button><span class="tag" id="cont"></span></div>
<div class="tw"><table><thead><tr><th></th><th>SKU</th><th>Título</th><th class="n">Custo</th><th class="n">Preço</th><th class="n">Preço ML</th><th class="n">vs ML</th><th class="n">Lucro</th><th class="n">Est.</th><th class="n">Fotos</th><th>Situação</th></tr></thead><tbody id="tb"></tbody></table></div></div>
</section>

<section id="p-full" hidden>
<div class="tiles" id="ftiles"></div>
<div class="card"><div class="row"><button class="pri" id="bFull" onclick="runFull()">Atualizar análise do Full</button><span id="fmsg" class="mut"></span><span id="fquando" class="mut" style="margin-left:auto"></span></div>
<div class="bar"><i id="fprog"></i></div>
<label style="margin-top:10px">MLBs extras para incluir (um por linha ou separados por vírgula)</label>
<textarea id="fmanual" rows="2" placeholder="6838348720, MLB4708874253"></textarea>
<div class="row" style="margin-top:8px"><button onclick="saveManual()">Salvar lista e atualizar</button><span class="mut">Meta de cobertura: <b id="fdias"></b> dias (muda em Regras de preço).</span></div>
<div class="row" style="margin-top:10px"><label style="margin:0">Enviar só o que vendeu pelo menos</label><input type="number" id="fmin" min="0" value="0" style="width:90px" onchange="setMin()"><span class="mut">unidades em 30 dias. MLBs extras ficam fora desse filtro.</span></div></div>
<div class="card"><div class="row" style="margin-bottom:12px"><h2 style="margin:0;margin-right:auto">Reposição por SKU</h2>
<select id="ffiltro" onchange="drawFull()" style="width:auto"><option value="">Todos</option><option value="enviar">A enviar</option><option value="fiscal">Sem dados fiscais</option><option value="sem">Sem estoque no Bling M3</option><option value="coberto">Cobertos</option></select>
<button onclick="baixarCsv()">Baixar envio</button><button onclick="baixarCompra()">Baixar compra</button><button onclick="baixarFiscal()">Pendências fiscais</button><span class="tag" id="fcont"></span></div>
<div class="tw"><table><thead><tr><th>SKU</th><th>Título</th><th>MLBs</th><th class="n">Vendas 30d</th><th class="n">Último pedido</th><th class="n">Saldo Full</th><th class="n">Meta</th><th class="n">Sugerido</th><th class="n">Bling M3</th><th class="n">Enviar</th><th>Situação</th><th>Dados fiscais</th></tr></thead><tbody id="ftb"></tbody></table></div></div>
<div class="card"><h2>Finalizar pedido</h2><div class="row"><input type="text" id="fped_nome" placeholder="Nome (ex.: Pedido 01)" style="width:210px"><input type="date" id="fped_data" style="width:170px"><button class="pri" onclick="finalizarPedido()">Finalizar e salvar no histórico</button></div>
<p class="mut">Salva as quantidades da coluna Enviar. Depois elas aparecem em Último pedido.</p><div id="fhist"></div></div>
</section>

<section id="p-conexoes" hidden><div class="card"><h2>Conexões</h2><div id="conns"></div>
<p class="mut">O Bling usado aqui é o da M3. Para trocar de conta, entre nela em janela anônima e clique em Reconectar.</p></div></section>

<section id="p-regras" hidden><div class="card"><h2>Regras de preço e envio</h2>
<div class="grid">
<div><label>Lucro (% do valor anunciado)</label><input type="number" step="0.1" id="lucro"></div>
<div><label>Promoção semanal (%)</label><input type="number" step="0.1" id="promo"></div>
<div><label>Imposto, pior cenário (%)</label><input type="number" step="0.1" id="imposto"></div>
<div><label>Preço mínimo anunciado (R$)</label><input type="number" step="0.01" id="piso"></div>
<div><label>Alerta: % acima do preço do ML</label><input type="number" step="1" id="alerta_acima_ml"></div>
<div><label>Cobertura do Full (dias)</label><input type="number" step="1" id="dias_cobertura"></div>
<div><label>ID da loja Shopee no Bling (vazio = automático)</label><input type="text" id="bling_loja_id"></div>
<div><label>Prefixos de SKU (vírgula)</label><input type="text" id="prefixos"></div>
<div><label>ID da categoria Shopee (vazio = automática)</label><input type="text" id="categoria_id"></div>
<div><label>Peso padrão (kg)</label><input type="number" step="0.01" id="peso_padrao"></div>
<div><label>Medidas padrão C x L x A (cm)</label><input type="text" id="dim_padrao"></div>
</div>
<div style="margin-top:12px"><label>Buscar categoria na Shopee (ex.: moto injeção)</label><div class="row"><input type="text" id="qcat" style="flex:1" placeholder="moto injeção"><button onclick="buscaCat()">Buscar</button></div><div id="rcat" class="mut" style="margin-top:8px"></div></div>
<div style="margin-top:12px"><label>Atributos obrigatórios da Shopee (JSON: nome e valor; use {sku} para o código do produto)</label><textarea id="atributos" rows="3"></textarea></div>
<div class="row" style="margin-top:12px"><label style="display:flex;gap:8px;align-items:center;margin:0"><input type="checkbox" id="criar_sem_estoque"> Criar também itens sem estoque no ML</label><button class="pri" onclick="salvar()">Salvar e recalcular preços</button></div>
</div></section>
</main>
<div class="bottom" id="bar"><div class="in"><button class="pri" id="bEnv" onclick="enviar()" disabled>Enviar selecionados</button><span id="selinfo" class="tag">0 selecionados</span><span id="smsg" class="mut"></span><span class="mut" style="margin-left:auto">Dá para enviar enquanto a análise roda. Máximo de 50 por envio.</span></div></div>
<script>
let S=null,sel=new Set(),tab=(location.hash||'#anuncios').slice(1),filt='pendente';
const $=id=>document.getElementById(id);
const acima=c=>c.dif!=null&&c.dif>Number((S&&S.settings.alerta_acima_ml)||0);
const brl=v=>v==null?'-':'R$ '+Number(v).toFixed(2).replace('.',',');
const esc=t=>String(t==null?'':t).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const NOMES={ml:'Mercado Livre (Duplo Giro)',bling:'Bling M3',shopee:'Shopee'};
const SIT={pendente:['Pronto','ok'],bloqueado:['Bloqueado','warn'],sem_custo:['Sem custo','bad'],sem_foto:['Sem foto','bad'],sem_estoque:['Sem estoque','warn'],simulado:['Simulado','warn'],criado:['Criado','ok'],erro:['Erro','bad'],erro_preco:['Erro de preço','bad']};
const PROB=['sem_custo','sem_foto','sem_estoque','bloqueado','erro','erro_preco'];
const match=(c,f)=>f==='acima'?acima(c):f==='problemas'?PROB.includes(c.status):(!f||c.status===f);
function go(t){tab=t;location.hash=t;if(t==='full')loadFull();draw()}
function setF(f){filt=f;draw()}
async function load(){const r=await fetch('/api/state');S=await r.json();draw();}
function draw(){
 if(!S)return;
 ['anuncios','full','conexoes','regras'].forEach(t=>{$('p-'+t).hidden=tab!==t;$('t-'+t).classList.toggle('on',tab===t)});
 $('bar').hidden=tab!=='anuncios';
 $('modo').innerHTML=S.dry?'<span class="mode sim">Modo simulação: nada é criado</span>':'<span class="mode live">Modo real: cria na Shopee</span>';
 $('dots').innerHTML=Object.keys(NOMES).map(k=>`<span title="${NOMES[k]}"><i class="dot ${S.conn[k]?'ok':''}"></i>${k==='ml'?'ML':k==='bling'?'Bling':'Shopee'}</span>`).join('');
 $('conns').innerHTML=Object.keys(NOMES).map(k=>{const on=S.conn[k],cfg=S.cfg_ok[k];
  return `<div class="conn"><b>${NOMES[k]}</b><span class="row"><span class="tag ${on?'ok':'bad'}">${on?'Conectado':(cfg?'Não conectado':'Faltam variáveis no Render')}</span>${cfg?`<a class="btn" href="/connect/${k}">${on?'Reconectar':'Conectar'}</a>`:''}</span></div>`}).join('');
 const st=S.settings,f=(id,v)=>{const e=$(id);if(document.activeElement!==e){if(e.type==='checkbox')e.checked=!!v;else e.value=v}};
 f('lucro',st.lucro);f('promo',st.promo);f('imposto',st.imposto);f('piso',st.piso);f('alerta_acima_ml',st.alerta_acima_ml);f('dias_cobertura',st.dias_cobertura);f('bling_loja_id',st.bling_loja_id);f('prefixos',st.prefixos);f('categoria_id',st.categoria_id);f('peso_padrao',st.peso_padrao);f('dim_padrao',st.dim_padrao.join(' x '));f('atributos',JSON.stringify(st.atributos));f('criar_sem_estoque',st.criar_sem_estoque);
 const n=fn=>S.cands.filter(fn).length;
 const T=[['pendente','Prontos para enviar',n(c=>c.status==='pendente'&&!acima(c)),'ok'],['acima','Acima do preço do ML',n(acima),'warn'],['problemas','Com pendência',n(c=>PROB.includes(c.status)),'bad'],['criado','Criados na Shopee',n(c=>c.status==='criado'),'']];
 $('tiles').innerHTML=T.map(t=>`<button class="tile ${t[3]} ${filt===t[0]?'on':''}" onclick="setF('${t[0]}')"><span class="num">${t[2]}</span><span>${t[1]}</span></button>`).join('');
 $('filtro').value=filt;
 const a=S.job,s=S.job_send;
 $('amsg').textContent=a.running?a.msg:(a.log.length?a.msg:'Pronto para analisar');
 $('aprog').style.width=(a.total?Math.round(a.done/a.total*100):0)+'%';$('alog').textContent=a.log.join('\n');
 $('smsg').textContent=s.running?('Enviando '+s.done+' de '+s.total+'...'):(s.log.length?s.msg:'');
 $('slog').textContent=s.log.join('\n');const y=S.job_sync;$('ymsg').textContent=y.running?('Sincronizando '+y.done+' de '+y.total+'...'):(y.log.length?y.msg:'');$('bSync').disabled=y.running;
 const fj=S.job_full;$('fmsg').textContent=fj.running?fj.msg:(fj.log.length?fj.msg:'');$('fprog').style.width=(fj.total?Math.round(fj.done/fj.total*100):0)+'%';$('bFull').disabled=fj.running;$('fdias').textContent=S.settings.dias_cobertura;
 if(tab==='full'&&S.full_quando!==fq)loadFull();
 $('bAn').disabled=a.running;
 const all=S.cands.filter(c=>match(c,filt)),rows=all.slice(0,500);
 $('cont').textContent=all.length+' itens'+(all.length>500?' (mostrando 500)':'');
 $('tb').innerHTML=rows.length?rows.map(c=>{const g=SIT[c.status]||[c.status,''];const can=['pendente','simulado','erro','bloqueado'].includes(c.status);
  return `<tr><td><input type="checkbox" ${can?'':'disabled'} ${sel.has(c.sku)?'checked':''} onchange="tog('${esc(c.sku)}',this.checked)" aria-label="Selecionar ${esc(c.sku)}"></td><td>${esc(c.sku)}</td><td>${c.link?`<a href="${esc(c.link)}" target="_blank" rel="noopener">${esc(c.titulo)}</a>`:esc(c.titulo)}</td><td class="n">${brl(c.custo)}</td><td class="n">${brl(c.preco)}</td><td class="n">${brl(c.preco_ml)}</td><td class="n" style="${acima(c)?'color:var(--bad);font-weight:600':''}">${c.dif==null?'-':(c.dif>0?'+':'')+Number(c.dif).toFixed(0)+'%'}</td><td class="n">${brl(c.lucro)}</td><td class="n">${c.qtd??'-'}</td><td class="n">${c.fotos}</td><td><span class="tag ${g[1]}">${g[0]}</span>${c.msg?`<div class="mut" style="font-size:12px;margin-top:3px">${esc(c.msg)}</div>`:''}</td></tr>`}).join(''):'<tr><td colspan="11" class="mut" style="padding:22px">Nenhum item neste filtro. Clique em Analisar para buscar os anúncios que faltam na Shopee.</td></tr>';
 upd();
}
function upd(){$('selinfo').textContent=sel.size+' selecionados';$('bEnv').disabled=!sel.size||(S&&S.job_send.running)}
function tog(s,on){if(on){if(sel.size>=S.max_lote){alert('O máximo é '+S.max_lote+' por envio.');draw();return}sel.add(s)}else sel.delete(s);upd()}
function sel50(){sel.clear();S.cands.filter(c=>c.status==='pendente'&&!acima(c)).slice(0,S.max_lote).forEach(c=>sel.add(c.sku));draw()}
function selNone(){sel.clear();draw()}
async function post(u,b){const r=await fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b||{})});const j=await r.json();if(!r.ok)alert(j.erro||'Erro');return j}
let F=null,fq=null,envMap={},fmin=Number(localStorage.getItem('fmin')||0);
const fenv=l=>envMap[l.sku]!==undefined?envMap[l.sku]:((l.vendas<fmin&&!l.manual)?0:(l.bl==null?l.sug:Math.min(l.sug,l.bl)));
function setMin(){fmin=Math.max(0,Math.floor(+$('fmin').value||0));localStorage.setItem('fmin',fmin);drawFull()}
const FS={coberto:['Coberto','ok'],enviar:['Enviar','ok'],parcial:['Bling não cobre','warn'],sem_estoque:['Sem estoque no Bling','bad'],sem_bling:['Não achei no Bling','bad']};
async function loadFull(){const r=await fetch('/api/full');F=await r.json();fq=F.quando;if(document.activeElement!==$('fmanual'))$('fmanual').value=(F.manual||[]).join('\n');drawFull()}
const ult=l=>{const P=(F.pedidos||[]).slice().sort((a,b)=>(a.data||'').localeCompare(b.data||'')||(a.criado||'').localeCompare(b.criado||''));for(let i=P.length-1;i>=0;i--){if(P[i].itens[l.sku]!==undefined)return P[i]}return null};
const ultCell=l=>{const u=ult(l);return u?`<td class="n" title="${esc(u.nome)} (${esc(u.data||'sem data')})">${u.itens[l.sku]}</td>`:'<td class="n mut">-</td>'};
const fiscCell=l=>l.fiscal===undefined?'<td class="mut">-</td>':(l.fiscal.length?`<td><span class="tag bad">${esc(l.fiscal.join(', '))}</span></td>`:'<td><span class="tag ok">Ok</span></td>');
function csvDown(nome,cab,rows){const q=v=>'"'+String(v==null?'':v).replace(/"/g,'""')+'"';const csv=[cab.join(';')].concat(rows.map(r=>r.map(q).join(';'))).join('\n');const a=document.createElement('a');a.href=URL.createObjectURL(new Blob(['\ufeff'+csv],{type:'text/csv;charset=utf-8'}));a.download=nome;a.click()}
const mlb1=l=>l.mlb||String(l.mlbs).split(' | ')[0];
function baixarCompra(){
 const m={};
 (F?F.linhas:[]).forEach(l=>{const q=fenv(l);if(q<=0)return;
  const sal={};Object.keys(l.cs||{}).forEach(k=>sal[k.toUpperCase()]=l.cs[k]);
  l.sku.split('-').map(x=>x.trim().toUpperCase()).filter(x=>x&&!x.startsWith('MLB')).forEach(c=>{
   const o=m[c]=m[c]||{q:0,usos:new Set(),bl:null};o.q+=q;o.usos.add(l.sku+' x'+q);if(o.bl==null&&sal[c]!=null)o.bl=sal[c]})});
 const R=Object.keys(m).sort().map(c=>{const o=m[c];return [c,o.q,o.bl==null?'':o.bl,o.bl==null?o.q:Math.max(0,o.q-o.bl),[...o.usos].join(' | ')]});
 csvDown('compra-full.csv',['SKU componente','Qtd necessaria','Saldo Bling M3','A comprar','Usado em'],R)}
function baixarFiscal(){
 const R=(F?F.linhas:[]).filter(l=>fenv(l)>0&&l.fiscal&&l.fiscal.length).map(l=>[mlb1(l),l.sku,l.titulo,fenv(l),l.fiscal.join(', ')]);
 csvDown('pendencias-fiscais.csv',['MLB','SKU','Titulo','Qtd a enviar','Situacao fiscal'],R)}
function baixarPedido(i){
 const p=F.pedidos[i],mp={};F.linhas.forEach(l=>mp[l.sku]=mlb1(l));
 csvDown((p.nome||'pedido').replace(/[^\w-]+/g,'_')+'.csv',['MLB','SKU','Qtd'],Object.keys(p.itens).map(s=>[mp[s]||'',s,p.itens[s]]))}
async function excluirPedido(i){const p=F.pedidos[i];if(!confirm('Excluir '+p.nome+' do histórico?'))return;await post('/api/full/pedido/excluir',{nome:p.nome});loadFull()}
async function finalizarPedido(){
 const itens={};(F.linhas||[]).forEach(l=>{const q=fenv(l);if(q>0)itens[l.sku]=q});
 const n=Object.keys(itens).length;if(!n){alert('Não há itens com quantidade para enviar.');return}
 const pecas=Object.values(itens).reduce((a,b)=>a+b,0);
 if(!confirm('Salvar '+n+' SKUs ('+pecas+' peças) no histórico?'))return;
 await post('/api/full/pedido',{nome:$('fped_nome').value,data:$('fped_data').value,itens});
 envMap={};$('fped_nome').value='';await loadFull()}
function drawHist(){
 $('fhist').innerHTML=(F.pedidos||[]).map((p,i)=>`<div class="conn"><span><b>${esc(p.nome)}</b> <span class="mut">${esc(p.data||'sem data')} · ${Object.keys(p.itens).length} SKUs · ${p.total} peças</span></span><span class="row"><button onclick="baixarPedido(${i})">Baixar</button><button onclick="excluirPedido(${i})">Excluir</button></span></div>`).reverse().join('')||'<span class="mut">Nenhum pedido salvo ainda.</span>'}
function drawTiles(){
 const L=F.linhas,c=f=>L.filter(f).length,env=L.filter(l=>fenv(l)>0),pecas=env.reduce((x,l)=>x+fenv(l),0);
 $('ftiles').innerHTML=[['Peças a enviar',pecas,'ok'],['SKUs a enviar',env.length,'ok'],['Cadastrar dados fiscais no ML',env.filter(l=>l.fiscal&&l.fiscal.length).length,'bad'],['Sem estoque no Bling M3',c(l=>['sem_estoque','sem_bling'].includes(l.status)),'bad'],['Já cobertos',c(l=>l.status==='coberto'),''],['SKUs analisados',L.length,'']].map(t=>`<div class="tile ${t[2]}"><span class="num">${t[1]}</span><span>${t[0]}</span></div>`).join('')}
function drawFull(){
 if(!F)return;
 $('fquando').textContent=F.quando?'Última análise: '+F.quando+' (UTC)':'Ainda não analisado';
 drawTiles();
 if(document.activeElement!==$('fmin'))$('fmin').value=fmin;
 const L=F.linhas;
 const fl=$('ffiltro').value,rows=L.filter(l=>!fl||(fl==='enviar'?fenv(l)>0:fl==='fiscal'?(fenv(l)>0&&!!l.fiscal&&l.fiscal.length>0):fl==='sem'?['sem_estoque','sem_bling'].includes(l.status):l.status==='coberto'));
 $('fcont').textContent=rows.length+' SKUs';drawHist();
 $('ftb').innerHTML=rows.length?rows.map(l=>{const g=FS[l.status];return `<tr><td>${esc(l.sku)}${l.manual?' <span class="tag">extra</span>':''}</td><td>${esc(l.titulo)}</td><td class="mut" style="font-size:12px">${esc(l.mlbs)}</td><td class="n">${l.vendas}</td>${ultCell(l)}<td class="n">${l.full}</td><td class="n">${l.alvo}</td><td class="n">${l.sug}</td><td class="n" title="${esc(l.det)}">${l.bl==null?'-':l.bl}</td><td class="n"><input type="number" min="0" value="${fenv(l)}" style="width:76px;padding:5px" onchange="envMap['${esc(l.sku)}']=Math.max(0,Math.floor(+this.value||0));drawTiles()"></td><td><span class="tag ${g[1]}">${g[0]}</span></td>${fiscCell(l)}</tr>`}).join(''):'<tr><td colspan="12" class="mut" style="padding:22px">Nada por aqui. Clique em Atualizar análise do Full.</td></tr>';
}
async function runFull(){await post('/api/full/run');load()}
async function saveManual(){await post('/api/full/manual',{texto:$('fmanual').value});await runFull()}
function baixarCsv(){
 const q=v=>'"'+String(v==null?'':v).replace(/"/g,'""')+'"';
 const L=(F?F.linhas:[]).filter(l=>fenv(l)>0);
 const csv=['MLB;SKU;Titulo;Vendas 30d;Saldo Full;Meta;Sugerido;Bling M3;Enviar'].concat(L.map(l=>[l.mlb||String(l.mlbs).split(' | ')[0],l.sku,l.titulo,l.vendas,l.full,l.alvo,l.sug,l.bl==null?'':l.bl,fenv(l)].map(q).join(';'))).join('\n');
 const a=document.createElement('a');a.href=URL.createObjectURL(new Blob(['\ufeff'+csv],{type:'text/csv;charset=utf-8'}));a.download='novo-envio-full.csv';a.click()}
async function sync(){await post('/api/sync');load()}
async function analisar(){await post('/api/analyze');load()}
async function enviar(){
 const n=sel.size;if(!n)return;
 if(!S.dry&&!confirm('Criar '+n+' anúncios na Shopee agora?'))return;
 const j=await post('/api/send',{skus:[...sel]});if(j.ok){sel.clear()}load()}
async function buscaCat(){
 $('rcat').textContent='Buscando...';
 try{const r=await fetch('/api/categorias?q='+encodeURIComponent($('qcat').value));const j=await r.json();
 $('rcat').innerHTML=Array.isArray(j)?(j.length?j.map(x=>`<div style="padding:6px 0;border-bottom:1px solid var(--line)"><button onclick="usaCat(${x.id})" style="padding:3px 10px;margin-right:8px">Usar</button><b>${x.id}</b> ${esc(x.caminho)}</div>`).join(''):'Nenhuma categoria encontrada.'):esc(j.erro||'Erro');
 }catch(e){$('rcat').textContent='Erro: '+e}}
async function usaCat(id){await post('/api/settings',{categoria_id:String(id)});$('rcat').textContent='Categoria '+id+' salva para todos os próximos envios.';load()}
async function salvar(){
 const dim=$('dim_padrao').value.split(/[x,;\s]+/).filter(Boolean).map(Number);
 const j=await post('/api/settings',{lucro:$('lucro').value,promo:$('promo').value,imposto:$('imposto').value,piso:$('piso').value,alerta_acima_ml:$('alerta_acima_ml').value,dias_cobertura:$('dias_cobertura').value,bling_loja_id:$('bling_loja_id').value,prefixos:$('prefixos').value,categoria_id:$('categoria_id').value,peso_padrao:$('peso_padrao').value,dim_padrao:dim,atributos:$('atributos').value,criar_sem_estoque:$('criar_sem_estoque').checked});
 if(j.ok)load()}
load();setInterval(load,4000);
</script></body></html>"""


@app.route("/")
def home():
    return render_template_string(PAGE)


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
