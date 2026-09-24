"""
TypCore WhatsApp Bot
Webhook FastAPI + Evolution API
"""
import os
import re
import json
import httpx
from datetime import datetime
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

import ia  # camada de IA (provedor trocável; desligada sem chave)

# ── CONFIGURAÇÃO ─────────────────────────────────────────────
def _normalizar_url(bruto: str, padrao: str) -> str:
    """Tolera valor mal formatado na variável de ambiente.

    Já quebrou em produção com "Request URL is missing an 'http://' or
    'https://' protocol": basta um espaço, uma quebra de linha ou a falta
    do esquema para o httpx recusar. Em vez de derrubar o atendimento por
    isso, normaliza aqui.
    """
    u = (bruto or "").strip().strip('"').strip("'")
    u = u.replace("\n", "").replace("\r", "").replace(" ", "")
    if not u:
        return padrao
    if not u.startswith(("http://", "https://")):
        u = "https://" + u
    return u.rstrip("/")


# Endereço da Evolution API deste projeto. Fica aqui como PADRÃO porque a
# variável EVOLUTION_URL parou de chegar ao container em 23/09 (o /versao
# mostrava "localhost:8080", que é o padrão antigo) e o bot ficou mudo sem
# nenhum sintoma visível. "localhost" nunca serve em produção — este
# endereço serve. A variável de ambiente continua tendo prioridade: se ela
# existir e for válida, é ela que vale.
EVOLUTION_URL_PADRAO = "https://evolution-api-production-8c70.up.railway.app"

EVOLUTION_URL    = _normalizar_url(os.getenv("EVOLUTION_URL"), EVOLUTION_URL_PADRAO)
EVOLUTION_APIKEY = os.getenv("EVOLUTION_APIKEY", "typcore-evolution-key")
INSTANCE_NAME    = os.getenv("INSTANCE_NAME", "typcore")
NUMERO_NOTIF_PADRAO = "5511970667575"

# CUIDADO COM O DEFAULT DO os.getenv: ele so vale quando a variavel NAO
# EXISTE. Uma variavel que existe com valor EM BRANCO devolve "" e passa
# por cima do padrao. Foi exatamente isso que aconteceu em 24/09: o
# NUMERO_NOTIF estava criado e vazio no Railway, o bot mandava
# {"number": ""} e a Evolution respondia 400 com jid "@s.whatsapp.net".
# Toda notificacao falhou por isso, desde sempre.
_notif_bruto = os.getenv("NUMERO_NOTIF") or ""
NUMERO_NOTIF = "".join(filter(str.isdigit, _notif_bruto)) or NUMERO_NOTIF_PADRAO

# Numero brasileiro valido tem 12 ou 13 digitos (55 + DDD + 8 ou 9).
# Fora disso o valor esta errado — melhor cair no padrao do que falhar
# em silencio justamente na hora em que o cliente precisa de atendimento.
if not (12 <= len(NUMERO_NOTIF) <= 13):
    print(f"AVISO: NUMERO_NOTIF={_notif_bruto!r} nao parece numero valido "
          f"({len(NUMERO_NOTIF)} digitos). Usando o padrao do codigo.")
    NUMERO_NOTIF = NUMERO_NOTIF_PADRAO

if not _notif_bruto.strip():
    print(f"AVISO: NUMERO_NOTIF vazio ou ausente no ambiente. "
          f"Usando o padrao do codigo: {NUMERO_NOTIF}")
elif "".join(filter(str.isdigit, _notif_bruto)) != _notif_bruto:
    print(f"AVISO: NUMERO_NOTIF tinha caracteres nao numericos "
          f"({_notif_bruto!r}); usando {NUMERO_NOTIF!r}.")

app = FastAPI(title="TypCore WhatsApp Bot")

# ── ESTADO DAS CONVERSAS (em memória) ────────────────────────
# { "5511999999999": { "estado": "menu", "nome": "João", "ultima": datetime } }
conversas: dict = {}

TIMEOUT_MINUTOS = 30  # reseta conversa após inatividade


# ── CLIENTE EVOLUTION API ────────────────────────────────────

