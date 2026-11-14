import os
import re
import secrets
import sqlite3
import threading
import unicodedata
from datetime import timedelta, datetime, timezone
from functools import wraps
from urllib.parse import urlencode

import requests
from flask import (
    Flask, abort, flash, jsonify, redirect, render_template,
    request, session, url_for
)

app = Flask(__name__)

app.secret_key = os.environ.get("FLASK_SECRET_KEY")
if not app.secret_key:
    raise RuntimeError(
        "Configure a variável de ambiente FLASK_SECRET_KEY antes de iniciar."
    )

DEV_MODE = os.environ.get("EB_DEV_MODE", "0") == "1"

if DEV_MODE and os.environ.get("FLASK_DEBUG") != "1":
    raise RuntimeError(
        "O modo de teste exige FLASK_DEBUG=1 e deve ser usado somente localmente."
    )

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "0") == "1",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
)

CLIENT_ID = os.environ.get("ROBLOX_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("ROBLOX_CLIENT_SECRET", "")

ADMIN_ROBLOX_ID = os.environ.get(
    "EB_ADMIN_ROBLOX_ID", "5604190095"
).strip()

ROBLOX_OPEN_CLOUD_API_KEY = os.environ.get(
    "ROBLOX_OPEN_CLOUD_API_KEY", ""
).strip()

ROBLOX_GROUP_ID = "509593146"
ROBLOX_GROUPS_URL = (
    f"https://apis.roblox.com/cloud/v2/groups/{ROBLOX_GROUP_ID}"
)
ROBLOX_AUTHORIZE_URL = "https://apis.roblox.com/oauth/v1/authorize"
ROBLOX_TOKEN_URL = "https://apis.roblox.com/oauth/v1/token"
ROBLOX_USERINFO_URL = "https://apis.roblox.com/oauth/v1/userinfo"
ROBLOX_USERS_URL = "https://users.roblox.com/v1/users"
ROBLOX_AVATAR_URL = "https://thumbnails.roblox.com/v1/users/avatar-headshot"

REDIRECT_URI = os.environ.get(
    "ROBLOX_REDIRECT_URI",
    "http://127.0.0.1:5000/auth/roblox/callback",
)

DATABASE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "eb_cyber.db",
)

PROFILE_CACHE_SECONDS = 300
HTTP_TIMEOUT = 12

SERVICES = [
    ("Canal Oficial", "megaphone", "Comunicados oficiais."),
    ("Informações do Site", "info", "Informações sobre o portal."),
    ("Central de Ajuda", "life-buoy", "Ajuda e perguntas frequentes."),
    ("Configurações", "settings", "Configurações da conta."),
    ("Resgatar Código", "ticket", "Resgate de códigos."),
    ("Loja EBCOIN", "coins", "Loja e saldo EBCOIN."),
    ("PASSE VIP (Manager)", "crown", "Gerenciamento do passe VIP."),
    ("Entregar Patente", "badge-check", "Ferramentas de patentes."),
    ("Promoção em Divisão", "users", "Promoções em divisões."),
    ("Estatísticas", "chart-no-axes-combined", "Estatísticas do grupo."),
    ("Hierarquia", "network", "Patentes e hierarquia."),
    ("Logs Globais", "scroll-text", "Registros administrativos."),
    ("CDP", "timer", "Consulta de períodos de CDP."),
    ("Histórico Patentes", "history", "Histórico de patentes."),
    ("Ranking", "trophy", "Classificações do grupo."),
    ("Formulários", "clipboard-list", "Formulários oficiais."),
    ("Documentos", "files", "Documentos do grupo."),
    ("Reportar Bug", "bug", "Relate um problema."),
    ("Atualizações", "refresh-cw", "Novidades do portal."),
    ("Mensagens", "mail", "Mensagens do portal."),
    ("Configurar Servidores", "server", "Configuração de servidores."),
    ("Msg. de Verificação", "shield-check", "Mensagens de verificação."),
]


FUSO_BRASILIA = timezone(timedelta(hours=-3))  # Brasil não tem horário de verão


@app.template_filter("data_br")
def filtro_data_br(valor):
    """Converte '2026-11-15T17:28:47+00:00' em '15/11/2026 14:28' (Brasília)."""
    if not valor:
        return ""

    try:
        momento = datetime.fromisoformat(str(valor))
    except ValueError:
        return str(valor)

    if momento.tzinfo is None:
        momento = momento.replace(tzinfo=FUSO_BRASILIA)

    return momento.astimezone(FUSO_BRASILIA).strftime("%d/%m/%Y %H:%M")


STATUS_ROTULOS = {
    "concluido": "Concluído",
    "falhou": "Falhou",
    "pendente": "Pendente",
    "aprovado": "Aprovado",
    "recusado": "Recusado",
}


@app.template_filter("status_br")
def filtro_status_br(valor):
    """'concluido' -> 'Concluído'; desconhecidos ficam com inicial maiúscula."""
    texto = str(valor or "")
    return STATUS_ROTULOS.get(texto, texto[:1].upper() + texto[1:])


def agora_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def slugify(text):
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def get_services():
    return [
        {
            "name": name,
            "icon": icon,
            "description": description,
            "slug": slugify(name),
        }
        for name, icon, description in SERVICES
    ]


def conectar_banco():
    db = sqlite3.connect(DATABASE, timeout=15)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db


