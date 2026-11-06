# -*- coding: utf-8 -*-
"""
Criador de anúncios Shopee  -  serviço SEPARADO do painel agente-shopee.

Fluxo:
  1. Analisar: compara os anúncios ativos do Mercado Livre (conta Duplo Giro) com os SKUs da Shopee.
  2. Para cada SKU que falta, busca custo/peso/medidas no Bling da irmã e calcula o preço.
  3. Você marca os itens e envia de 50 em 50 (fotos e descrição vêm do Mercado Livre).

Modo simulação (DRY_RUN=1, padrão): não cria nada na Shopee, só mostra o que seria enviado.
"""
import os, json, time, hmac, hashlib, threading, math, re, sqlite3, base64, unicodedata
from datetime import datetime
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
LOCAL_DEV = os.environ.get("LOCAL_DEV") == "1"
DRY_RUN_ENV = os.environ.get("DRY_RUN", "1") != "0"

SHOPEE_HOST = os.environ.get("SHOPEE_HOST", "https://partner.shopeemobile.com")
SHOPEE_PID = int(os.environ.get("SHOPEE_PARTNER_ID", "0") or 0)
SHOPEE_KEY = os.environ.get("SHOPEE_PARTNER_KEY", "")

ML_ID = os.environ.get("ML_CLIENT_ID", "")
ML_SECRET = os.environ.get("ML_CLIENT_SECRET", "")

BLING_ID = os.environ.get("BLING_CLIENT_ID", "")      # app criado no Bling da IRMÃ
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


def sp_attributes(cat):
    if cat not in _attr_cache:
        r = sp("/api/v2/product/get_attributes", params={"category_id": cat})
        _attr_cache[cat] = (r.get("response") or {}).get("attribute_list", [])
    return _attr_cache[cat]


def sp_logistics():
    if "l" not in _logi_cache:
        r = sp("/api/v2/logistics/get_channel_list")
        ch = (r.get("response") or {}).get("logistics_channel_list", [])
        _logi_cache["l"] = [{"logistic_id": c["logistics_channel_id"], "enabled": True} for c in ch if c.get("enabled")]
    return _logi_cache["l"]


def sp_category(title, s):
    if str(s.get("categoria_id") or "").strip():
        return int(s["categoria_id"])
    r = sp("/api/v2/product/category_recommend", params={"item_name": title[:120]})
    ids = (r.get("response") or {}).get("category_id") or []
    if not ids:
        raise RuntimeError("Shopee não sugeriu categoria; defina o ID da categoria nas configurações")
    return int(ids[0])


NEG = {"nao", "no", "none", "sem", "nenhum", "nao possui", "nao tem"}


def resolve_attrs(cat, s):
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
                                   "attributes": "id,title,seller_custom_field,pictures,attributes,available_quantity,permalink,status"})
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
                 "qtd": int(b.get("available_quantity") or 0), "fotos": fotos, "link": b.get("permalink")}
            if k not in out or len(fotos) > len(out[k]["fotos"]):
                out[k] = d
    log(f"ML: {len(out)} SKUs com o prefixo ({sem_sku} anúncios sem SKU ignorados)")
    return out


# ----------------------------------------------------------------------------
# Bling da irmã (custo, peso e medidas)
# ----------------------------------------------------------------------------
def bl_auth_url():
    return "https://www.bling.com.br/Api/v3/oauth/authorize?" + urlencode(
        {"response_type": "code", "client_id": BLING_ID, "state": "criador"})