async def enviar_detalhado(numero: str, texto: str) -> dict:
    """Envia e devolve o resultado CRU: status HTTP e corpo da resposta.

    Existe porque o envio falhava em silencio: sem ver o corpo da resposta
    da Evolution nao da para saber se o problema e o numero, a instancia
    desconectada ou a apikey.
    """
    url = f"{EVOLUTION_URL}/message/sendText/{INSTANCE_NAME}"
    payload = {"number": numero, "text": texto}
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            r = await client.post(url, json=payload,
                                  headers={"apikey": EVOLUTION_APIKEY})
        except Exception as e:
            print(f"ERRO ao enviar para {numero}: {e}")
            return {"ok": False, "status": None,
                    "detalhe": f"{type(e).__name__}: {e}"}
    if r.status_code >= 400:
        print(f"ERRO {r.status_code} ao enviar para {numero}: {r.text[:300]}")
        return {"ok": False, "status": r.status_code, "detalhe": r.text[:500]}
    try:
        corpo = r.json()
    except Exception:
        corpo = r.text[:300]
    return {"ok": True, "status": r.status_code, "detalhe": corpo}


async def enviar_mensagem(numero: str, texto: str):
    """Envia mensagem de texto via Evolution API."""
    res = await enviar_detalhado(numero, texto)
    return res["detalhe"] if res["ok"] else None


# Ultima tentativa de notificacao, exposta em /versao. Sem isto uma falha
# aqui e invisivel: o cliente ouve "vou encaminhar", o envio falha, e nao
# sobra rastro em lugar nenhum.
ultima_notificacao = {"quando": None, "para": None, "ok": None,
                      "auto_teste": None, "erro": None}

# Numero do WhatsApp conectado na instancia, descoberto no 1o webhook.
instancia_info = {"numero": None}


def conflito_numero() -> bool:
    """NUMERO_NOTIF aponta para o proprio bot?

    Se sim, a notificacao e enviada para o bot ele mesmo: a Evolution
    aceita, o /versao mostra ok=true, e o atendente nunca recebe nada.
    E o unico modo de falha que parece sucesso — por isso vai explicito.
    """
    n = instancia_info["numero"]
    return bool(n) and n == NUMERO_NOTIF


async def notificar_atendente(numero_cliente: str, nome: str, ultima_msg: str):
    """Notifica o atendente humano quando cliente pede suporte."""
    # Teste feito do PROPRIO numero de notificacao: a notificacao cai na
    # mesma conversa em que o bot responde, misturada com as respostas
    # dele. Nao e erro, mas confunde na hora de conferir — por isso vai
    # rotulada e fica registrada em /versao.
    auto_teste = numero_cliente == NUMERO_NOTIF
    cabecalho = ("🔧 *TESTE — você escreveu do próprio número de "
                 "notificação*" if auto_teste
                 else "🔔 *Novo cliente aguardando atendimento*")
    texto = (
        f"{cabecalho}\n\n"
        f"👤 Nome: {nome or 'Não informado'}\n"
        f"📱 Número: {numero_cliente}\n"
        f"💬 Última mensagem: _{ultima_msg}_\n\n"
        f"Acesse o WhatsApp para atender."
    )
    res = await enviar_detalhado(NUMERO_NOTIF, texto)
    ultima_notificacao.update({
        "quando": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "para":   NUMERO_NOTIF,
        "ok":     res["ok"],
        "auto_teste": auto_teste,
        "erro":   None if res["ok"] else f"HTTP {res['status']}: {res['detalhe']}",
    })
    if not res["ok"]:
        print(f"FALHA ao notificar atendente em {NUMERO_NOTIF!r} "
              f"(cliente {numero_cliente}): {res['detalhe']}")
    elif conflito_numero():
        print(f"ATENCAO: NUMERO_NOTIF ({NUMERO_NOTIF}) e o MESMO numero do "
              f"bot. A notificacao foi enviada, mas para o proprio bot — "
              f"troque NUMERO_NOTIF no Railway pelo seu celular.")
    else:
        print(f"[notif] enviada para {NUMERO_NOTIF} "
              f"(cliente {numero_cliente})")


# ── TEXTOS DO BOT ────────────────────────────

# REGISTRO: o cliente esta avaliando pagar uma mensalidade. Emoji numerado,
# exclamacao e "ótimo!/perfeito!" fazem o atendimento parecer amador e
# derrubam o valor percebido do software. Tom: educado, curto, sem euforia.
# Nunca nomear pessoas nem revelar o tamanho da empresa.

HORARIO = "Atendimento: seg a sex, 8h às 18h · sáb, 9h às 13h"

MENU_PRINCIPAL = """\
*TypCore* — Sistemas de gestão

Olá. Sou o assistente virtual da TypCore.

Escolha uma opção:

*1* · Suporte técnico
*2* · Ativar licença
*3* · Planos e preços
*4* · Falar com um atendente

_Responda com o número da opção._"""

