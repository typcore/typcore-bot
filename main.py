"""
TypCore WhatsApp Bot
Webhook FastAPI + Evolution API
"""
import os
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
NUMERO_NOTIF     = os.getenv("NUMERO_NOTIF", "5511970667575")  # seu celular pessoal

app = FastAPI(title="TypCore WhatsApp Bot")

# ── ESTADO DAS CONVERSAS (em memória) ────────────────────────
# { "5511999999999": { "estado": "menu", "nome": "João", "ultima": datetime } }
conversas: dict = {}

TIMEOUT_MINUTOS = 30  # reseta conversa após inatividade


# ── CLIENTE EVOLUTION API ────────────────────────────────────

async def enviar_mensagem(numero: str, texto: str):
    """Envia mensagem de texto via Evolution API."""
    url = f"{EVOLUTION_URL}/message/sendText/{INSTANCE_NAME}"
    payload = {
        "number": numero,
        "text":   texto,
    }
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            r = await client.post(
                url,
                json=payload,
                headers={"apikey": EVOLUTION_APIKEY},
            )
            return r.json()
        except Exception as e:
            print(f"ERRO ao enviar para {numero}: {e}")


async def notificar_atendente(numero_cliente: str, nome: str, ultima_msg: str):
    """Notifica o atendente humano quando cliente pede suporte."""
    texto = (
        f"🔔 *Novo cliente aguardando atendimento!*\n\n"
        f"👤 Nome: {nome or 'Não informado'}\n"
        f"📱 Número: {numero_cliente}\n"
        f"💬 Última mensagem: _{ultima_msg}_\n\n"
        f"Acesse o WhatsApp para atender."
    )
    await enviar_mensagem(NUMERO_NOTIF, texto)


# ── TEXTOS DO BOT ────────────────────────────────────────────

MENU_PRINCIPAL = """\
Olá! Bem-vindo à *TypCore* 👋

Sou o assistente virtual da TypCore e estou aqui para ajudá-lo.

Por favor, selecione uma opção:

1️⃣  Suporte técnico
2️⃣  Ativar minha licença
3️⃣  Planos e preços
4️⃣  Falar com um atendente

_Digite o número da opção desejada._"""

MENU_SUPORTE = """\
*Suporte Técnico* 🛠️

Selecione o assunto:

1️⃣  Sistema não abre ou trava
2️⃣  Erro de conexão com banco de dados
3️⃣  Problema na ativação do serial
4️⃣  Dúvida sobre funcionalidades
5️⃣  Outro assunto

0️⃣  ← Voltar ao menu principal"""

# ATENCAO: estes valores tem de bater com window.TYPCORE.tiers do site
# (typcore.com.br). Mexeu no preco la, atualize AQUI tambem — hoje sao
# duas fontes separadas. Conferido em 23/09/2026.
MENU_PRECOS = """\
*Planos TypCore* 💼

O preço depende da nota fiscal que o seu negócio emite:

*Essencial* — sem nota fiscal
R$ 89/mês · R$ 79 no trimestral · R$ 69 no anual
_Clínicas, salões, petshops, pilates, fisioterapia_

*Fiscal* — com NFC-e ou NFS-e
R$ 129/mês · R$ 119 no trimestral · R$ 99 no anual
_Mercadinhos, lojas, mecânicas, celulares, informática_

*Fiscal Completo* — NFC-e e NFS-e
R$ 199/mês · R$ 189 no trimestral · R$ 179 no anual
_Para quem emite os dois tipos de nota_

Todos incluem: até 3 computadores, suporte no WhatsApp,
atualizações e backup. Sem fidelidade e sem taxa de implantação.

🎁 *15 dias grátis, sem cartão.*

*Disponíveis agora:*
🦷 Odonto · 💅 Estética · 🏃 Fisioterapia
🛒 Mercadinho · 💻 Informática · 📱 Celulares

*Em breve:* Mecânica (Autos e Motos), Lojas, Salão,
Veterinária, Petshop, Pilates e outros.

1️⃣  Quero contratar
2️⃣  Tenho dúvidas sobre os planos
3️⃣  Não sei qual plano é o meu
0️⃣  ← Voltar ao menu principal"""