def bl_post_token(data):
    basic = base64.b64encode(f"{BLING_ID}:{BLING_SECRET}".encode()).decode()
    r = requests.post("https://www.bling.com.br/Api/v3/oauth/token", data=data, timeout=30,
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
        raise RuntimeError("Bling da irmã não conectado")
    if t["expires_at"] - time.time() < 300:
        bl_post_token({"grant_type": "refresh_token", "refresh_token": t["refresh_token"]})
        t = kv_get("bling_tok")
    return t


def bl_get(path, params=None):
    t = bl_token()
    for tentativa in range(5):
        time.sleep(0.4)   # limite do Bling: 3 consultas por segundo
        r = requests.get("https://www.bling.com.br/Api/v3" + path, params=params, timeout=60,
                         headers={"Authorization": "Bearer " + t["access_token"]})
        if r.status_code == 429:
            time.sleep(2 + tentativa)
            continue
        return r.status_code, r.json()
    raise RuntimeError("Bling limitou as consultas (429)")


def bling_dados(sku):
    """Custo (fornecedor.precoCusto), peso e medidas do produto no Bling da irmã."""
    code, r = bl_get("/produtos", {"codigos[]": sku})
    if code != 200 or not r.get("data"):
        return None
    pid = r["data"][0]["id"]
    code, r = bl_get(f"/produtos/{pid}")
    if code != 200:
        return None
    d = r.get("data") or {}
    custo = (d.get("fornecedor") or {}).get("precoCusto") or d.get("precoCusto") or 0
    dim = d.get("dimensoes") or {}
    return {"custo": float(custo or 0), "peso": float(d.get("pesoBruto") or 0),
            "dim": [dim.get("profundidade") or 0, dim.get("largura") or 0, dim.get("altura") or 0]}


# ----------------------------------------------------------------------------
# Tarefas em segundo plano
# ----------------------------------------------------------------------------
JOB = {"running": False, "kind": "", "msg": "", "done": 0, "total": 0, "log": []}
JOB_LOCK = threading.Lock()


def jlog(m):
    JOB["msg"] = m
    JOB["log"] = (JOB["log"] + [f"{datetime.now().strftime('%H:%M:%S')} {m}"])[-60:]


def start_job(kind, fn, *a):
    with JOB_LOCK:
        if JOB["running"]:
            return False
        JOB.update(running=True, kind=kind, msg="Iniciando...", done=0, total=0, log=[])

    def wrap():
        try:
            fn(*a)
        except Exception as e:
            jlog(f"ERRO: {e}")
        finally:
            JOB["running"] = False
    threading.Thread(target=wrap, daemon=True).start()
    return True


def set_cand(sku, data, status, msg=""):
    run("DELETE FROM cria_cand WHERE sku=?", (sku,))
    run("INSERT INTO cria_cand (sku, data, status, msg, updated) VALUES (?,?,?,?,?)",
        (sku, json.dumps(data), status, msg, now()))


def aplica_regras(d, s):
    """Define situação e preço do candidato."""
    if not d.get("custo"):
        return "sem_custo", "sem custo no Bling da irmã"
    if not d.get("fotos"):
        return "sem_foto", "anúncio do ML sem fotos"
    if d["qtd"] <= 0 and not s["criar_sem_estoque"]:
        return "sem_estoque", "sem estoque no ML"
    p = calc_price(d["custo"], s)
    if not p:
        return "erro_preco", "não foi possível calcular o preço"
    d["preco"] = p
    d["calc"] = breakdown(p, d["custo"], s)
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
    JOB["total"] = len(faltam)
    for sku_n, d in faltam.items():
        if antigos.get(d["sku"]) == "criado":
            continue
        try:
            b = bling_dados(d["sku"])
        except Exception as e:
            jlog(f"Bling: {e}")
            b = None
        if b:
            d.update(custo=b["custo"], peso=b["peso"], dim=b["dim"])
        st, msg = aplica_regras(d, s)
        set_cand(d["sku"], d, st, msg)
        JOB["done"] += 1
        if JOB["done"] % 10 == 0:
            jlog(f"Preparados {JOB['done']} de {JOB['total']}")
    # remove quem já existe na Shopee agora ou não está mais ativo no ML
    atuais = {d["sku"] for d in faltam.values()}
    for sku, st_antigo in antigos.items():
        if st_antigo != "criado" and sku not in atuais:
            run("DELETE FROM cria_cand WHERE sku=?", (sku,))
    jlog(f"Análise concluída: {JOB['done']} itens preparados")


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
    attrs, problemas = resolve_attrs(cat, s)
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
        ids = []
        for i, url in enumerate(d["fotos"][:9]):
            img = requests.get(url, timeout=60).content
            ids.append(sp_upload_image(img, f"{sku}-{i}.jpg"))
    body = {
        "original_price": d["preco"], "description": texto, "item_name": d["titulo"][:120],
        "item_status": "NORMAL", "item_sku": d["sku"], "condition": "NEW", "category_id": cat,
        "normal_stock": d["qtd"], "weight": round(float(peso), 3),
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
    set_cand(sku, d, "criado", f"item {item_id}")
    return "criado", str(item_id)


def job_enviar(skus):
    s = settings()
    JOB["total"] = len(skus)
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
        JOB["done"] += 1
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


@app.route("/callback/<svc>")
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
                      "lucro": (d.get("calc") or {}).get("lucro"), "qtd": d.get("qtd"),
                      "fotos": len(d.get("fotos") or []), "status": st, "msg": msg, "link": d.get("link")})
    return jsonify({
        "conn": {"shopee": bool(kv_get("shopee_tok")), "ml": bool(kv_get("ml_tok")), "bling": bool(kv_get("bling_tok"))},
        "cfg_ok": {"shopee": bool(SHOPEE_PID and SHOPEE_KEY), "ml": bool(ML_ID and ML_SECRET), "bling": bool(BLING_ID and BLING_SECRET)},
        "settings": settings(), "dry": dry_run_on(), "job": JOB, "cands": cands, "max_lote": MAX_LOTE})