MENU_SUPORTE = """\
*Suporte técnico*

Qual é o assunto?

*1* · O sistema não abre ou travou
*2* · Erro de conexão com o banco de dados
*3* · Problema na ativação da licença
*4* · Dúvida sobre funcionalidades
*5* · Outro assunto

*0* · Voltar ao menu"""

# ATENCAO: estes valores tem de bater com window.TYPCORE.tiers do site
# (typcore.com.br). Mexeu no preco la, atualize AQUI tambem — hoje sao
# duas fontes separadas. Conferido em 23/09/2026.
MENU_PRECOS = """\
*Planos TypCore*

O preço depende da nota fiscal que o seu negócio emite.

*Essencial* — sem nota fiscal
R$ 89/mês · R$ 79 no trimestral · R$ 69 no anual
_Clínicas, salões, petshops, pilates, fisioterapia_

*Fiscal* — com NFC-e ou NFS-e
R$ 129/mês · R$ 119 no trimestral · R$ 99 no anual
_Mercadinhos, lojas, mecânicas, celulares, informática_

*Fiscal Completo* — NFC-e e NFS-e
R$ 199/mês · R$ 189 no trimestral · R$ 179 no anual
_Para quem emite os dois tipos de nota_

Todos os planos incluem até 3 computadores, suporte no WhatsApp,
atualizações e backup. Sem fidelidade e sem taxa de implantação.

*15 dias de teste, sem cartão.*

*Disponíveis:* Odonto, Estética, Fisioterapia, Mercadinho,
Informática e Celulares.

*Em desenvolvimento:* Mecânica (Autos e Motos), Lojas, Salão,
Veterinária, Petshop e Pilates.

*1* · Quero contratar
*2* · Tenho dúvidas sobre os planos
*3* · Não sei qual plano é o meu
*0* · Voltar ao menu"""

RESPOSTAS_SUPORTE = {
    "1": """\
*O sistema não abre ou travou*

Tente nesta ordem:

1. Feche o sistema por completo e aguarde 30 segundos.
2. Abra novamente como administrador
   (botão direito → Executar como administrador).
3. Se continuar, confirme se o serviço *MariaDB* está ativo:
   Windows + R → services.msc → MariaDB → Iniciar.

Isso resolveu?

*1* · Resolvido
*2* · Continua o problema
*0* · Voltar ao menu""",

    "2": """\
*Erro de conexão com o banco de dados*

Confirme os três pontos:

1. O serviço *MariaDB* está em execução?
   Windows + R → services.msc → MariaDB → Iniciar.
2. O arquivo *config.ini* existe na pasta do sistema?
   Se não existir, abra o sistema e configure a conexão.
3. Se persistir, a reinstalação preserva os seus dados.

Isso resolveu?

*1* · Resolvido
*2* · Continua o problema
*0* · Voltar ao menu""",

    "3": """\
*Problema na ativação da licença*

Confirme:

1. O serial está no formato *XXXX-XXXX-XXXX-XXXX*.
2. O computador está conectado à internet.
3. A licença permite até *3 computadores* — verifique se o limite
   já foi usado em outras máquinas.
4. O serial consta no e-mail de confirmação da assinatura.

Isso resolveu?

*1* · Resolvido
*2* · Continua o problema
*0* · Voltar ao menu""",

    # Os links abaixo foram conferidos em 23/09/2026 (todos respondem 200).
    # O caminho antigo /manuais/ estava dando 404 — o correto e /downloads/.
    "4": """\
*Manuais dos sistemas*

Odonto
typcore.com.br/downloads/Manual_TypCore_Odonto.pdf

Estética
typcore.com.br/downloads/Manual_TypCore_Estetica.pdf

Fisioterapia
typcore.com.br/downloads/Manual_TypCore_Fisioterapia.pdf

Mercadinho
typcore.com.br/downloads/Manual_TypCore_Mercadinho.pdf

Informática
typcore.com.br/downloads/Manual_TypCore_Informatica.pdf

Celulares
typcore.com.br/downloads/Manual_TypCore_Celulares.pdf

Ficou alguma dúvida?

*1* · Quero falar com um atendente
*0* · Voltar ao menu""",
}


# ── MÁQUINA DE ESTADOS ───────────────────────────────────────