RESPOSTAS_SUPORTE = {
    "1": """\
*Sistema não abre ou trava* 💻

Siga os passos abaixo:

1. Feche o sistema completamente
2. Aguarde 30 segundos
3. Abra novamente como *Administrador*
   (botão direito → Executar como administrador)
4. Se persistir, verifique se o serviço *MariaDB* está ativo:
   Windows + R → services.msc → MariaDB → Iniciar

Isso resolveu o problema?

1️⃣  Sim, resolveu!
2️⃣  Não, ainda com problema
0️⃣  ← Voltar ao menu""",

    "2": """\
*Erro de conexão com banco de dados* 🗄️

Verifique:

1. O serviço *MariaDB* está rodando?
   Windows + R → services.msc → MariaDB → Iniciar

2. O arquivo *config.ini* existe na pasta do sistema?
   Se não existir, abra o sistema e configure a conexão

3. Tente reinstalar o sistema se o problema persistir

Isso resolveu?

1️⃣  Sim, resolveu!
2️⃣  Não, preciso de mais ajuda
0️⃣  ← Voltar ao menu""",

    "3": """\
*Problema na ativação do serial* 🔑

Verifique:

1. O serial está no formato correto?
   Exemplo: *XXXX-XXXX-XXXX-XXXX*

2. Certifique-se de que o computador está conectado à internet

3. O serial foi ativado em outra máquina?
   Cada licença permite até *3 computadores*

4. Verifique o e-mail onde recebeu o serial

Isso resolveu?

1️⃣  Sim, resolveu!
2️⃣  Não, preciso de mais ajuda
0️⃣  ← Voltar ao menu""",

    # Os links abaixo foram conferidos em 23/09/2026 (todos respondem 200).
    # O caminho antigo /manuais/ estava dando 404 — o correto e /downloads/.
    "4": """\
*Dúvidas sobre funcionalidades* 📖

Baixe o manual do seu sistema:

🦷 Odonto
typcore.com.br/downloads/Manual_TypCore_Odonto.pdf

💅 Estética
typcore.com.br/downloads/Manual_TypCore_Estetica.pdf

🏃 Fisioterapia
typcore.com.br/downloads/Manual_TypCore_Fisioterapia.pdf

🛒 Mercadinho
typcore.com.br/downloads/Manual_TypCore_Mercadinho.pdf

💻 Informática
typcore.com.br/downloads/Manual_TypCore_Informatica.pdf

📱 Celulares
typcore.com.br/downloads/Manual_TypCore_Celulares.pdf

Ainda tem dúvidas?

1️⃣  Sim, quero falar com um atendente
0️⃣  ← Voltar ao menu""",
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
)