@app.route("/api/settings", methods=["POST"])
def api_settings():
    j = request.get_json(force=True)
    s = settings()
    try:
        for k in ("lucro", "promo", "imposto", "piso", "peso_padrao"):
            s[k] = float(str(j.get(k, s[k])).replace(",", "."))
        s["prefixos"] = str(j.get("prefixos", s["prefixos"]))
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
<style>
:root{--bg:#f4f5f3;--card:#fff;--ink:#1d2321;--mut:#65706b;--line:#dde2de;--acc:#0e6b5c;--acc-ink:#fff;--warn:#9a5b00;--bad:#b3261e;--ok:#17703a}
@media (prefers-color-scheme:dark){:root{--bg:#141817;--card:#1c2220;--ink:#e8ece9;--mut:#9aa6a0;--line:#2f3835;--acc:#3fb59f;--acc-ink:#07201b;--warn:#e0a24a;--bad:#ff8a80;--ok:#6fd49a}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:1100px;margin:0 auto;padding:18px 14px 80px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:16px;margin:0 0 10px}
.sub{color:var(--mut);margin:0 0 16px}
section{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:14px;margin-bottom:14px}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
button,.btn{font:inherit;border:1px solid var(--line);background:var(--card);color:var(--ink);border-radius:6px;padding:8px 12px;cursor:pointer;text-decoration:none;display:inline-block}
button.pri{background:var(--acc);color:var(--acc-ink);border-color:var(--acc);font-weight:600}
button:disabled{opacity:.5;cursor:not-allowed}
button:focus-visible,input:focus-visible,textarea:focus-visible,a:focus-visible{outline:2px solid var(--acc);outline-offset:2px}
.conn{display:flex;justify-content:space-between;align-items:center;gap:8px;padding:8px 0;border-bottom:1px solid var(--line)}
.conn:last-child{border:0}
.tag{font-size:12px;padding:2px 8px;border-radius:99px;border:1px solid var(--line);color:var(--mut)}
.tag.ok{color:var(--ok);border-color:var(--ok)}.tag.bad{color:var(--bad);border-color:var(--bad)}.tag.warn{color:var(--warn);border-color:var(--warn)}
label{display:block;font-size:13px;color:var(--mut);margin-bottom:3px}
input[type=text],input[type=number],textarea{width:100%;font:inherit;padding:7px 9px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--ink)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
.bar{height:8px;background:var(--line);border-radius:4px;overflow:hidden;margin:8px 0}
.bar i{display:block;height:100%;background:var(--acc);width:0}
#log{font:12px/1.5 ui-monospace,Menlo,Consolas,monospace;color:var(--mut);max-height:130px;overflow:auto;white-space:pre-wrap}
.tw{overflow-x:auto}
table{border-collapse:collapse;width:100%;min-width:760px}
th,td{padding:7px 8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{font-size:13px;color:var(--mut);font-weight:600;position:sticky;top:0;background:var(--card)}
td.n,th.n{text-align:right;font-variant-numeric:tabular-nums}
.sim{background:var(--warn);color:#fff;border-radius:6px;padding:6px 10px;font-size:13px}
.live{background:var(--bad);color:#fff;border-radius:6px;padding:6px 10px;font-size:13px}
.bottom{position:fixed;left:0;right:0;bottom:0;background:var(--card);border-top:1px solid var(--line);padding:10px 14px}
.bottom .in{max-width:1100px;margin:0 auto;display:flex;gap:10px;align-items:center;flex-wrap:wrap}
details summary{cursor:pointer;font-weight:600}
</style></head><body><main>
<h1>Criador de anúncios Shopee</h1>
<p class="sub">Compara o Mercado Livre com a Shopee e cria os anúncios que faltam, de 50 em 50.</p>

<section><div class="row" style="justify-content:space-between"><h2 style="margin:0">Conexões</h2><span id="modo"></span></div><div id="conns"></div></section>

<section><details><summary>Regras de preço e configurações</summary>
<div class="grid" style="margin-top:12px">
<div><label>Lucro (% do valor anunciado)</label><input type="number" step="0.1" id="lucro"></div>
<div><label>Promoção semanal (%)</label><input type="number" step="0.1" id="promo"></div>
<div><label>Imposto, pior cenário (%)</label><input type="number" step="0.1" id="imposto"></div>
<div><label>Preço mínimo anunciado (R$)</label><input type="number" step="0.01" id="piso"></div>
<div><label>Prefixos de SKU (vírgula)</label><input type="text" id="prefixos"></div>
<div><label>ID da categoria Shopee (vazio = automática)</label><input type="text" id="categoria_id"></div>
<div><label>Peso padrão (kg)</label><input type="number" step="0.01" id="peso_padrao"></div>
<div><label>Medidas padrão C x L x A (cm)</label><input type="text" id="dim_padrao"></div>
</div>
<div style="margin-top:10px"><label>Atributos obrigatórios da Shopee (JSON: nome → valor)</label><textarea id="atributos" rows="3"></textarea></div>
<div class="row" style="margin-top:10px"><label style="display:flex;gap:6px;align-items:center;margin:0"><input type="checkbox" id="criar_sem_estoque"> Criar também itens sem estoque no ML</label>
<button class="pri" onclick="salvar()">Salvar e recalcular preços</button></div>
</details></section>

<section><h2>1. Analisar</h2>
<div class="row"><button class="pri" id="bAn" onclick="analisar()">Analisar ML x Shopee</button>
<span class="tag" id="jobtag"></span></div>
<div class="bar"><i id="prog"></i></div><div id="log"></div></section>

<section><h2>2. Escolher e enviar</h2>
<div class="row" style="margin-bottom:10px">
<select id="filtro" onchange="draw()" style="font:inherit;padding:7px;border-radius:6px;border:1px solid var(--line);background:var(--bg);color:var(--ink)">
<option value="pendente">Prontos para enviar</option><option value="bloqueado">Bloqueados</option><option value="sem_custo">Sem custo</option><option value="sem_foto">Sem foto</option><option value="sem_estoque">Sem estoque</option><option value="simulado">Simulados</option><option value="criado">Criados</option><option value="erro">Com erro</option><option value="">Todos</option></select>
<button onclick="sel50()">Marcar os próximos 50</button><button onclick="selNone()">Limpar seleção</button><span class="tag" id="cont"></span></div>
<div class="tw"><table><thead><tr><th></th><th>SKU</th><th>Título</th><th class="n">Custo</th><th class="n">Preço</th><th class="n">Lucro</th><th class="n">Est.</th><th class="n">Fotos</th><th>Situação</th></tr></thead><tbody id="tb"></tbody></table></div>
</section></main>
<div class="bottom"><div class="in"><button class="pri" id="bEnv" onclick="enviar()" disabled>Enviar selecionados</button><span id="selinfo" class="tag">0 selecionados</span><span style="color:var(--mut);font-size:13px">Máximo de 50 por envio.</span></div></div>
<script>
let S=null,sel=new Set();
const $=id=>document.getElementById(id);
const brl=v=>v==null?'-':'R$ '+Number(v).toFixed(2).replace('.',',');
const esc=t=>String(t==null?'':t).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const NOMES={shopee:'Shopee',ml:'Mercado Livre (Duplo Giro)',bling:'Bling da irmã'};
const SIT={pendente:['Pronto','ok'],bloqueado:['Bloqueado','warn'],sem_custo:['Sem custo','bad'],sem_foto:['Sem foto','bad'],sem_estoque:['Sem estoque','warn'],simulado:['Simulado','warn'],criado:['Criado','ok'],erro:['Erro','bad'],erro_preco:['Erro de preço','bad']};
async function load(){const r=await fetch('/api/state');S=await r.json();draw();}
function draw(){
 if(!S)return;
 $('modo').innerHTML=S.dry?'<span class="sim">Modo simulação: nada é criado na Shopee</span>':'<span class="live">Modo real: os anúncios serão criados</span>';
 $('conns').innerHTML=Object.keys(NOMES).map(k=>{const on=S.conn[k],cfg=S.cfg_ok[k];
  return `<div class="conn"><span>${NOMES[k]}</span><span class="row"><span class="tag ${on?'ok':'bad'}">${on?'Conectado':(cfg?'Não conectado':'Faltam variáveis no Render')}</span>${cfg?`<a class="btn" href="/connect/${k}">${on?'Reconectar':'Conectar'}</a>`:''}</span></div>`}).join('');
 const st=S.settings,f=(id,v)=>{const e=$(id);if(document.activeElement!==e){if(e.type==='checkbox')e.checked=!!v;else e.value=v}};
 f('lucro',st.lucro);f('promo',st.promo);f('imposto',st.imposto);f('piso',st.piso);f('prefixos',st.prefixos);f('categoria_id',st.categoria_id);f('peso_padrao',st.peso_padrao);f('dim_padrao',st.dim_padrao.join(' x '));f('atributos',JSON.stringify(st.atributos));f('criar_sem_estoque',st.criar_sem_estoque);
 const j=S.job;$('jobtag').textContent=j.running?(j.kind==='analisar'?'Analisando...':'Enviando...'):'Parado';
 $('prog').style.width=(j.total?Math.round(j.done/j.total*100):0)+'%';$('log').textContent=j.log.join('\n');$('log').scrollTop=1e9;
 $('bAn').disabled=j.running;
 const fl=$('filtro').value,const all=S.cands.filter(c=>!fl||c.status===fl),rows=all.slice(0,500);
 $('cont').textContent=all.length+' itens'+(all.length>500?' (mostrando 500)':'');
 $('tb').innerHTML=rows.length?rows.map(c=>{const s=SIT[c.status]||[c.status,''];const can=c.status==='pendente'||c.status==='simulado'||c.status==='erro'||c.status==='bloqueado';
  return `<tr><td><input type="checkbox" ${can?'':'disabled'} ${sel.has(c.sku)?'checked':''} onchange="tog('${esc(c.sku)}',this.checked)" aria-label="Selecionar ${esc(c.sku)}"></td><td>${esc(c.sku)}</td><td>${c.link?`<a href="${esc(c.link)}" target="_blank" rel="noopener">${esc(c.titulo)}</a>`:esc(c.titulo)}</td><td class="n">${brl(c.custo)}</td><td class="n">${brl(c.preco)}</td><td class="n">${brl(c.lucro)}</td><td class="n">${c.qtd??'-'}</td><td class="n">${c.fotos}</td><td><span class="tag ${s[1]}">${s[0]}</span>${c.msg?`<div style="color:var(--mut);font-size:12px;margin-top:3px">${esc(c.msg)}</div>`:''}</td></tr>`}).join(''):'<tr><td colspan="9" style="color:var(--mut);padding:18px">Nada por aqui. Clique em "Analisar ML x Shopee" para buscar os anúncios que faltam.</td></tr>';
 upd();
}
function upd(){$('selinfo').textContent=sel.size+' selecionados';$('bEnv').disabled=!sel.size||(S&&S.job.running)}
function tog(s,on){if(on){if(sel.size>=S.max_lote){alert('O máximo é '+S.max_lote+' por envio.');draw();return}sel.add(s)}else sel.delete(s);upd()}
function sel50(){sel.clear();S.cands.filter(c=>c.status==='pendente').slice(0,S.max_lote).forEach(c=>sel.add(c.sku));draw()}
function selNone(){sel.clear();draw()}
async function post(u,b){const r=await fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b||{})});const j=await r.json();if(!r.ok)alert(j.erro||'Erro');return j}
async function analisar(){await post('/api/analyze');load()}
async function enviar(){
 const n=sel.size;if(!n)return;
 if(!S.dry&&!confirm('Criar '+n+' anúncios na Shopee agora?'))return;
 const j=await post('/api/send',{skus:[...sel]});if(j.ok){sel.clear()}load()}
async function salvar(){
 const dim=$('dim_padrao').value.split(/[x,;\s]+/).filter(Boolean).map(Number);
 const j=await post('/api/settings',{lucro:$('lucro').value,promo:$('promo').value,imposto:$('imposto').value,piso:$('piso').value,prefixos:$('prefixos').value,categoria_id:$('categoria_id').value,peso_padrao:$('peso_padrao').value,dim_padrao:dim,atributos:$('atributos').value,criar_sem_estoque:$('criar_sem_estoque').checked});
 if(j.ok)load()}
load();setInterval(load,4000);
</script></body></html>"""


@app.route("/")
def home():
    return render_template_string(PAGE)


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