# Sinais de que a mensagem é SUPORTE de quem já é cliente. Essas conversas
# não passam pela IA de propósito: é onde a pessoa descreve o negócio dela
# em detalhe, e onde resposta inventada custa mais caro. Verificar pelo
# TEXTO (e não pelo estado) é essencial — com IA ativa o cliente escreve
# "meu sistema não abre" direto, sem passar pelo menu.
SINAIS_SUPORTE = (
    "nao abre", "não abre", "nao funciona", "não funciona", "travou", "travando",
    "parou de funcionar", "deu erro", "erro ", "bug", "falha", "nao consigo",
    "não consigo", "nao entra", "não entra", "fechou sozinho", "sumiu",
    "perdi", "backup", "banco de dados", "mariadb", "serial", "licenca",
    "licença", "ativar", "ativacao", "ativação", "nao imprime", "não imprime",
    "nota nao sai", "nota não sai", "rejeitada", "sefaz",
    # Pedidos explicitos de suporte. Frases, nunca a palavra "suporte"
    # solta — "tem suporte incluso?" e pergunta de venda, nao de suporte.
    "preciso de suporte", "quero suporte", "queria suporte",
    "falar com o suporte", "falar com suporte", "chamar o suporte",
    "suporte tecnico", "suporte técnico", "assistencia tecnica",
    "abrir um chamado", "abrir chamado",
)


# "Gostaria de suporte" passou direto pela lista de frases e foi parar na
# IA (visto em 24/09). Listar variacao por variacao nao escala: o cliente
# escreve "preciso", "queria", "to precisando", "necessito". A regra e
# verbo de necessidade + a palavra suporte/ajuda tecnica.
_PEDIDO_SUPORTE = re.compile(
    r"\b(preciso|precisava|precisando|quero|queria|gostaria|necessito|"
    r"poderia ter|pode me dar|me d[aá])\b[^.?!]{0,25}\b"
    r"(suporte|assist[eê]ncia|ajuda t[eé]cnica|atendimento t[eé]cnico)\b"
)


def parece_suporte(texto: str) -> bool:
    t = texto.lower()
    if any(s in t for s in SINAIS_SUPORTE):
        return True
    return bool(_PEDIDO_SUPORTE.search(t))


def get_conversa(numero: str) -> dict:
    agora = datetime.utcnow()
    if numero in conversas:
        ultima = conversas[numero].get("ultima", agora)
        diff   = (agora - ultima).total_seconds() / 60
        if diff > TIMEOUT_MINUTOS:
            del conversas[numero]

    if numero not in conversas:
        conversas[numero] = {
            "estado":    "inicio",
            "nome":      "",
            "ultima_msg": "",
            "ultima":    agora,
            # Histórico só para dar contexto à IA. Vive em memória e some
            # a cada deploy do Railway — aceitável porque a conversa
            # também expira em TIMEOUT_MINUTOS.
            "historico": [],
            # Marca que o aviso AUTOMATICO de suporte ja foi disparado nesta
            # conversa. Escalonamento EXPLICITO (cliente pede humano) sempre
            # notifica de novo: ali a informacao e nova.
            "avisado_suporte": False,
        }
    return conversas[numero]


