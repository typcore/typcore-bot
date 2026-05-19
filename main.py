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

# ── CONFIGURAÇÃO ─────────────────────────────────────────────
EVOLUTION_URL = os.getenv("EVOLUTION_URL", "http://localhost:8080").strip()
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
    # Forçamos a URL correta direto aqui para eliminar o erro de leitura do os.getenv
    url_fixa = "https://evolution-api-production-8c70.up.railway.app"
    
    url = f"{url_fixa}/message/sendText/{INSTANCE_NAME}"
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

MENU_PRECOS = """\
*Planos TypCore* 💼

Todos os sistemas possuem os seguintes planos:

• *Trial* — 15 dias grátis, sem cartão
• *Mensal* — R$ 149/mês
• *Trimestral* — R$ 399 (economia de 11%)
• *Anual* — R$ 1.399 (economia de 22%)

Sistemas disponíveis:
🌸 TypCore Estética
🦷 TypCore Odonto
🛒 TypCore Mercadinho
🔧 TypCore Mecânica
📱 TypCore Celulares

Para contratar ou tirar dúvidas, selecione:

1️⃣  Quero contratar
2️⃣  Tenho dúvidas sobre os planos
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

    "4": """\
*Dúvidas sobre funcionalidades* 📖

Você pode consultar o manual do sistema:

• 📄 Manual TypCore Estética:
  typcore.com.br/manuais/Manual_TypCore_Estetica_v2.pdf

• 📹 Tutoriais em vídeo:
  Em breve em nosso site

Ainda tem dúvidas?

1️⃣  Sim, quero falar com um atendente
0️⃣  ← Voltar ao menu""",
}


# ── MÁQUINA DE ESTADOS ───────────────────────────────────────

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
        else:
            await enviar_mensagem(numero, "Opção inválida.\n\n" + MENU_PRECOS)
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

        # A Evolution envia os dados dentro de uma lista ou objeto. Vamos garantir o mapeamento:
        data_payload = body.get("data", {})
        
        # Se vier como lista (padrão Baileys), pegamos o primeiro item
        if isinstance(data_payload, list):
            if not data_payload:
                return JSONResponse({"ok": True})
            data = data_payload[0]
        else:
            data = data_payload

        # Ignora mensagens enviadas pelo próprio bot
        if data.get("key", {}).get("fromMe"):
            return JSONResponse({"ok": True})

        remote_jid = data.get("key", {}).get("remoteJid", "")

        # Validação crucial: Ignora se for grupo, lista de transmissão ou atualização de status
        if not remote_jid or "@g.us" in remote_jid or "@broadcast" in remote_jid or "status@broadcast" in remote_jid:
            return JSONResponse({"ok": True})

        # Extrai número limpo
        numero = remote_jid.split("@")[0]

        mensagem = data.get("message", {})
        if not mensagem:
            return JSONResponse({"ok": True})

        # Captura texto de conversas normais, respondidas, mídias com legenda ou cliques em botões
        texto = (
            mensagem.get("conversation")
            or mensagem.get("extendedTextMessage", {}).get("text")
            or mensagem.get("imageMessage", {}).get("caption")
            or mensagem.get("videoMessage", {}).get("caption")
            or mensagem.get("buttonsResponseMessage", {}).get("selectedButtonId")
            or mensagem.get("templateButtonReplyMessage", {}).get("selectedId")
            or ""
        )

        # Nome do contato que aparece no WhatsApp do cliente
        nome = data.get("pushName", "")

        # Só processa se o cliente de fato enviou algum texto ou comando
        if texto.strip():
            await processar_mensagem(numero, texto, nome)

    except Exception as e:
        # Esse print vai direto para o Log do seu Railway facilitando o seu debug
        print(f"ERRO webhook TypCore: {e}")

    return JSONResponse({"ok": True})


@app.get("/")
def root():
    return {"status": "ok", "servico": "TypCore WhatsApp Bot"}


@app.get("/conversas")
def ver_conversas():
    """Endpoint de debug — mostra conversas ativas."""
    return {
        k: {**v, "ultima": v["ultima"].isoformat()}
        for k, v in conversas.items()
    }