def parece_suporte(texto: str) -> bool:
    t = texto.lower()
    return any(s in t for s in SINAIS_SUPORTE)


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
        # resposta vazia = IA indisponível: segue para o menu de sempre,
        # em vez de deixar o cliente sem resposta.
        if escalar:
            conv["estado"] = "aguardando_humano"
            await notificar_atendente(numero, conv["nome"], conv["ultima_msg"])
            await enviar_mensagem(numero, (
                "Vou chamar alguém para te atender melhor. 👍\n\n"
                "⏱️ Seg–Sex: 08h–18h | Sáb: 09h–13h"
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
                "*Ativação de Licença* 🔑\n\n"
                "Para ativar sua licença:\n\n"
                "1. Abra o sistema instalado\n"
                "2. Na tela de ativação, insira seu serial\n"
                "   Formato: XXXX-XXXX-XXXX-XXXX\n"
                "3. Clique em *Ativar sistema*\n\n"
                "Não recebeu seu serial? Verifique sua caixa de entrada "
                "e a pasta de spam.\n\n"
                "1️⃣  Não recebi o serial\n"
                "2️⃣  Meu serial não funciona\n"
                "0️⃣  ← Voltar ao menu"
            ))

        elif texto == "3":
            conv["estado"] = "precos"
            await enviar_mensagem(numero, MENU_PRECOS)

        elif texto == "4":
            conv["estado"] = "aguardando_humano"
            await notificar_atendente(numero, conv["nome"], conv["ultima_msg"])
            await enviar_mensagem(numero, (
                "Perfeito! 👍\n\n"
                "Um de nossos atendentes foi notificado e entrará em contato "
                "em breve pelo WhatsApp.\n\n"
                "⏱️ *Horário de atendimento:*\n"
                "Segunda a sexta: 08h às 18h\n"
                "Sábados: 09h às 13h\n\n"
                "Obrigado pela paciência! 😊"
            ))

        else:
            await enviar_mensagem(numero, (
                "Opção não reconhecida. Por favor, digite o número da opção desejada.\n\n"
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
                "Entendido! Vou encaminhar para nosso suporte. 👍\n\n"
                "Um atendente entrará em contato em breve.\n\n"
                "⏱️ Seg–Sex: 08h–18h | Sáb: 09h–13h"
            ))

        else:
            await enviar_mensagem(numero, "Opção inválida.\n\n" + MENU_SUPORTE)
        return

    # ── PÓS-SUPORTE (resolveu ou não?) ──
    if estado.startswith("suporte_"):
        if texto == "1":
            conv["estado"] = "menu"
            await enviar_mensagem(numero, (
                "Ótimo! Fico feliz que tenha resolvido. 😊\n\n"
                "Posso ajudar com mais alguma coisa?\n\n"
                + MENU_PRINCIPAL
            ))
        elif texto == "2":
            conv["estado"] = "aguardando_humano"
            await notificar_atendente(numero, conv["nome"], f"Suporte não resolvido: {estado}")
            await enviar_mensagem(numero, (
                "Compreendo. Vou encaminhar para nosso suporte especializado. 🛠️\n\n"
                "Um técnico entrará em contato em breve.\n\n"
                "⏱️ Seg–Sex: 08h–18h | Sáb: 09h–13h"
            ))
        elif texto == "0":
            conv["estado"] = "suporte"
            await enviar_mensagem(numero, MENU_SUPORTE)
        else:
            await enviar_mensagem(numero, "Por favor, responda com 1 (resolveu) ou 2 (não resolveu).")
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
                "Entendido! Vou acionar nosso suporte para resolver isso. 🔑\n\n"
                "Um atendente entrará em contato em breve com seu serial.\n\n"
                "⏱️ Seg–Sex: 08h–18h | Sáb: 09h–13h"
            ))
        else:
            await enviar_mensagem(numero, "Por favor, selecione uma das opções acima.")
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
                "Perfeito! Um de nossos consultores entrará em contato para "
                "apresentar a melhor opção para o seu negócio. 😊\n\n"
                "⏱️ Seg–Sex: 08h–18h | Sáb: 09h–13h"
            ))
        elif texto == "3":
            # Qual plano serve depende da nota que o negocio emite, e muita
            # gente nao sabe responder isso sozinha. Em vez de arriscar um
            # palpite, encaminha para uma pessoa — que e o que resolve.
            conv["estado"] = "aguardando_humano"
            await notificar_atendente(
                numero, conv["nome"], "Não sabe qual plano — precisa de orientação fiscal")
            await enviar_mensagem(numero, (
                "Sem problema, isso é bem comum. 😊\n\n"
                "O plano certo depende do tipo de nota que o seu negócio "
                "precisa emitir, e isso muda conforme o ramo e o município.\n\n"
                "Vou pedir para alguém te chamar e confirmar isso com você — "
                "assim você não paga por nota que não usa.\n\n"
                "⏱️ Seg–Sex: 08h–18h | Sáb: 09h–13h"
            ))
        else:
            await enviar_mensagem(numero, "Opção inválida.\n\n" + MENU_PRECOS)
        return

    # ── ESTADO IA (cliente digitou número ou "menu") ──
    if estado == "ia":
        conv["estado"] = "menu"
        await enviar_mensagem(numero, MENU_PRINCIPAL)
        return

    # ── AGUARDANDO HUMANO ──
    if estado == "aguardando_humano":
        await enviar_mensagem(numero, (
            "Seu atendimento já foi encaminhado para nossa equipe. 👍\n\n"
            "Em breve um atendente entrará em contato.\n\n"
            "⏱️ Seg–Sex: 08h–18h | Sáb: 09h–13h\n\n"
            "Digite *menu* para acessar o menu principal."
        ))
        if texto.lower() == "menu":
            conv["estado"] = "menu"
            await enviar_mensagem(numero, MENU_PRINCIPAL)
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
VERSAO = "2.7.0"


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