async def processar_mensagem(numero: str, texto: str, nome_contato: str):
    conv  = get_conversa(numero)
    texto = texto.strip()
    conv["ultima"]    = datetime.utcnow()
    conv["ultima_msg"] = texto

    if not conv["nome"] and nome_contato:
        conv["nome"] = nome_contato.split()[0]  # primeiro nome

    estado = conv["estado"]

    # ── CAMINHO COM IA ─────────────────────────────────────────
    # Atende em texto livre quando: há IA configurada, o cliente NÃO está
    # num fluxo determinístico (suporte/ativação/aguardando humano) e ele
    # não digitou só um número de menu.
    #
    # Suporte técnico fica DE FORA de propósito: é onde o cliente descreve
    # o negócio dele em detalhe, e onde uma resposta inventada custa caro.
    so_numero = texto.isdigit() or texto.lower() in ("menu", "0")
    fluxo_fixo = estado in ("suporte", "ativacao", "aguardando_humano",
                            "suporte_resolvido", "precos")

    # Suporte detectado pelo texto: desvia para o fluxo determinístico
    # ANTES de qualquer coisa sair para o provedor de IA.
    if not so_numero and not fluxo_fixo and parece_suporte(texto):
        conv["estado"] = "suporte"
        # Avisa o atendente JA, sem esperar o cliente pedir humano. Quem
        # esta com o sistema parado muitas vezes abandona o menu numerado
        # e some — e ai ninguem fica sabendo que existiu o problema.
        # A tag diz que ele esta tentando se resolver sozinho, para nao
        # interromper quem esta a meio caminho.
        if not conv.get("avisado_suporte"):
            conv["avisado_suporte"] = True
            await notificar_atendente(
                numero, conv["nome"],
                texto + "  [suporte detectado - auto-atendimento em andamento]")
        await enviar_mensagem(numero, MENU_SUPORTE)
        return

    if ia.ia_ativa() and not so_numero and not fluxo_fixo:
        conv["historico"].append({"quem": "cliente", "texto": texto})
        conv["historico"] = conv["historico"][-12:]  # janela curta

        resposta, escalar = await ia.responder(conv["historico"])

        if resposta:
            conv["historico"].append({"quem": "bot", "texto": resposta})
            conv["estado"] = "ia"
            await enviar_mensagem(numero, resposta)
            if escalar:
                conv["estado"] = "aguardando_humano"
                await notificar_atendente(numero, conv["nome"], conv["ultima_msg"])
            return
        # IA indisponível (sobrecarga do provedor, timeout, cota).
        # NÃO devolver o menu: quem escreveu uma pergunta e recebe um menu
        # numerado acha que não foi entendido. Uma pessoa assume a conversa.
        conv["estado"] = "aguardando_humano"
        await notificar_atendente(
            numero, conv["nome"],
            conv["ultima_msg"] + ("" if escalar else "  [IA indisponível]"))
        await enviar_mensagem(numero, (
            "Vou encaminhar a sua mensagem para um atendente.\n\n"
            "A nossa equipe já foi notificada e responde aqui mesmo.\n\n"
            + HORARIO
        ))
        return

    # ── INICIO ──
    if estado == "inicio":
        conv["estado"] = "menu"
        await enviar_mensagem(numero, MENU_PRINCIPAL)
        return

    # ── MENU PRINCIPAL ──
    if estado == "menu":
        if texto == "1":
            conv["estado"] = "suporte"
            await enviar_mensagem(numero, MENU_SUPORTE)

        elif texto == "2":
            conv["estado"] = "ativacao"
            await enviar_mensagem(numero, (
                "*Ativação de licença*\n\n"
                "1. Abra o sistema instalado.\n"
                "2. Na tela de ativação, informe o serial\n"
                "   (formato XXXX-XXXX-XXXX-XXXX).\n"
                "3. Clique em *Ativar sistema*.\n\n"
                "O serial consta no e-mail de confirmação da assinatura. "
                "Verifique também a pasta de spam.\n\n"
                "*1* · Não recebi o serial\n"
                "*2* · O serial não funciona\n"
                "*0* · Voltar ao menu"
            ))

        elif texto == "3":
            conv["estado"] = "precos"
            await enviar_mensagem(numero, MENU_PRECOS)

        elif texto == "4":
            conv["estado"] = "aguardando_humano"
            await notificar_atendente(numero, conv["nome"], conv["ultima_msg"])
            await enviar_mensagem(numero, (
                "Sua solicitação foi registrada.\n\n"
                "Um atendente responde aqui mesmo, neste WhatsApp.\n\n"
                + HORARIO
            ))

        else:
            await enviar_mensagem(numero, (
                "Não reconheci essa opção. Responda com o número.\n\n"
                + MENU_PRINCIPAL
            ))
        return

    # ── SUPORTE ──
    if estado == "suporte":
        if texto == "0":
            conv["estado"] = "menu"
            await enviar_mensagem(numero, MENU_PRINCIPAL)

        elif texto in RESPOSTAS_SUPORTE:
            conv["estado"] = f"suporte_{texto}"
            await enviar_mensagem(numero, RESPOSTAS_SUPORTE[texto])

        elif texto == "5":
            conv["estado"] = "aguardando_humano"
            await notificar_atendente(numero, conv["nome"], "Suporte - outro assunto")
            await enviar_mensagem(numero, (
                "Registrado. Vou encaminhar para o suporte técnico.\n\n"
                "Um atendente responde aqui mesmo.\n\n"
                + HORARIO
            ))

        else:
            await enviar_mensagem(numero, "Não reconheci essa opção.\n\n" + MENU_SUPORTE)
        return

    # ── PÓS-SUPORTE (resolveu ou não?) ──
    if estado.startswith("suporte_"):
        if texto == "1":
            conv["estado"] = "menu"
            await enviar_mensagem(numero, (
                "Bom saber que resolveu.\n\n"
                "Posso ajudar em mais alguma coisa?\n\n"
                + MENU_PRINCIPAL
            ))
        elif texto == "2":
            conv["estado"] = "aguardando_humano"
            await notificar_atendente(numero, conv["nome"], f"Suporte não resolvido: {estado}")
            await enviar_mensagem(numero, (
                "Vou encaminhar para o suporte técnico com o histórico "
                "do que você já tentou.\n\n"
                + HORARIO
            ))
        elif texto == "0":
            conv["estado"] = "suporte"
            await enviar_mensagem(numero, MENU_SUPORTE)
        else:
            await enviar_mensagem(numero, "Responda com *1* (resolvido) ou *2* (continua o problema).")
        return

    # ── ATIVAÇÃO ──
    if estado == "ativacao":
        if texto == "0":
            conv["estado"] = "menu"
            await enviar_mensagem(numero, MENU_PRINCIPAL)
        elif texto in ("1", "2"):
            conv["estado"] = "aguardando_humano"
            motivo = "Não recebeu serial" if texto == "1" else "Serial não funciona"
            await notificar_atendente(numero, conv["nome"], f"Ativação: {motivo}")
            await enviar_mensagem(numero, (
                "Registrado. Vou encaminhar para o suporte verificar "
                "a sua licença.\n\n"
                + HORARIO
            ))
        else:
            await enviar_mensagem(numero, "Responda com o número de uma das opções acima.")
        return

    # ── PREÇOS ──
    if estado == "precos":
        if texto == "0":
            conv["estado"] = "menu"
            await enviar_mensagem(numero, MENU_PRINCIPAL)
        elif texto in ("1", "2"):
            conv["estado"] = "aguardando_humano"
            await notificar_atendente(numero, conv["nome"], "Interesse em planos/contratação")
            await enviar_mensagem(numero, (
                "Sua solicitação foi registrada. Um consultor entra em "
                "contato para indicar o plano adequado ao seu negócio.\n\n"
                + HORARIO
            ))
        elif texto == "3":
            # Qual plano serve depende da nota que o negocio emite, e muita
            # gente nao sabe responder isso sozinha. Em vez de arriscar um
            # palpite, encaminha para uma pessoa — que e o que resolve.
            conv["estado"] = "aguardando_humano"
            await notificar_atendente(
                numero, conv["nome"], "Não sabe qual plano — precisa de orientação fiscal")
            await enviar_mensagem(numero, (
                "É uma dúvida comum. O plano depende do tipo de nota que o "
                "seu negócio precisa emitir, e isso varia conforme o ramo "
                "e o município.\n\n"
                "Um consultor vai confirmar isso com você antes de qualquer "
                "contratação, para você não pagar por nota que não usa.\n\n"
                + HORARIO
            ))
        else:
            await enviar_mensagem(numero, "Não reconheci essa opção.\n\n" + MENU_PRECOS)
        return

    # ── ESTADO IA ──
    # Só chega aqui quem digitou número ou "menu" estando na conversa com a
    # IA — ou seja, pediu o menu de propósito. Texto livre não cai aqui:
    # é tratado no bloco da IA lá em cima.
    if estado == "ia":
        conv["estado"] = "menu"
        await enviar_mensagem(numero, MENU_PRINCIPAL)
        return

    # ── AGUARDANDO HUMANO ──
    if estado == "aguardando_humano":
        # "menu" tem de sair ANTES: antes disto o cliente recebia o aviso
        # de "ja encaminhado" E o menu, duas mensagens para um comando.
        if texto.lower() in ("menu", "0"):
            conv["estado"] = "menu"
            await enviar_mensagem(numero, MENU_PRINCIPAL)
            return
        await enviar_mensagem(numero, (
            "Sua solicitação já está com a nossa equipe.\n\n"
            + HORARIO + "\n\n"
            "Responda *menu* para voltar ao menu principal."
        ))
        return

    # Estado desconhecido — reseta
    conv["estado"] = "menu"
    await enviar_mensagem(numero, MENU_PRINCIPAL)