def inicializar_banco():
    with conectar_banco() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS comunicados (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                titulo TEXT NOT NULL,
                conteudo TEXT NOT NULL,
                autor_id TEXT NOT NULL,
                autor_nome TEXT NOT NULL,
                fixado INTEGER NOT NULL DEFAULT 0,
                criado_em TEXT NOT NULL,
                atualizado_em TEXT
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS preferencias_usuario (
                user_id TEXT PRIMARY KEY,
                theme TEXT NOT NULL DEFAULT 'dark',
                show_avatar INTEGER NOT NULL DEFAULT 1,
                show_rank INTEGER NOT NULL DEFAULT 1,
                show_roblox_id INTEGER NOT NULL DEFAULT 1,
                atualizado_em TEXT NOT NULL
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS perfil_roblox_cache (
                user_id TEXT PRIMARY KEY,
                username TEXT,
                avatar_url TEXT,
                rank_name TEXT,
                rank_id TEXT,
                consultado_em TEXT NOT NULL
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS codigos_resgate (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                codigo TEXT NOT NULL COLLATE NOCASE UNIQUE,
                tipo TEXT NOT NULL CHECK(tipo IN ('promocao', 'moeda')),
                valor INTEGER NOT NULL DEFAULT 0,
                limite_resgates INTEGER,
                resgates_feitos INTEGER NOT NULL DEFAULT 0,
                criado_por TEXT NOT NULL,
                criado_em TEXT NOT NULL,
                expira_em TEXT,
                ativo INTEGER NOT NULL DEFAULT 1
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS resgates_codigos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                codigo_id INTEGER NOT NULL,
                user_id TEXT NOT NULL,
                username TEXT NOT NULL,
                resgatado_em TEXT NOT NULL,
                status TEXT NOT NULL,
                mensagem TEXT,
                FOREIGN KEY(codigo_id) REFERENCES codigos_resgate(id),
                UNIQUE(codigo_id, user_id)
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS saldos_ebcoin (
                user_id TEXT PRIMARY KEY,
                saldo INTEGER NOT NULL DEFAULT 0 CHECK(saldo >= 0),
                atualizado_em TEXT NOT NULL
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS movimentacoes_ebcoin (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                quantidade INTEGER NOT NULL,
                motivo TEXT NOT NULL,
                criado_em TEXT NOT NULL
            )
        """)

        # Produtos da Loja EBCOIN.
        # A categoria é exclusivamente Passe VIP ou Promoções.
        db.execute("""
            CREATE TABLE IF NOT EXISTS loja_produtos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nome TEXT NOT NULL,
                categoria TEXT NOT NULL
                    CHECK(categoria IN ('Passe VIP', 'Promoções')),
                descricao TEXT NOT NULL DEFAULT '',
                preco INTEGER NOT NULL CHECK(preco > 0),
                ativo INTEGER NOT NULL DEFAULT 1,
                criado_em TEXT NOT NULL,
                atualizado_em TEXT NOT NULL
            )
        """)

        # Cada pedido guarda uma cópia do nome e do preço no momento da compra.
        db.execute("""
            CREATE TABLE IF NOT EXISTS compras_loja (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                produto_id INTEGER,
                produto_nome TEXT NOT NULL,
                categoria TEXT NOT NULL,
                preco INTEGER NOT NULL CHECK(preco > 0),
                user_id TEXT NOT NULL,
                username TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pendente'
                    CHECK(status IN ('pendente', 'aprovado', 'recusado')),
                criado_em TEXT NOT NULL,
                atualizado_em TEXT NOT NULL,
                decidido_por TEXT,
                decidido_em TEXT,
                reembolso_feito INTEGER NOT NULL DEFAULT 0
                    CHECK(reembolso_feito IN (0, 1)),
                FOREIGN KEY(produto_id) REFERENCES loja_produtos(id)
                    ON DELETE SET NULL
            )
        """)

        db.execute("""
            CREATE INDEX IF NOT EXISTS idx_compras_loja_usuario
            ON compras_loja(user_id, id DESC)
        """)

        db.execute("""
            CREATE INDEX IF NOT EXISTS idx_compras_loja_status
            ON compras_loja(status, id DESC)
        """)

        # Migração: quantas patentes o produto promove na entrega automática.
        # 0 = entrega manual (ex.: Passe VIP).
        colunas = [
            linha["name"]
            for linha in db.execute("PRAGMA table_info(loja_produtos)")
        ]

        if "patentes" not in colunas:
            db.execute("""
                ALTER TABLE loja_produtos
                ADD COLUMN patentes INTEGER NOT NULL DEFAULT 0
            """)

            for nome_produto, qtd in (
                ("Promoção de 1 patente", 1),
                ("Promoção de 2 patentes", 2),
                ("Promoção de 3 patentes", 3),
            ):
                texto = "patente" if qtd == 1 else "patentes"
                db.execute("""
                    UPDATE loja_produtos
                    SET patentes = ?, descricao = ?
                    WHERE nome = ? AND categoria = 'Promoções'
                """, (
                    qtd,
                    f"Promoção de {qtd} {texto} aplicada automaticamente "
                    "no grupo Roblox logo após a compra.",
                    nome_produto,
                ))

        # Migração: cada pedido guarda quantas patentes devia receber e
        # quantas já foram entregues (para entrega parcial e aprovação manual).
        colunas_compras = [
            linha["name"]
            for linha in db.execute("PRAGMA table_info(compras_loja)")
        ]

        if "patentes" not in colunas_compras:
            db.execute("""
                ALTER TABLE compras_loja
                ADD COLUMN patentes INTEGER NOT NULL DEFAULT 0
            """)

        if "patentes_entregues" not in colunas_compras:
            db.execute("""
                ALTER TABLE compras_loja
                ADD COLUMN patentes_entregues INTEGER NOT NULL DEFAULT 0
            """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS configuracoes_loja (
                chave TEXT PRIMARY KEY,
                valor TEXT NOT NULL
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS notificacoes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                tipo TEXT NOT NULL,
                titulo TEXT NOT NULL,
                mensagem TEXT NOT NULL DEFAULT '',
                lida INTEGER NOT NULL DEFAULT 0,
                criado_em TEXT NOT NULL
            )
        """)

        db.execute("""
            CREATE INDEX IF NOT EXISTS idx_notificacoes_usuario
            ON notificacoes(user_id, lida, id DESC)
        """)


inicializar_banco()


def is_admin(user=None):
    user = user or session.get("user")
    return bool(
        user
        and ADMIN_ROBLOX_ID
        and str(user.get("id", "")) == ADMIN_ROBLOX_ID
    )


def somente_admin(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        user = session.get("user")

        if not user:
            flash("Entre com sua conta Roblox primeiro.", "warning")
            return redirect(url_for("index"))

        if not ADMIN_ROBLOX_ID:
            abort(503, description="O administrador não foi configurado.")

        if not is_admin(user):
            abort(403)

        return view(*args, **kwargs)

    return wrapper


def validar_csrf():
    token_formulario = request.form.get("csrf_token", "")
    token_sessao = session.get("csrf_token", "")

    if (
        not token_formulario
        or not token_sessao
        or not secrets.compare_digest(token_formulario, token_sessao)
    ):
        abort(400, description="Token de segurança inválido. Atualize a página.")


@app.before_request
def preparar_csrf():
    if session.get("user") and not session.get("csrf_token"):
        session["csrf_token"] = secrets.token_urlsafe(32)


@app.context_processor
def disponibilizar_contexto_global():
    user = session.get("user")
    preferences = None

    if user and user.get("id"):
        try:
            preferences = obter_preferencias(user["id"])
        except sqlite3.Error:
            app.logger.exception("Falha ao carregar preferências.")

    if not preferences:
        preferences = {
            "theme": "dark",
            "show_avatar": 1,
            "show_rank": 1,
            "show_roblox_id": 1,
        }

    nao_lidas = 0
    if user and user.get("id"):
        nao_lidas = contar_notificacoes_nao_lidas(user["id"])

    return {
        "csrf_token": lambda: session.get("csrf_token", ""),
        "preferences": preferences,
        "notificacoes_nao_lidas": nao_lidas,
    }


@app.after_request
def add_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Cache-Control"] = "no-store"
    return response


# -------------------- CONFIGURAÇÕES E PERFIL --------------------

def obter_preferencias(user_id):
    user_id = str(user_id)

    with conectar_banco() as db:
        row = db.execute(
            "SELECT * FROM preferencias_usuario WHERE user_id = ?",
            (user_id,),
        ).fetchone()

        if not row:
            db.execute("""
                INSERT OR IGNORE INTO preferencias_usuario
                    (user_id, theme, show_avatar, show_rank,
                     show_roblox_id, atualizado_em)
                VALUES (?, 'dark', 1, 1, 1, ?)
            """, (user_id, agora_iso()))

            row = db.execute(
                "SELECT * FROM preferencias_usuario WHERE user_id = ?",
                (user_id,),
            ).fetchone()

    return dict(row)


def salvar_preferencias(user_id, form):
    theme = form.get("theme", "dark")
    if theme not in ("dark", "light"):
        theme = "dark"

    valores = (
        str(user_id),
        theme,
        1 if form.get("show_avatar") == "1" else 0,
        1 if form.get("show_rank") == "1" else 0,
        1 if form.get("show_roblox_id") == "1" else 0,
        agora_iso(),
    )

    with conectar_banco() as db:
        db.execute("""
            INSERT INTO preferencias_usuario
                (user_id, theme, show_avatar, show_rank,
                 show_roblox_id, atualizado_em)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                theme = excluded.theme,
                show_avatar = excluded.show_avatar,
                show_rank = excluded.show_rank,
                show_roblox_id = excluded.show_roblox_id,
                atualizado_em = excluded.atualizado_em
        """, valores)


def obter_perfil_cache(user_id):
    with conectar_banco() as db:
        row = db.execute(
            "SELECT * FROM perfil_roblox_cache WHERE user_id = ?",
            (str(user_id),),
        ).fetchone()
    return dict(row) if row else None


def consultar_perfil_roblox(user):
    user_id = str(user.get("id", "")).strip()
    if not user_id.isdigit():
        raise ValueError("ID Roblox inválido.")

    username = user.get("name") or "Usuário Roblox"
    avatar_url = user.get("picture") or ""
    rank_name = "Configure a chave Open Cloud"
    rank_id = ""

    try:
        response = requests.get(
            f"{ROBLOX_USERS_URL}/{user_id}",
            timeout=HTTP_TIMEOUT,
        )
        response.raise_for_status()
        username = response.json().get("name") or username
    except (requests.RequestException, ValueError):
        app.logger.warning("Falha ao consultar perfil público Roblox.")

    try:
        response = requests.get(
            ROBLOX_AVATAR_URL,
            params={
                "userIds": user_id,
                "size": "150x150",
                "format": "Png",
                "isCircular": "true",
            },
            timeout=HTTP_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json().get("data", [])
        if data and data[0].get("imageUrl"):
            avatar_url = data[0]["imageUrl"]
    except (requests.RequestException, ValueError):
        app.logger.warning("Falha ao consultar avatar Roblox.")

    if ROBLOX_OPEN_CLOUD_API_KEY:
        try:
            membership = buscar_membro_grupo(user_id)
            if membership:
                role_ids = membership.get("_role_ids", [])
                rank_id = role_ids[0] if role_ids else ""
                rank_name = "Membro do grupo"

                if rank_id:
                    for cargo in listar_cargos_grupo():
                        cargo_id = id_recurso(
                            cargo.get("id") or cargo.get("path")
                        )
                        if cargo_id == rank_id:
                            rank_name = cargo.get(
                                "displayName", rank_name
                            )
                            break
            else:
                rank_name = "Não encontrado no grupo"
        except (requests.RequestException, ValueError, RuntimeError):
            rank_name = "Consulta indisponível"
            app.logger.warning("Falha ao consultar cargo do grupo.")

    agora = agora_iso()

    with conectar_banco() as db:
        db.execute("""
            INSERT INTO perfil_roblox_cache
                (user_id, username, avatar_url, rank_name,
                 rank_id, consultado_em)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                avatar_url = excluded.avatar_url,
                rank_name = excluded.rank_name,
                rank_id = excluded.rank_id,
                consultado_em = excluded.consultado_em
        """, (user_id, username, avatar_url, rank_name, rank_id, agora))

    return {
        "user_id": user_id,
        "username": username,
        "avatar_url": avatar_url,
        "rank_name": rank_name,
        "rank_id": rank_id,
        "consultado_em": agora,
    }


def obter_perfil(user, force_refresh=False):
    user_id = str(user.get("id", ""))
    cached = obter_perfil_cache(user_id)

    if cached and not force_refresh:
        try:
            consultado = datetime.fromisoformat(cached["consultado_em"])
            idade = (
                datetime.now().astimezone() - consultado.astimezone()
            ).total_seconds()
            if idade < PROFILE_CACHE_SECONDS:
                return cached
        except (ValueError, TypeError):
            pass

    try:
        return consultar_perfil_roblox(user)
    except (requests.RequestException, ValueError, RuntimeError):
        app.logger.exception("Falha ao atualizar perfil Roblox.")
        if cached:
            cached["warning"] = "Exibindo os últimos dados salvos."
            return cached

        return {
            "user_id": user_id,
            "username": user.get("name", "Usuário Roblox"),
            "avatar_url": user.get("picture", ""),
            "rank_name": "Consulta indisponível",
            "rank_id": "",
            "warning": "Não foi possível consultar o perfil agora.",
        }


# -------------------- SALDO E CÓDIGOS EBCOIN --------------------

def obter_saldo_ebcoin(user_id):
    with conectar_banco() as db:
        row = db.execute(
            "SELECT saldo FROM saldos_ebcoin WHERE user_id = ?",
            (str(user_id),),
        ).fetchone()

    return int(row["saldo"]) if row else 0


def listar_codigos():
    with conectar_banco() as db:
        rows = db.execute("""
            SELECT c.*,
                (
                    SELECT COUNT(*)
                    FROM resgates_codigos r
                    WHERE r.codigo_id = c.id
                      AND r.status = 'concluido'
                ) AS resgates_confirmados
            FROM codigos_resgate c
            ORDER BY c.id DESC
        """).fetchall()

    return [dict(row) for row in rows]


def listar_resgates_usuario(user_id):
    with conectar_banco() as db:
        rows = db.execute("""
            SELECT r.*, c.codigo, c.tipo, c.valor
            FROM resgates_codigos r
            JOIN codigos_resgate c ON c.id = r.codigo_id
            WHERE r.user_id = ?
            ORDER BY r.id DESC
            LIMIT 20
        """, (str(user_id),)).fetchall()

    return [dict(row) for row in rows]


def id_recurso(valor):
    """Extrai o ID final de 'users/123', 'groups/1/roles/2' ou de um dict.

    A Open Cloud v2 devolve 'user' e 'role' como texto (caminho do recurso),
    mas aceitamos dict também para não quebrar se o formato mudar.
    """
    if isinstance(valor, dict):
        valor = (
            valor.get("path")
            or valor.get("id")
            or valor.get("userId")
            or valor.get("name")
        )

    if valor is None or valor == "":
        return ""

    return str(valor).rstrip("/").split("/")[-1]


def cabecalhos_open_cloud(json_body=False):
    if not ROBLOX_OPEN_CLOUD_API_KEY:
        raise RuntimeError("A chave Roblox Open Cloud não foi configurada.")

    headers = {"x-api-key": ROBLOX_OPEN_CLOUD_API_KEY}
    if json_body:
        headers["Content-Type"] = "application/json"
    return headers


def listar_cargos_grupo():
    """Lista todos os cargos do grupo (com paginação)."""
    headers = cabecalhos_open_cloud()
    cargos = []
    page_token = None

    for _ in range(20):
        params = {"maxPageSize": 100}
        if page_token:
            params["pageToken"] = page_token

        response = requests.get(
            f"{ROBLOX_GROUPS_URL}/roles",
            headers=headers,
            params=params,
            timeout=HTTP_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
        cargos.extend(data.get("groupRoles", []))

        page_token = data.get("nextPageToken")
        if not page_token:
            break

    return cargos


def buscar_membro_grupo(user_id):
    headers = cabecalhos_open_cloud()
    page_token = None

    for _ in range(100):
        params = {"maxPageSize": 100}
        if page_token:
            params["pageToken"] = page_token

        response = requests.get(
            f"{ROBLOX_GROUPS_URL}/memberships",
            headers=headers,
            params=params,
            timeout=HTTP_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()

        for membership in data.get("groupMemberships", []):
            membro_id = id_recurso(
                membership.get("user") or membership.get("userId")
            )

            if membro_id == str(user_id):
                path = membership.get("path", "")
                membership_id = (
                    path.rstrip("/").rsplit("/", 1)[-1]
                    if path else str(membership.get("id", ""))
                )

                if not membership_id:
                    raise RuntimeError(
                        "A Roblox não retornou o identificador da associação."
                    )

                # Normaliza para o resto do código não depender do formato.
                role_ids = membership.get("roles")
                if role_ids is None:
                    role_ids = membership.get("role")
                if not isinstance(role_ids, list):
                    role_ids = [role_ids] if role_ids else []

                membership["_membership_id"] = membership_id
                membership["_role_ids"] = [
                    rid for rid in (id_recurso(r) for r in role_ids) if rid
                ]
                return membership

        page_token = data.get("nextPageToken")
        if not page_token:
            return None

    raise RuntimeError(
        "Não foi possível localizar o membro após 100 páginas."
    )


def promover_membro_uma_patente(user_id):
    """Promove o membro uma patente e remove as antigas.

    Retorna (nome_do_novo_cargo, lista_de_avisos). O assignRole da Open Cloud
    apenas ADICIONA um cargo, então o novo é atribuído primeiro (para o membro
    nunca ficar sem cargo) e só depois os antigos são removidos.
    """
    headers = cabecalhos_open_cloud(json_body=True)

    membership = buscar_membro_grupo(user_id)
    if not membership:
        raise RuntimeError(
            "Você precisa fazer parte do grupo Roblox para usar este código."
        )

    role_ids = membership["_role_ids"]
    if not role_ids:
        raise RuntimeError("Não foi possível identificar a patente atual.")

    roles = listar_cargos_grupo()
    mapa = {
        id_recurso(role.get("id") or role.get("path")): role
        for role in roles
    }

    if any(role_id not in mapa for role_id in role_ids):
        raise RuntimeError("Não foi possível identificar todas as patentes atuais.")

    ranks_atuais = [mapa[role_id].get("rank") for role_id in role_ids]
    if not all(isinstance(rank, int) for rank in ranks_atuais):
        raise RuntimeError(
            "A Roblox não retornou a hierarquia numérica das patentes."
        )

    # Se o membro já tem cargos acumulados, vale o mais alto.
    current_rank = max(ranks_atuais)

    higher_roles = [
        role for role in roles
        if isinstance(role.get("rank"), int)
        and role["rank"] > current_rank
    ]
    higher_roles.sort(key=lambda role: role["rank"])

    if not higher_roles:
        raise RuntimeError("Você já está na patente mais alta disponível.")

    next_role = higher_roles[0]
    next_role_id = id_recurso(next_role.get("id") or next_role.get("path"))
    membership_id = membership["_membership_id"]
    base = f"{ROBLOX_GROUPS_URL}/memberships/{membership_id}"

    response = requests.post(
        f"{base}:assignRole",
        headers=headers,
        json={"role": f"groups/{ROBLOX_GROUP_ID}/roles/{next_role_id}"},
        timeout=HTTP_TIMEOUT,
    )
    response.raise_for_status()

    avisos = []
    for old_id in role_ids:
        if old_id == next_role_id:
            continue

        try:
            response = requests.post(
                f"{base}:unassignRole",
                headers=headers,
                json={"role": f"groups/{ROBLOX_GROUP_ID}/roles/{old_id}"},
                timeout=HTTP_TIMEOUT,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            nome_antigo = mapa[old_id].get("displayName", old_id)
            app.logger.error(
                "Promoveu %s mas não removeu o cargo antigo %s: %s",
                user_id, nome_antigo, exc,
            )
            avisos.append(nome_antigo)

    return next_role.get("displayName", "Patente promovida"), avisos


# -------------------- LOJA EBCOIN --------------------

def listar_produtos(ativos_apenas=False):
    consulta = "SELECT * FROM loja_produtos"
    parametros = ()

    if ativos_apenas:
        consulta += " WHERE ativo = 1"

    consulta += """
        ORDER BY
            CASE categoria
                WHEN 'Passe VIP' THEN 1
                WHEN 'Promoções' THEN 2
                ELSE 3
            END,
            id ASC
    """

    with conectar_banco() as db:
        rows = db.execute(consulta, parametros).fetchall()

    return [dict(row) for row in rows]


def listar_compras_usuario(user_id):
    with conectar_banco() as db:
        rows = db.execute("""
            SELECT *
            FROM compras_loja
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT 30
        """, (str(user_id),)).fetchall()

    return [dict(row) for row in rows]


def listar_compras_admin():
    with conectar_banco() as db:
        rows = db.execute("""
            SELECT *
            FROM compras_loja
            ORDER BY
                CASE status
                    WHEN 'pendente' THEN 1
                    WHEN 'aprovado' THEN 2
                    ELSE 3
                END,
                id DESC
            LIMIT 200
        """).fetchall()

    return [dict(row) for row in rows]


# -------------------- NOTIFICAÇÕES --------------------

MAX_NOTIFICACOES_POR_USUARIO = 100


def notificar(user_id, tipo, titulo, mensagem=""):
    """Cria uma notificação para o jogador. Nunca derruba a requisição."""
    try:
        with conectar_banco() as db:
            db.execute("""
                INSERT INTO notificacoes
                    (user_id, tipo, titulo, mensagem, criado_em)
                VALUES (?, ?, ?, ?, ?)
            """, (str(user_id), tipo, titulo[:120], mensagem[:500], agora_iso()))

            db.execute("""
                DELETE FROM notificacoes
                WHERE user_id = ?
                  AND id NOT IN (
                      SELECT id FROM notificacoes
                      WHERE user_id = ?
                      ORDER BY id DESC
                      LIMIT ?
                  )
            """, (str(user_id), str(user_id), MAX_NOTIFICACOES_POR_USUARIO))
    except sqlite3.Error:
        app.logger.exception("Falha ao criar notificação.")


def contar_notificacoes_nao_lidas(user_id):
    try:
        with conectar_banco() as db:
            linha = db.execute("""
                SELECT COUNT(*) AS total
                FROM notificacoes
                WHERE user_id = ? AND lida = 0
            """, (str(user_id),)).fetchone()
        return int(linha["total"])
    except sqlite3.Error:
        return 0


# -------------------- LIMITE DA ENTREGA AUTOMÁTICA --------------------

def obter_rank_maximo_automatico():
    """Maior rank que o sistema pode entregar sozinho (None = nenhum)."""
    with conectar_banco() as db:
        linha = db.execute("""
            SELECT valor FROM configuracoes_loja
            WHERE chave = 'rank_maximo_automatico'
        """).fetchone()

    if not linha:
        return None

    try:
        return int(linha["valor"])
    except (TypeError, ValueError):
        return None


def listar_cargos_para_admin():
    try:
        cargos = [
            {"rank": c["rank"], "nome": c.get("displayName", str(c["rank"]))}
            for c in listar_cargos_grupo()
            if isinstance(c.get("rank"), int)
        ]
    except (RuntimeError, requests.RequestException):
        return []

    return sorted(cargos, key=lambda c: c["rank"])


def mensagem_erro(exc):
    if isinstance(exc, RuntimeError):
        return str(exc)
    return "A Roblox não respondeu como esperado. Tente novamente depois."


def analisar_promocao(user_id, quantidade):
    """Descobre em que patente o membro ficaria após `quantidade` promoções."""
    membership = buscar_membro_grupo(user_id)
    if not membership:
        raise RuntimeError(
            "Você precisa fazer parte do grupo Roblox para receber a promoção."
        )

    role_ids = membership["_role_ids"]
    if not role_ids:
        raise RuntimeError("Não foi possível identificar a patente atual.")

    roles = listar_cargos_grupo()
    mapa = {
        id_recurso(role.get("id") or role.get("path")): role
        for role in roles
    }

    if any(role_id not in mapa for role_id in role_ids):
        raise RuntimeError("Não foi possível identificar todas as patentes atuais.")

    ranks = [mapa[role_id].get("rank") for role_id in role_ids]
    if not all(isinstance(rank, int) for rank in ranks):
        raise RuntimeError("A Roblox não retornou a hierarquia numérica das patentes.")

    atual = max(ranks)
    acima = sorted(
        (r for r in roles
         if isinstance(r.get("rank"), int) and r["rank"] > atual),
        key=lambda r: r["rank"],
    )

    if len(acima) < quantidade:
        raise RuntimeError(
            "Não há patentes suficientes acima da sua para essa promoção."
        )

    alvo = acima[quantidade - 1]
    return {
        "rank_atual": atual,
        "rank_final": alvo["rank"],
        "nome_final": alvo.get("displayName", "patente superior"),
    }


ENTREGA_LOCK = threading.Lock()
SISTEMA_ID = "sistema"


def concluir_compra_automatica(compra_id):
    """Marca o pedido como aprovado pelo sistema (entrega já realizada)."""
    agora = agora_iso()
    with conectar_banco() as db:
        db.execute("""
            UPDATE compras_loja
            SET status = 'aprovado', atualizado_em = ?,
                decidido_por = ?, decidido_em = ?
            WHERE id = ? AND status = 'pendente'
        """, (agora, SISTEMA_ID, agora, compra_id))


def reembolsar_compra_automatica(compra_id):
    """Recusa o pedido pendente e devolve o EBCOIN, numa só transação."""
    agora = agora_iso()
    with conectar_banco() as db:
        db.execute("BEGIN IMMEDIATE")
        compra = db.execute(
            "SELECT * FROM compras_loja WHERE id = ?", (compra_id,)
        ).fetchone()

        if not compra or compra["status"] != "pendente":
            db.rollback()
            return False

        user_id = str(compra["user_id"])
        preco = int(compra["preco"])

        db.execute("""
            INSERT INTO saldos_ebcoin (user_id, saldo, atualizado_em)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                saldo = saldos_ebcoin.saldo + excluded.saldo,
                atualizado_em = excluded.atualizado_em
        """, (user_id, preco, agora))

        db.execute("""
            INSERT INTO movimentacoes_ebcoin
                (user_id, quantidade, motivo, criado_em)
            VALUES (?, ?, ?, ?)
        """, (
            user_id, preco,
            f"Reembolso automático: entrega do pedido #{compra_id} falhou",
            agora,
        ))

        db.execute("""
            UPDATE compras_loja
            SET status = 'recusado', atualizado_em = ?,
                decidido_por = ?, decidido_em = ?, reembolso_feito = 1
            WHERE id = ? AND status = 'pendente'
        """, (agora, SISTEMA_ID, agora, compra_id))

        db.commit()
    return True


def entregar_pedido(compra_id):
    """Entrega as patentes que ainda faltam de um pedido pendente.

    Retorna (promovidas_agora, ultimo_cargo, erro). Relê o pedido dentro do
    bloqueio, então um clique duplo não promove duas vezes.
    """
    promovidas = 0
    ultimo_cargo = None

    with ENTREGA_LOCK:
        with conectar_banco() as db:
            compra = db.execute(
                "SELECT * FROM compras_loja WHERE id = ?", (compra_id,)
            ).fetchone()

        if not compra or compra["status"] != "pendente":
            return 0, None, "Esse pedido não está mais pendente."

        user_id = str(compra["user_id"])
        restante = int(compra["patentes"] or 0) - int(compra["patentes_entregues"] or 0)

        for _ in range(max(restante, 0)):
            try:
                ultimo_cargo, avisos = promover_membro_uma_patente(user_id)
            except (RuntimeError, requests.RequestException) as exc:
                app.logger.error(
                    "Entrega do pedido %s falhou após %s promoção(ões): %s",
                    compra_id, promovidas, exc,
                )
                return promovidas, ultimo_cargo, mensagem_erro(exc)

            promovidas += 1

            with conectar_banco() as db:
                db.execute("""
                    UPDATE compras_loja
                    SET patentes_entregues = patentes_entregues + 1,
                        atualizado_em = ?
                    WHERE id = ?
                """, (agora_iso(), compra_id))

            if avisos:
                app.logger.error(
                    "Cargos antigos não removidos de %s: %s", user_id, avisos
                )

    return promovidas, ultimo_cargo, None


@app.route(
    "/servico/loja-ebcoin/comprar/<int:produto_id>",
    methods=["POST"],
)
def comprar_produto(produto_id):
    user = session.get("user")

    if not user:
        flash("Entre com sua conta Roblox antes de comprar.", "warning")
        return redirect(url_for("index"))

    validar_csrf()

    # O formulário só envia "sim" depois da confirmação no navegador.
    if request.form.get("confirmar") != "sim":
        flash("A compra não foi confirmada.", "warning")
        return redirect(url_for("service_page", slug="loja-ebcoin"))

    user_id = str(user.get("id", "")).strip()
    username = str(user.get("name", "Usuário Roblox")).strip()

    if not user_id:
        flash("Não foi possível identificar sua conta Roblox.", "error")
        return redirect(url_for("service_page", slug="loja-ebcoin"))

    try:
        with conectar_banco() as db:
            db.execute("BEGIN IMMEDIATE")

            produto = db.execute("""
                SELECT *
                FROM loja_produtos
                WHERE id = ? AND ativo = 1
            """, (produto_id,)).fetchone()

            if not produto:
                db.rollback()
                flash("Esse produto não existe ou está indisponível.", "warning")
                return redirect(url_for("service_page", slug="loja-ebcoin"))

            preco = int(produto["preco"])

            # A atualização condicional impede saldo negativo.
            debito = db.execute("""
                UPDATE saldos_ebcoin
                SET saldo = saldo - ?, atualizado_em = ?
                WHERE user_id = ? AND saldo >= ?
            """, (preco, agora_iso(), user_id, preco))

            if debito.rowcount != 1:
                db.rollback()
                flash(
                    "Saldo insuficiente para comprar esse produto.",
                    "warning",
                )
                return redirect(url_for("service_page", slug="loja-ebcoin"))

            db.execute("""
                INSERT INTO movimentacoes_ebcoin
                    (user_id, quantidade, motivo, criado_em)
                VALUES (?, ?, ?, ?)
            """, (
                user_id,
                -preco,
                f"Compra na Loja EBCOIN: {produto['nome']}",
                agora_iso(),
            ))

            insercao = db.execute("""
                INSERT INTO compras_loja
                    (produto_id, produto_nome, categoria, preco,
                     user_id, username, status, criado_em, atualizado_em,
                     patentes)
                VALUES (?, ?, ?, ?, ?, ?, 'pendente', ?, ?, ?)
            """, (
                produto["id"],
                produto["nome"],
                produto["categoria"],
                preco,
                user_id,
                username,
                agora_iso(),
                agora_iso(),
                (
                    int(produto["patentes"] or 0)
                    if produto["categoria"] == "Promoções" else 0
                ),
            ))

            compra_id = insercao.lastrowid
            db.commit()

    except sqlite3.Error:
        app.logger.exception("Falha ao processar compra da Loja EBCOIN.")
        flash(
            "Não foi possível concluir a compra. Seu saldo deve ser conferido "
            "antes de tentar novamente.",
            "error",
        )
        return redirect(url_for("service_page", slug="loja-ebcoin"))

    patentes = int(produto["patentes"] or 0)
    nome_produto = produto["nome"]
    destino = url_for("service_page", slug="loja-ebcoin")

    notificar(
        user_id, "compra", "Compra realizada",
        f"Você comprou {nome_produto} por {preco} EBCOIN. Pedido #{compra_id}.",
    )

    # Produto sem entrega automática (ex.: Passe VIP): segue para análise.
    if produto["categoria"] != "Promoções" or patentes <= 0:
        notificar(
            user_id, "pedido", "Pedido em análise",
            f"O pedido #{compra_id} ({nome_produto}) aguarda a administração.",
        )
        flash(
            "Pedido registrado! O valor foi debitado e o pedido aguarda "
            "análise administrativa.",
            "success",
        )
        return redirect(destino)

    if not ROBLOX_OPEN_CLOUD_API_KEY:
        notificar(
            user_id, "pedido", "Pedido em análise",
            f"A entrega automática está indisponível. O pedido #{compra_id} "
            "será concluído pela administração.",
        )
        flash(
            "Pedido registrado! A entrega automática está indisponível no "
            "momento, então um administrador vai concluir a promoção.",
            "warning",
        )
        return redirect(destino)

    # Antes de entregar, confere em que patente o jogador ficaria.
    try:
        analise = analisar_promocao(user_id, patentes)
    except (RuntimeError, requests.RequestException) as exc:
        app.logger.error("Análise da promoção falhou para %s: %s", user_id, exc)
        erro = mensagem_erro(exc)
        reembolsar_compra_automatica(compra_id)
        notificar(
            user_id, "reembolso", "EBCOIN devolvido",
            f"Não foi possível entregar {nome_produto}: {erro} "
            f"Os {preco} EBCOIN foram devolvidos.",
        )
        flash(
            f"Não foi possível entregar a promoção: {erro} "
            "Seu EBCOIN foi devolvido.",
            "error",
        )
        return redirect(destino)

    limite = obter_rank_maximo_automatico()

    if limite is None or analise["rank_final"] > limite:
        notificar(
            user_id, "pedido", "Pedido em análise",
            f"{nome_produto} levaria você a {analise['nome_final']}, patente "
            f"que precisa de aprovação da administração. Pedido #{compra_id}.",
        )
        flash(
            "Pedido registrado! Essa promoção leva você a uma patente que "
            "precisa ser aprovada pela administração. O valor foi debitado "
            "e será devolvido se o pedido for recusado.",
            "warning",
        )
        return redirect(destino)

    promovidas, cargo, erro = entregar_pedido(compra_id)

    if erro is None:
        concluir_compra_automatica(compra_id)
        notificar(
            user_id, "promocao", "Promoção recebida",
            f"Você foi promovido para {cargo}. Pedido #{compra_id}.",
        )
        flash(
            f"Promoção entregue! Sua nova patente é {cargo}.",
            "success",
        )
    elif promovidas == 0:
        reembolsar_compra_automatica(compra_id)
        notificar(
            user_id, "reembolso", "EBCOIN devolvido",
            f"Não foi possível entregar {nome_produto}: {erro} "
            f"Os {preco} EBCOIN foram devolvidos.",
        )
        flash(
            f"Não foi possível entregar a promoção: {erro} "
            "Seu EBCOIN foi devolvido.",
            "error",
        )
    else:
        notificar(
            user_id, "pedido", "Promoção parcial",
            f"{promovidas} de {patentes} promoções foram aplicadas "
            f"(patente atual: {cargo}). A administração vai concluir o pedido "
            f"#{compra_id}.",
        )
        flash(
            f"{promovidas} de {patentes} promoções foram aplicadas "
            f"(patente atual: {cargo}), mas a entrega parou: {erro} "
            "Um administrador vai concluir o restante do pedido.",
            "warning",
        )

    return redirect(destino)


@app.route(
    "/servico/loja-ebcoin/admin/produto/salvar",
    methods=["POST"],
)
@somente_admin
def salvar_produto_loja():
    validar_csrf()

    produto_id_texto = request.form.get("produto_id", "").strip()
    nome = request.form.get("nome", "").strip()
    categoria = request.form.get("categoria", "").strip()
    descricao = request.form.get("descricao", "").strip()
    preco_texto = request.form.get("preco", "").strip()
    ativo = 1 if request.form.get("ativo") == "on" else 0
    patentes_texto = request.form.get("patentes", "0").strip() or "0"

    destino = url_for("service_page", slug="loja-ebcoin")

    if not nome or len(nome) > 100:
        flash("O nome do produto deve ter entre 1 e 100 caracteres.", "warning")
        return redirect(destino)

    if categoria not in ("Passe VIP", "Promoções"):
        flash("Escolha Passe VIP ou Promoções como categoria.", "warning")
        return redirect(destino)

    if len(descricao) > 1000:
        flash("A descrição não pode ultrapassar 1000 caracteres.", "warning")
        return redirect(destino)

    try:
        preco = int(preco_texto)
        if preco <= 0 or preco > 2_000_000_000:
            raise ValueError
    except ValueError:
        flash("O preço deve ser um número inteiro positivo.", "warning")
        return redirect(destino)

    if categoria == "Promoções":
        try:
            patentes = int(patentes_texto)
            if patentes < 1 or patentes > 100:
                raise ValueError
        except ValueError:
            flash(
                "Informe quantas patentes a promoção dá (de 1 a 100).",
                "warning",
            )
            return redirect(destino)
    else:
        patentes = 0

    agora = agora_iso()

    try:
        with conectar_banco() as db:
            if produto_id_texto:
                try:
                    produto_id = int(produto_id_texto)
                    if produto_id <= 0:
                        raise ValueError
                except ValueError:
                    flash("Identificador de produto inválido.", "warning")
                    return redirect(destino)

                resultado = db.execute("""
                    UPDATE loja_produtos
                    SET nome = ?, categoria = ?, descricao = ?,
                        preco = ?, ativo = ?, patentes = ?, atualizado_em = ?
                    WHERE id = ?
                """, (
                    nome,
                    categoria,
                    descricao,
                    preco,
                    ativo,
                    patentes,
                    agora,
                    produto_id,
                ))

                if resultado.rowcount == 0:
                    abort(404)

                flash("Produto atualizado com sucesso.", "success")

            else:
                db.execute("""
                    INSERT INTO loja_produtos
                        (nome, categoria, descricao, preco, ativo,
                         patentes, criado_em, atualizado_em)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    nome,
                    categoria,
                    descricao,
                    preco,
                    ativo,
                    patentes,
                    agora,
                    agora,
                ))

                flash("Produto criado com sucesso.", "success")

    except sqlite3.Error:
        app.logger.exception("Falha ao salvar produto da loja.")
        flash("Não foi possível salvar o produto.", "error")

    return redirect(destino)


@app.route(
    "/servico/loja-ebcoin/admin/produto/<int:produto_id>/alternar",
    methods=["POST"],
)
@somente_admin
def alternar_produto_loja(produto_id):
    validar_csrf()

    with conectar_banco() as db:
        produto = db.execute("""
            SELECT id, ativo
            FROM loja_produtos
            WHERE id = ?
        """, (produto_id,)).fetchone()

        if not produto:
            abort(404)

        novo_estado = 0 if produto["ativo"] else 1

        db.execute("""
            UPDATE loja_produtos
            SET ativo = ?, atualizado_em = ?
            WHERE id = ?
        """, (novo_estado, agora_iso(), produto_id))

    flash(
        "Produto ativado." if novo_estado else "Produto desativado.",
        "success",
    )
    return redirect(url_for("service_page", slug="loja-ebcoin"))


@app.route(
    "/servico/loja-ebcoin/admin/produto/<int:produto_id>/excluir",
    methods=["POST"],
)
@somente_admin
def excluir_produto_loja(produto_id):
    validar_csrf()

    with conectar_banco() as db:
        produto = db.execute(
            "SELECT nome FROM loja_produtos WHERE id = ?", (produto_id,)
        ).fetchone()

        if not produto:
            abort(404)

        # Os pedidos antigos guardam nome e preço próprios, então o
        # histórico continua intacto (produto_id vira vazio).
        db.execute("DELETE FROM loja_produtos WHERE id = ?", (produto_id,))

    flash(f"Produto \"{produto['nome']}\" excluído.", "success")
    return redirect(url_for("service_page", slug="loja-ebcoin"))


@app.route(
    "/servico/loja-ebcoin/admin/compra/<int:compra_id>/decidir",
    methods=["POST"],
)
@somente_admin
def decidir_compra_loja(compra_id):
    validar_csrf()

    decisao = request.form.get("decisao", "").strip().lower()

    if decisao not in ("aprovar", "recusar"):
        flash("Decisão inválida.", "warning")
        return redirect(url_for("service_page", slug="loja-ebcoin"))

    user_admin = session["user"]
    admin_id = str(user_admin.get("id", ""))
    agora = agora_iso()
    cargo_entregue = None

    # Ao aprovar uma promoção pendente, o portal aplica as patentes que
    # faltam (sem limite de patente: a decisão é do administrador).
    if decisao == "aprovar" and ROBLOX_OPEN_CLOUD_API_KEY:
        with conectar_banco() as db:
            previa = db.execute(
                "SELECT * FROM compras_loja WHERE id = ?", (compra_id,)
            ).fetchone()

        if (
            previa
            and previa["status"] == "pendente"
            and int(previa["patentes"] or 0) - int(previa["patentes_entregues"] or 0) > 0
        ):
            promovidas, cargo_entregue, erro = entregar_pedido(compra_id)

            if erro is not None:
                flash(
                    f"A entrega falhou ({promovidas} promoção(ões) aplicada(s) "
                    f"agora): {erro} O pedido continua pendente.",
                    "error",
                )
                return redirect(url_for("service_page", slug="loja-ebcoin"))

    try:
        with conectar_banco() as db:
            db.execute("BEGIN IMMEDIATE")

            compra = db.execute("""
                SELECT *
                FROM compras_loja
                WHERE id = ?
            """, (compra_id,)).fetchone()

            if not compra:
                db.rollback()
                abort(404)

            if compra["status"] != "pendente":
                db.rollback()
                flash(
                    "Esse pedido já foi decidido e não pode ser processado novamente.",
                    "warning",
                )
                return redirect(
                    url_for("service_page", slug="loja-ebcoin")
                )

            if decisao == "aprovar":
                db.execute("""
                    UPDATE compras_loja
                    SET status = 'aprovado',
                        atualizado_em = ?,
                        decidido_por = ?,
                        decidido_em = ?
                    WHERE id = ? AND status = 'pendente'
                """, (agora, admin_id, agora, compra_id))

                db.commit()

                texto_aprovado = (
                    f"Seu pedido #{compra_id} ({compra['produto_nome']}) "
                    "foi aprovado pela administração."
                )
                if cargo_entregue:
                    texto_aprovado += f" Você foi promovido para {cargo_entregue}."
                notificar(
                    str(compra["user_id"]), "pedido", "Pedido aprovado",
                    texto_aprovado,
                )

                if cargo_entregue:
                    flash(
                        f"Pedido aprovado e promoção aplicada: {cargo_entregue}.",
                        "success",
                    )
                else:
                    flash(
                        "Pedido aprovado. Confirme que a entrega foi realizada "
                        "conforme combinado com o usuário.",
                        "success",
                    )

            else:
                # A recusa e o reembolso são feitos na mesma transação.
                # O estado pendente impede um segundo reembolso.
                user_id = str(compra["user_id"])
                preco = int(compra["preco"])

                db.execute("""
                    INSERT INTO saldos_ebcoin
                        (user_id, saldo, atualizado_em)
                    VALUES (?, ?, ?)
                    ON CONFLICT(user_id) DO UPDATE SET
                        saldo = saldos_ebcoin.saldo + excluded.saldo,
                        atualizado_em = excluded.atualizado_em
                """, (user_id, preco, agora))

                db.execute("""
                    INSERT INTO movimentacoes_ebcoin
                        (user_id, quantidade, motivo, criado_em)
                    VALUES (?, ?, ?, ?)
                """, (
                    user_id,
                    preco,
                    f"Reembolso da compra recusada #{compra_id}",
                    agora,
                ))

                db.execute("""
                    UPDATE compras_loja
                    SET status = 'recusado',
                        atualizado_em = ?,
                        decidido_por = ?,
                        decidido_em = ?,
                        reembolso_feito = 1
                    WHERE id = ? AND status = 'pendente'
                """, (agora, admin_id, agora, compra_id))

                db.commit()

                notificar(
                    user_id, "reembolso", "Pedido recusado",
                    f"O pedido #{compra_id} ({compra['produto_nome']}) foi "
                    f"recusado e {preco} EBCOIN foram devolvidos.",
                )
                flash(
                    "Pedido recusado e EBCOIN devolvido ao saldo do usuário.",
                    "success",
                )

    except sqlite3.Error:
        app.logger.exception("Falha ao decidir pedido da loja.")
        flash(
            "Não foi possível processar a decisão. Verifique o pedido e o saldo.",
            "error",
        )

    return redirect(url_for("service_page", slug="loja-ebcoin"))


@app.route("/servico/loja-ebcoin/admin/rank-maximo", methods=["POST"])
@somente_admin
def salvar_rank_maximo():
    validar_csrf()

    destino = url_for("service_page", slug="loja-ebcoin")
    texto = request.form.get("rank_maximo", "").strip()

    try:
        with conectar_banco() as db:
            if not texto:
                db.execute("""
                    DELETE FROM configuracoes_loja
                    WHERE chave = 'rank_maximo_automatico'
                """)
                flash(
                    "Limite removido: todas as promoções passam a ir para "
                    "análise da administração.",
                    "success",
                )
                return redirect(destino)

            try:
                rank = int(texto)
                if rank < 0 or rank > 255:
                    raise ValueError
            except ValueError:
                flash("O rank deve ser um número de 0 a 255.", "warning")
                return redirect(destino)

            db.execute("""
                INSERT INTO configuracoes_loja (chave, valor)
                VALUES ('rank_maximo_automatico', ?)
                ON CONFLICT(chave) DO UPDATE SET valor = excluded.valor
            """, (str(rank),))

        flash("Limite da entrega automática salvo.", "success")

    except sqlite3.Error:
        app.logger.exception("Falha ao salvar limite da entrega automática.")
        flash("Não foi possível salvar o limite.", "error")

    return redirect(destino)


@app.route("/notificacoes")
def api_notificacoes():
    user = session.get("user")
    if not user or not user.get("id"):
        return jsonify({"erro": "Entre para ver suas notificações."}), 401

    with conectar_banco() as db:
        linhas = db.execute("""
            SELECT id, tipo, titulo, mensagem, lida, criado_em
            FROM notificacoes
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT 30
        """, (str(user["id"]),)).fetchall()

    itens = [
        {
            "id": linha["id"],
            "tipo": linha["tipo"],
            "titulo": linha["titulo"],
            "mensagem": linha["mensagem"],
            "lida": bool(linha["lida"]),
            "data": filtro_data_br(linha["criado_em"]),
        }
        for linha in linhas
    ]

    return jsonify({
        "nao_lidas": sum(1 for item in itens if not item["lida"]),
        "itens": itens,
    })


@app.route("/notificacoes/marcar-lidas", methods=["POST"])
def marcar_notificacoes_lidas():
    user = session.get("user")
    if not user or not user.get("id"):
        return jsonify({"erro": "Entre para continuar."}), 401

    validar_csrf()

    with conectar_banco() as db:
        db.execute(
            "UPDATE notificacoes SET lida = 1 WHERE user_id = ? AND lida = 0",
            (str(user["id"]),),
        )

    return jsonify({"ok": True})


# -------------------- ADMINISTRAÇÃO DE CÓDIGOS --------------------

@app.route("/servico/resgatar-codigo/criar", methods=["POST"])
@somente_admin
def criar_codigo():
    validar_csrf()

    codigo = request.form.get("codigo", "").strip()
    tipo = request.form.get("tipo", "").strip().lower()
    valor_texto = request.form.get("valor", "0").strip()
    limite_texto = request.form.get("limite_resgates", "").strip()
    duracao_texto = request.form.get("duracao_dias", "").strip()

    destino = url_for("service_page", slug="resgatar-codigo")

    if not codigo or len(codigo) > 64:
        flash("O código precisa ter entre 1 e 64 caracteres.", "warning")
        return redirect(destino)

    if tipo not in ("promocao", "moeda"):
        flash("Selecione um tipo de código válido.", "warning")
        return redirect(destino)

    try:
        valor = int(valor_texto or "0")
        if tipo == "moeda" and valor <= 0:
            raise ValueError
        if tipo == "promocao":
            valor = 0
    except ValueError:
        flash(
            "A quantidade de EBCOIN precisa ser um número positivo.",
            "warning",
        )
        return redirect(destino)

    try:
        limite = int(limite_texto) if limite_texto else None
        if limite is not None and limite <= 0:
            raise ValueError
    except ValueError:
        flash(
            "O limite precisa ser positivo ou ficar em branco para ilimitado.",
            "warning",
        )
        return redirect(destino)

    try:
        dias = int(duracao_texto) if duracao_texto else 0
        if dias < 0 or dias > 3650:
            raise ValueError
    except ValueError:
        flash("A duração deve ser de 0 a 3650 dias.", "warning")
        return redirect(destino)

    expira_em = None
    if dias:
        expira_em = (
            datetime.now(timezone.utc) + timedelta(days=dias)
        ).isoformat(timespec="seconds")

    try:
        with conectar_banco() as db:
            db.execute("""
                INSERT INTO codigos_resgate
                    (codigo, tipo, valor, limite_resgates, resgates_feitos,
                     criado_por, criado_em, expira_em, ativo)
                VALUES (?, ?, ?, ?, 0, ?, ?, ?, 1)
            """, (
                codigo,
                tipo,
                valor,
                limite,
                str(session["user"]["id"]),
                agora_iso(),
                expira_em,
            ))
    except sqlite3.IntegrityError:
        flash("Esse código já existe. Escolha outro texto.", "warning")
        return redirect(destino)

    flash("Código criado com sucesso!", "success")
    return redirect(destino)


@app.route(
    "/servico/resgatar-codigo/desativar/<int:codigo_id>",
    methods=["POST"],
)
@somente_admin
def desativar_codigo(codigo_id):
    validar_csrf()

    with conectar_banco() as db:
        result = db.execute(
            "UPDATE codigos_resgate SET ativo = 0 WHERE id = ?",
            (codigo_id,),
        )

    if result.rowcount == 0:
        abort(404)

    flash("Código desativado.", "success")
    return redirect(url_for("service_page", slug="resgatar-codigo"))


@app.route("/servico/resgatar-codigo/resgatar", methods=["POST"])
def resgatar_codigo():
    user = session.get("user")

    if not user:
        flash("Entre com sua conta Roblox primeiro.", "warning")
        return redirect(url_for("index"))

    validar_csrf()

    codigo_digitado = request.form.get("codigo", "").strip()
    user_id = str(user.get("id", "")).strip()
    username = str(user.get("name", "Usuário Roblox"))

    destino = url_for("service_page", slug="resgatar-codigo")

    if not codigo_digitado or len(codigo_digitado) > 64:
        flash("Digite um código válido.", "warning")
        return redirect(destino)

    with conectar_banco() as db:
        db.execute("BEGIN IMMEDIATE")

        code = db.execute("""
            SELECT *
            FROM codigos_resgate
            WHERE codigo = ? COLLATE NOCASE AND ativo = 1
        """, (codigo_digitado,)).fetchone()

        if not code:
            db.rollback()
            flash("Código inválido ou desativado.", "error")
            return redirect(destino)

        if code["expira_em"]:
            try:
                expiration = datetime.fromisoformat(code["expira_em"])

                if expiration <= datetime.now().astimezone():
                    db.rollback()
                    flash("Esse código expirou.", "warning")
                    return redirect(destino)

            except ValueError:
                db.rollback()
                flash("A validade desse código está inválida.", "error")
                return redirect(destino)

        if (
            code["limite_resgates"] is not None
            and code["resgates_feitos"] >= code["limite_resgates"]
        ):
            db.rollback()
            flash(
                "Esse código já atingiu o limite de resgates.",
                "warning",
            )
            return redirect(destino)

        existing = db.execute("""
            SELECT id, status
            FROM resgates_codigos
            WHERE codigo_id = ? AND user_id = ?
        """, (code["id"], user_id)).fetchone()

        if existing and existing["status"] != "falhou":
            db.rollback()
            flash("Você já tentou resgatar esse código.", "warning")
            return redirect(destino)

        if existing:
            # Tentativa anterior falhou: reaproveita a mesma linha
            # (há UNIQUE(codigo_id, user_id)) e tenta de novo.
            db.execute("""
                UPDATE resgates_codigos
                SET status = 'pendente', mensagem = NULL,
                    username = ?, resgatado_em = ?
                WHERE id = ?
            """, (username, agora_iso(), existing["id"]))
            redemption_id = existing["id"]
        else:
            redemption = db.execute("""
                INSERT INTO resgates_codigos
                    (codigo_id, user_id, username, resgatado_em,
                     status, mensagem)
                VALUES (?, ?, ?, ?, 'pendente', NULL)
            """, (code["id"], user_id, username, agora_iso()))
            redemption_id = redemption.lastrowid

        db.execute("""
            UPDATE codigos_resgate
            SET resgates_feitos = resgates_feitos + 1
            WHERE id = ?
        """, (code["id"],))

        code_id = code["id"]
        code_type = code["tipo"]
        coin_value = code["valor"]
        db.commit()

    if code_type == "moeda":
        try:
            with conectar_banco() as db:
                db.execute("BEGIN IMMEDIATE")

                db.execute("""
                    INSERT INTO saldos_ebcoin
                        (user_id, saldo, atualizado_em)
                    VALUES (?, ?, ?)
                    ON CONFLICT(user_id) DO UPDATE SET
                        saldo = saldos_ebcoin.saldo + excluded.saldo,
                        atualizado_em = excluded.atualizado_em
                """, (user_id, coin_value, agora_iso()))

                db.execute("""
                    INSERT INTO movimentacoes_ebcoin
                        (user_id, quantidade, motivo, criado_em)
                    VALUES (?, ?, ?, ?)
                """, (
                    user_id,
                    coin_value,
                    f"Resgate de código #{code_id}",
                    agora_iso(),
                ))

                db.execute("""
                    UPDATE resgates_codigos
                    SET status = 'concluido', mensagem = ?
                    WHERE id = ?
                """, (f"+{coin_value} EBCOIN", redemption_id))

                db.commit()

            notificar(
                user_id, "moeda", "Código resgatado",
                f"Você recebeu {coin_value} EBCOIN.",
            )
            flash(
                f"Código resgatado! Você recebeu {coin_value} EBCOIN.",
                "success",
            )

        except sqlite3.Error:
            app.logger.exception("Falha ao creditar EBCOIN.")
            flash(
                "Não foi possível creditar as moedas. Avise o administrador.",
                "error",
            )

    else:
        try:
            role_name, avisos = promover_membro_uma_patente(user_id)

            mensagem = f"Promovido para {role_name}"
            if avisos:
                mensagem += (
                    " (patente antiga não removida: "
                    + ", ".join(avisos) + ")"
                )

            with conectar_banco() as db:
                db.execute("""
                    UPDATE resgates_codigos
                    SET status = 'concluido', mensagem = ?
                    WHERE id = ?
                """, (mensagem, redemption_id))

            notificar(
                user_id, "promocao", "Promoção recebida",
                f"Você foi promovido para {role_name} com um código.",
            )

            if avisos:
                flash(
                    f"Promovido para {role_name}, mas não foi possível "
                    "remover a patente antiga. Avise o administrador.",
                    "warning",
                )
            else:
                flash(
                    f"Promoção realizada! Nova patente: {role_name}.",
                    "success",
                )

        except Exception as exc:  # noqa: BLE001 - nunca deixar 'pendente'
            app.logger.exception("Falha na promoção por código: %s", exc)

            with conectar_banco() as db:
                db.execute("""
                    UPDATE resgates_codigos
                    SET status = 'falhou',
                        mensagem = ?
                    WHERE id = ?
                """, ("A promoção não foi concluída.", redemption_id))

                db.execute("""
                    UPDATE codigos_resgate
                    SET resgates_feitos = CASE
                        WHEN resgates_feitos > 0 THEN resgates_feitos - 1
                        ELSE 0
                    END
                    WHERE id = ?
                """, (code_id,))

            flash(
                "Não foi possível promover. Confira se você está no grupo "
                "e se a chave Open Cloud tem as permissões necessárias.",
                "error",
            )

    return redirect(destino)


# -------------------- LOGIN E PÁGINAS --------------------

@app.route("/dev-login")
def dev_login():
    if (
        not DEV_MODE
        or not app.debug
        or os.environ.get("FLASK_DEBUG") != "1"
    ):
        abort(404)

    test_id = ADMIN_ROBLOX_ID or "123456789"

    session.clear()
    session["user"] = {
        "id": test_id,
        "name": "Cyber_Teste",
        "picture": "",
    }
    session["csrf_token"] = secrets.token_urlsafe(32)
    session.permanent = True

    return redirect(url_for("index"))


@app.route("/")
def index():
    user = session.get("user")

    preferences = (
        obter_preferencias(user["id"])
        if user and user.get("id")
        else {
            "theme": "dark",
            "show_avatar": 1,
            "show_rank": 1,
            "show_roblox_id": 1,
        }
    )

    return render_template(
        "index.html",
        user=user,
        services=get_services(),
        preferences=preferences,
        csrf_token=session.get("csrf_token", ""),
    )


@app.route("/login/roblox")
def login_roblox():
    if not CLIENT_ID or not CLIENT_SECRET:
        flash(
            "O login Roblox ainda não foi configurado. "
            "Configure as credenciais OAuth.",
            "warning",
        )
        return redirect(url_for("index"))

    state = secrets.token_urlsafe(32)
    session["oauth_state"] = state
    session.permanent = True

    params = {
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": "openid profile",
        "state": state,
    }

    return redirect(f"{ROBLOX_AUTHORIZE_URL}?{urlencode(params)}")


@app.route("/auth/roblox/callback")
def roblox_callback():
    if request.args.get("error"):
        flash("O login foi cancelado ou não foi autorizado.", "warning")
        return redirect(url_for("index"))

    received_state = request.args.get("state", "")
    expected_state = session.pop("oauth_state", None)

    if (
        not expected_state
        or not secrets.compare_digest(received_state, expected_state)
    ):
        flash("Não foi possível validar o login. Tente novamente.", "error")
        return redirect(url_for("index"))

    code = request.args.get("code")

    if not code:
        flash(
            "O Roblox não retornou o código de autorização.",
            "error",
        )
        return redirect(url_for("index"))

    try:
        token_response = requests.post(
            ROBLOX_TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "redirect_uri": REDIRECT_URI,
            },
            headers={
                "Content-Type": "application/x-www-form-urlencoded"
            },
            timeout=15,
        )
        token_response.raise_for_status()
        access_token = token_response.json()["access_token"]

        profile_response = requests.get(
            ROBLOX_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=15,
        )
        profile_response.raise_for_status()

        profile = profile_response.json()
        user_id = str(profile.get("sub", ""))

        if not user_id:
            raise ValueError(
                "O perfil não retornou um identificador."
            )

        session.clear()
        session["user"] = {
            "id": user_id,
            "name": (
                profile.get("preferred_username")
                or profile.get("name")
                or "Usuário Roblox"
            ),
            "picture": profile.get("picture", ""),
        }
        session["csrf_token"] = secrets.token_urlsafe(32)
        session.permanent = True

        return redirect(url_for("index"))

    except (requests.RequestException, ValueError, KeyError) as exc:
        resposta = getattr(exc, "response", None)
        detalhe = ""
        if resposta is not None:
            detalhe = (
                f" | HTTP {resposta.status_code} | "
                f"{(resposta.text or '')[:400]}"
            )
        app.logger.error(
            "Falha na autenticação Roblox: %s: %s%s | redirect_uri=%s",
            type(exc).__name__, exc, detalhe, REDIRECT_URI,
        )
        flash(
            "Não foi possível concluir o login. "
            "Confira a configuração OAuth.",
            "error",
        )
        return redirect(url_for("index"))


@app.route("/logout", methods=["POST"])
def logout():
    validar_csrf()
    session.clear()
    return redirect(url_for("index"))


@app.route("/servico/<slug>")
def service_page(slug):
    user = session.get("user")

    if not user:
        flash(
            "Entre com sua conta Roblox para acessar os serviços.",
            "warning",
        )
        return redirect(url_for("index"))

    service = next(
        (item for item in get_services() if item["slug"] == slug),
        None,
    )

    if service is None:
        return render_template(
            "service.html",
            user=user,
            service=None,
        ), 404

    if service["slug"] == "canal-oficial":
        return redirect(url_for("canal_oficial"))

    if service["slug"] == "configuracoes":
        return redirect(url_for("configuracoes"))

    if service["slug"] == "resgatar-codigo":
        return render_template(
            "service.html",
            user=user,
            service=service,
            preferences=obter_preferencias(user["id"]),
            csrf_token=session.get("csrf_token", ""),
            is_admin=is_admin(user),
            codes=listar_codigos() if is_admin(user) else [],
            balance=obter_saldo_ebcoin(user["id"]),
            redemption_history=listar_resgates_usuario(user["id"]),
        )

    if service["slug"] == "loja-ebcoin":
        administrador = is_admin(user)

        return render_template(
            "service.html",
            user=user,
            service=service,
            preferences=obter_preferencias(user["id"]),
            csrf_token=session.get("csrf_token", ""),
            is_admin=administrador,
            balance=obter_saldo_ebcoin(user["id"]),
            products=listar_produtos(ativos_apenas=True),
            admin_products=(
                listar_produtos(ativos_apenas=False)
                if administrador else []
            ),
            purchase_history=listar_compras_usuario(user["id"]),
            admin_orders=(
                listar_compras_admin()
                if administrador else []
            ),
            cargos_grupo=(
                listar_cargos_para_admin()
                if administrador else []
            ),
            rank_maximo=(
                obter_rank_maximo_automatico()
                if administrador else None
            ),
        )

    return render_template(
        "service.html",
        user=user,
        service=service,
        preferences=obter_preferencias(user["id"]),
        csrf_token=session.get("csrf_token", ""),
    )


@app.route("/configuracoes", methods=["GET", "POST"])
def configuracoes():
    user = session.get("user")

    if not user:
        flash(
            "Entre com sua conta Roblox para acessar as configurações.",
            "warning",
        )
        return redirect(url_for("index"))

    preferences = obter_preferencias(user["id"])

    if request.method == "POST":
        validar_csrf()

        if request.form.get("refresh_profile") == "1":
            profile = obter_perfil(user, force_refresh=True)

            if profile.get("warning"):
                flash(profile["warning"], "warning")
            else:
                flash("Perfil Roblox atualizado.", "success")

            return render_template(
                "configuracoes.html",
                user=user,
                preferences=obter_preferencias(user["id"]),
                profile=profile,
            )

        salvar_preferencias(user["id"], request.form)
        flash("Configurações salvas com sucesso!", "success")
        return redirect(url_for("configuracoes"))

    return render_template(
        "configuracoes.html",
        user=user,
        preferences=preferences,
        profile=obter_perfil(user),
    )


@app.route("/canal-oficial")
def canal_oficial():
    user = session.get("user")

    if not user:
        flash(
            "Entre com sua conta Roblox para acessar os serviços.",
            "warning",
        )
        return redirect(url_for("index"))

    with conectar_banco() as db:
        comunicados = db.execute("""
            SELECT *
            FROM comunicados
            ORDER BY fixado DESC, criado_em DESC, id DESC
        """).fetchall()

    return render_template(
        "canal_oficial.html",
        user=user,
        comunicados=comunicados,
        is_admin=is_admin(user),
        preferences=obter_preferencias(user["id"]),
        csrf_token=session.get("csrf_token", ""),
    )


@app.route("/canal-oficial/publicar", methods=["POST"])
@somente_admin
def publicar_comunicado():
    validar_csrf()

    titulo = request.form.get("titulo", "").strip()
    conteudo = request.form.get("conteudo", "").strip()
    fixado = 1 if request.form.get("fixado") == "on" else 0
    user = session["user"]

    if not titulo or not conteudo:
        flash("Preencha o título e o conteúdo.", "warning")
        return redirect(url_for("canal_oficial"))

    if len(titulo) > 120 or len(conteudo) > 10000:
        flash(
            "O título ou conteúdo ultrapassou o limite permitido.",
            "warning",
        )
        return redirect(url_for("canal_oficial"))

    with conectar_banco() as db:
        db.execute("""
            INSERT INTO comunicados
                (titulo, conteudo, autor_id, autor_nome,
                 fixado, criado_em)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            titulo,
            conteudo,
            str(user["id"]),
            user["name"],
            fixado,
            agora_iso(),
        ))

    flash("Comunicado publicado!", "success")
    return redirect(url_for("canal_oficial"))


@app.route(
    "/canal-oficial/<int:comunicado_id>/editar",
    methods=["POST"],
)
@somente_admin
def editar_comunicado(comunicado_id):
    validar_csrf()

    titulo = request.form.get("titulo", "").strip()
    conteudo = request.form.get("conteudo", "").strip()
    fixado = 1 if request.form.get("fixado") == "on" else 0

    if (
        not titulo
        or not conteudo
        or len(titulo) > 120
        or len(conteudo) > 10000
    ):
        flash(
            "Confira o título e o conteúdo antes de salvar.",
            "warning",
        )
        return redirect(url_for("canal_oficial"))

    with conectar_banco() as db:
        result = db.execute("""
            UPDATE comunicados
            SET titulo = ?, conteudo = ?, fixado = ?, atualizado_em = ?
            WHERE id = ?
        """, (
            titulo,
            conteudo,
            fixado,
            agora_iso(),
            comunicado_id,
        ))

        if result.rowcount == 0:
            abort(404)

    flash("Comunicado atualizado!", "success")
    return redirect(url_for("canal_oficial"))


@app.route(
    "/canal-oficial/<int:comunicado_id>/excluir",
    methods=["POST"],
)
@somente_admin
def excluir_comunicado(comunicado_id):
    validar_csrf()

    with conectar_banco() as db:
        result = db.execute(
            "DELETE FROM comunicados WHERE id = ?",
            (comunicado_id,),
        )

        if result.rowcount == 0:
            abort(404)

    flash("Comunicado excluído.", "success")
    return redirect(url_for("canal_oficial"))


if __name__ == "__main__":
    app.run(
        debug=os.environ.get("FLASK_DEBUG") == "1",
        host="127.0.0.1",
        port=5000,
    )