# ── WEBHOOK ──────────────────────────────────────────────────

@app.post("/webhook")
async def webhook(request: Request):
    try:
        body = await request.json()

        # Evolution API envia eventos — filtra só mensagens recebidas
        evento = body.get("event", "")
        if evento != "messages.upsert":
            return JSONResponse({"ok": True})

        # A Evolution manda em "sender" o JID da PROPRIA instancia (o
        # numero do WhatsApp do bot). Guardar isso permite detectar a
        # configuracao em que NUMERO_NOTIF aponta para o proprio bot: o
        # envio "funciona", a mensagem cai na conversa do bot consigo
        # mesmo, e o atendente nunca ve nada.
        rem = (body.get("sender") or "").split("@")[0]
        if rem.isdigit():
            instancia_info["numero"] = rem

        data = body.get("data", {})

        # Ignora mensagens enviadas pelo próprio bot
        if data.get("key", {}).get("fromMe"):
            return JSONResponse({"ok": True})

        # ── Extrai o número de quem escreveu ──────────────────────
        # O WhatsApp migrou para endereçamento LID: o remoteJid passou a vir
        # como "87389766705237@lid", que NÃO é telefone e não serve para
        # responder. Nesses casos o número real vem em "remoteJidAlt".
        # Sem tratar isso, o bot recebe a mensagem, processa e tenta
        # responder para um destinatário inexistente — falha silenciosa.
        key = data.get("key", {})
        jid = key.get("remoteJid", "") or ""

        if "@g.us" in jid:
            return JSONResponse({"ok": True})  # ignora grupos

        if jid.endswith("@lid"):
            alt = key.get("remoteJidAlt") or ""
            if not alt:
                # Sem o alt não há como responder. Melhor registrar e sair
                # do que enviar para um número inválido.
                print(f"AVISO: mensagem LID sem remoteJidAlt ({jid}) — ignorada")
                return JSONResponse({"ok": True})
            jid = alt

        numero = jid.replace("@s.whatsapp.net", "").replace("@c.us", "")
        if not numero or not numero.isdigit():
            print(f"AVISO: número não reconhecido a partir de {jid!r}")
            return JSONResponse({"ok": True})

        mensagem = data.get("message", {})
        texto = (
            mensagem.get("conversation")
            or mensagem.get("extendedTextMessage", {}).get("text")
            or ""
        )

        # Nome do contato
        nome = data.get("pushName", "")

        if texto:
            await processar_mensagem(numero, texto, nome)

    except Exception as e:
        print(f"ERRO webhook: {e}")

    return JSONResponse({"ok": True})


# Versão do código. Suba este número a cada alteração: é assim que se
# confirma, de fora, QUAL código está rodando depois de um deploy.
VERSAO = "3.8.0"


@app.get("/")
def root():
    return {"status": "ok", "servico": "TypCore WhatsApp Bot", "versao": VERSAO}


@app.get("/versao")
def ver_versao():
    """Diz o que está realmente no ar. Não expõe dado de cliente."""
    base = ia.carregar_base()
    return {
        "versao": VERSAO,
        "ia": {
            "provedor": ia.IA_PROVEDOR,
            "ativa": ia.ia_ativa(),
            "modelo_configurado": (ia.GEMINI_MODELO if ia.IA_PROVEDOR == "gemini"
                                   else ia.ANTHROPIC_MODELO if ia.IA_PROVEDOR == "anthropic"
                                   else None),
            # Qual modelo a API aceitou de fato (só aparece após a 1ª resposta)
            "modelo_em_uso": getattr(ia, "_modelo_ok", None),
        },
        "conhecimento": {
            "carregado": bool(base),
            "planos": len(base.get("planos", {})),
            "produtos": len(base.get("produtos", [])),
        },
        "conversas_ativas": len(conversas),
        # Prova de que a notificacao ao atendente esta saindo de verdade.
        "ultima_notificacao": ultima_notificacao,
        "notificacao": {
            "numero_destino": NUMERO_NOTIF,
            "origem": ("variavel de ambiente"
                       if (os.getenv("NUMERO_NOTIF") or "").strip()
                       else "padrao do codigo (variavel vazia ou ausente)"),
            # Quantos caracteres a variavel tinha. 0 = criada e em branco.
            "tamanho_da_variavel": len(os.getenv("NUMERO_NOTIF") or ""),
            # Descoberto no 1o webhook; null ate chegar a 1a mensagem.
            "numero_do_bot": instancia_info["numero"],
            "CONFLITO_notif_igual_ao_bot": conflito_numero(),
        },
        "evolution": {
            "url": EVOLUTION_URL,          # sem a apikey; só o endereço
            "instancia": INSTANCE_NAME,
            "url_valida": EVOLUTION_URL.startswith(("http://", "https://")),
            "origem": ("variavel de ambiente" if os.getenv("EVOLUTION_URL")
                       else "padrao do codigo"),
        },
        # Diagnóstico: mostra os NOMES das variáveis que o container
        # realmente enxerga. Só nomes — nenhum valor, para não vazar chave.
        # Serve para pegar nome com caractere invisível ou parecido, que
        # aparece normal no painel e não casa no os.getenv.
        "env_encontradas": {
            "EVOLUTION_URL": os.getenv("EVOLUTION_URL") is not None,
            "EVOLUTION_APIKEY": os.getenv("EVOLUTION_APIKEY") is not None,
            "INSTANCE_NAME": os.getenv("INSTANCE_NAME") is not None,
            "NUMERO_NOTIF": os.getenv("NUMERO_NOTIF") is not None,
        },
        "nomes_parecidos": sorted(
            repr(k) for k in os.environ
            if any(p in k.upper() for p in ("EVOL", "INSTANC", "NUMERO", "APIKEY"))
        ),
    }


@app.get("/teste-ia")
async def teste_ia():
    """Testa a IA com uma pergunta FIXA e mostra o resultado cru.

    Pergunta fixa de propósito: assim o endpoint não vira um proxy de
    LLM aberto para qualquer um usar às custas da cota.
    Não expõe dado de cliente.
    """
    if not ia.ia_ativa():
        return {"ok": False, "motivo": "IA desativada",
                "provedor": ia.IA_PROVEDOR}

    pergunta = "Quanto custa o sistema para uma assistência de informática?"
    resposta, escalar = await ia.responder(
        [{"quem": "cliente", "texto": pergunta}])

    return {
        "ok": resposta is not None,
        "pergunta": pergunta,
        "resposta": resposta,
        "escalar": escalar,
        "modelo_em_uso": getattr(ia, "_modelo_ok", None),
        "ultimo_erro": getattr(ia, "ultimo_erro", None),
    }


@app.get("/conversas")
def ver_conversas(token: str = ""):
    """Debug das conversas ativas.

    PROTEGIDO: antes ficava aberto, e a URL do Railway é previsível — ou
    seja, qualquer um lia telefone, nome e mensagens dos clientes. Agora
    exige o token de ADMIN_TOKEN. Sem a variável definida, fica desligado
    (fechado por padrão, e não aberto por padrão).
    """
    esperado = os.getenv("ADMIN_TOKEN", "")
    if not esperado:
        return JSONResponse(
            {"erro": "Endpoint desativado. Defina ADMIN_TOKEN no Railway para usá-lo."},
            status_code=403,
        )
    if token != esperado:
        return JSONResponse({"erro": "Token inválido."}, status_code=403)
    return {
        k: {**v, "ultima": v["ultima"].isoformat()}
        for k, v in conversas.items()
    }


# Janela minima entre testes: impede que alguem descubra a URL e encha o
# seu celular de mensagem. O destino e FIXO (NUMERO_NOTIF) e o texto e
# fixo — nao da para usar este endpoint para mandar mensagem a terceiros.
_ultimo_teste_notif = [0.0]


@app.get("/teste-notificacao")
async def teste_notificacao():
    """Dispara uma notificacao de teste e mostra a resposta crua da Evolution.

    Serve para separar as duas hipoteses sem depender do WhatsApp:
      - erro aqui  -> o envio ao NUMERO_NOTIF esta quebrado (numero, apikey
                      ou instancia desconectada); a resposta diz qual.
      - sucesso    -> o envio funciona e o numero e outro aparelho.
    """
    import time
    agora = time.time()
    if agora - _ultimo_teste_notif[0] < 20:
        return JSONResponse(
            {"erro": "Aguarde 20s entre testes."}, status_code=429)
    _ultimo_teste_notif[0] = agora

    res = await enviar_detalhado(
        NUMERO_NOTIF,
        "\U0001f527 Teste de notificacao do bot TypCore. "
        "Se voce recebeu isto, o aviso ao atendente esta funcionando.")
    return {
        "versao": VERSAO,
        "numero_destino": NUMERO_NOTIF,
        "numero_do_bot": instancia_info["numero"],
        "CONFLITO_notif_igual_ao_bot": conflito_numero(),
        "origem_do_numero": ("variavel de ambiente"
                             if os.getenv("NUMERO_NOTIF") else "padrao do codigo"),
        "instancia": INSTANCE_NAME,
        "evolution_url": EVOLUTION_URL,
        "resultado": res,
    }
