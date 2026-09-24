# -*- coding: utf-8 -*-
"""
Camada de IA do bot TypCore — provedor trocável.

Trocar de Gemini para Anthropic (ou desligar a IA) é só mudar a variável
IA_PROVEDOR no Railway. Nada de código muda.

    IA_PROVEDOR = gemini     -> usa a API do Google (free tier)
    IA_PROVEDOR = anthropic  -> usa a API da Anthropic (paga)
    IA_PROVEDOR = off        -> desliga; o bot usa só o menu

PRIVACIDADE — decisão deliberada, não descuido:
  Só o TEXTO da mensagem sai daqui. Telefone e nome do cliente NUNCA são
  enviados ao modelo, porque ele não precisa deles para falar de planos.
  Antes de enviar, limpar_dados_pessoais() remove CPF, CNPJ, telefone,
  e-mail e CEP que o cliente porventura tenha digitado.
  No free tier do Gemini, o Google usa o conteúdo para treinar os modelos;
  por isso o que sai precisa ser anônimo — e por isso o suporte técnico
  NÃO passa por aqui (é lá que o cliente conta detalhe do negócio dele).

REGRA DE OURO:
  O modelo só pode afirmar o que está em conhecimento.json, que é gerado
  das páginas do próprio site. Se não souber, ele encaminha para o humano.
  Inventar recurso que o ERP não tem gera reembolso e má reputação — é
  pior do que não responder.
"""
import asyncio
import os
import re
import json
import httpx
from pathlib import Path

IA_PROVEDOR = os.getenv("IA_PROVEDOR", "off").strip().lower()
GEMINI_KEY = os.getenv("GEMINI_API_KEY", "")
# Vazio de propósito: a ordenação escolhe o mais novo que funcionar.
# Defina só se quiser forçar um modelo específico.
GEMINI_MODELO = os.getenv("GEMINI_MODELO", "")
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODELO = os.getenv("ANTHROPIC_MODELO", "claude-haiku-4-5-20251001")

# Teto de mensagens por conversa: impede que alguém consuma a cota à toa.
MAX_TURNOS_IA = int(os.getenv("MAX_TURNOS_IA", "12"))
# 12s era pouco: a primeira chamada, com a base de conhecimento
# inteira no prompt, estourava e voltava erro VAZIO.
TIMEOUT_IA = float(os.getenv("TIMEOUT_IA", "35"))

_BASE = None


def carregar_base() -> dict:
    global _BASE
    if _BASE is None:
        p = Path(__file__).resolve().parent / "conhecimento.json"
        _BASE = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    return _BASE


# ── Limpeza de dados pessoais ────────────────────────────────
_PADROES = [
    (re.compile(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b"), "[CPF]"),
    (re.compile(r"\b\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}\b"), "[CNPJ]"),
    (re.compile(r"\b\d{5}-?\d{3}\b"), "[CEP]"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "[EMAIL]"),
    (re.compile(r"\(?\b\d{2}\)?\s?9?\d{4}[-\s]?\d{4}\b"), "[TELEFONE]"),
    (re.compile(r"\b(?:\d[ -]*?){13,19}\b"), "[NUMERO]"),
]


def limpar_dados_pessoais(texto: str) -> str:
    """Remove identificadores antes de qualquer coisa sair do servidor."""
    for padrao, marca in _PADROES:
        texto = padrao.sub(marca, texto)
    return texto


# ── Prompt ───────────────────────────────────────────────────
def montar_instrucao() -> str:
    kb = carregar_base()
    linhas = [
        "Você é o assistente virtual da TypCore, que vende sistemas de gestão (ERP) "
        "para pequenos negócios brasileiros.",
        "",
        "COMO FALAR:",
        "- Português brasileiro. Cordial e direto, como um atendimento comercial "
        "competente no WhatsApp. Nunca eufórico.",
        "- Respostas CURTAS: no máximo 4 linhas. É WhatsApp, não e-mail.",
        "- Nada de bullet point nem markdown pesado. Negrito do WhatsApp (*assim*) com parcimônia.",
        "- Sem emoji. Sem exclamação em 'Perfeito!', 'Ótimo!', 'Que bom!'.",
        "- Não repita saudação toda hora. Cumprimente uma vez só.",
        "",
        "O QUE VOCÊ É:",
        "- Você é o assistente virtual da TypCore e DIZ ISSO se perguntarem. "
        "Nunca finja ser uma pessoa.",
        "",
        "NUNCA REVELE A ESTRUTURA INTERNA DA EMPRESA:",
        "- NUNCA cite o nome de nenhuma pessoa da TypCore. Diga sempre 'nossa equipe', "
        "'o suporte técnico' ou 'um consultor'.",
        "- NUNCA diga quem desenvolveu ou programou os sistemas, nem que quem atende é "
        "o desenvolvedor. Isso passa a impressão de empresa de uma pessoa só e derruba "
        "a confiança de quem vai assinar uma mensalidade.",
        "- NUNCA fale sobre tamanho da equipe, quantos clientes existem, faturamento, "
        "ou que a empresa é nova.",
        "- Se perguntarem quem está por trás: 'A TypCore é uma empresa brasileira de "
        "software de gestão' e siga para o que a pessoa precisa.",
        "",
        "NAO PROMETA PRAZO:",
        "- Ao transferir, NUNCA diga 'só um instante', 'já volto' ou 'em 5 minutos'. "
        "A resposta pode sair fora do horário comercial.",
        "- Diga: 'Vou encaminhar para a nossa equipe, que responde aqui mesmo. "
        "Atendimento de seg a sex, 8h às 18h, e sáb, 9h às 13h.'",
        "",
        "REGRA MAIS IMPORTANTE — NÃO INVENTE:",
        "- Só afirme o que está nos DADOS abaixo. Nada fora disso.",
        "- Se perguntarem um recurso que não está listado, NÃO diga que tem nem que não tem.",
        "  Diga que vai confirmar e responda com [ESCALAR].",
        "- Nunca invente preço, prazo de lançamento, integração ou funcionalidade.",
        "- Na dúvida, escalar é sempre melhor que arriscar.",
        "",
        "QUANDO ESCALAR:",
        "- Termine a mensagem com [ESCALAR] quando: o cliente quiser fechar negócio, "
        "pedir para falar com alguém, reclamar, perguntar algo que não está nos dados, "
        "ou for assunto de suporte técnico de quem já é cliente.",
        "- O [ESCALAR] é removido antes de enviar; ele apenas notifica a equipe.",
        "",
        "=== DADOS (única fonte de verdade) ===",
        "",
        "PLANOS (por mês; o anual é o mais barato):",
    ]
    for p in kb.get("planos", {}).values():
        linhas.append(
            f"- {p['nome']}: R$ {p['mensal']} mensal, R$ {p['trimestral']} trimestral, "
            f"R$ {p['anual']} anual. {p['para_quem']}")
    linhas += ["", "CONDIÇÕES:"]
    for v in kb.get("fatos", {}).values():
        linhas.append(f"- {v}")
    linhas += ["", "PRODUTOS:"]
    for pr in kb.get("produtos", []):
        estado = "DISPONÍVEL" if pr["disponivel"] else "AINDA NÃO LANÇADO"
        linhas.append(f"\n{pr['nome']} ({estado}) — plano {pr['plano']} — {pr['pagina']}")
        linhas.append(f"  {pr['descricao']}")
        for r in pr["recursos"][:8]:
            linhas.append(f"  • {r}")
    linhas += [
        "",
        "SOBRE OS NÃO LANÇADOS: diga que está em desenvolvimento e ofereça avisar "
        "quando sair. NUNCA prometa data.",
    ]
    return "\n".join(linhas)


# ── Provedores ───────────────────────────────────────────────
# Nome do modelo resolvido em tempo de execução (ver _descobrir_modelo).
_modelo_ok = None
_lista_cache = None

# Último erro da IA, exposto em /teste-ia para diagnóstico.
ultimo_erro = None


async def _listar_candidatos() -> list:
    """Modelos que valem a tentativa, do mais novo para o mais antigo.

    Não basta ler a lista da API: ela inclui modelos que aparecem mas
    respondem 404 para contas novas (aconteceu com gemini-2.5-flash, que
    mandava usar gemini-3.6-flash). Por isso devolvemos vários candidatos
    e _gemini() tenta até um responder.
    """
    async with httpx.AsyncClient(timeout=TIMEOUT_IA) as c:
        r = await c.get(
            f"https://generativelanguage.googleapis.com/v1beta/models?key={GEMINI_KEY}")
        r.raise_for_status()
        modelos = r.json().get("models", [])

    nomes = [m["name"].split("/")[-1] for m in modelos
             if "generateContent" in m.get("supportedGenerationMethods", [])]

    def versao(n):
        m = re.search(r"gemini-(\d+)(?:\.(\d+))?", n)
        return (int(m.group(1)), int(m.group(2) or 0)) if m else (0, 0)

    # Fora modelos que não servem para conversa em texto. Eles aparecem na
    # lista e aceitam generateContent, mas recusam instrução de sistema
    # ("Developer instruction is not enabled for this model") ou geram
    # áudio/imagem. Sem este filtro, a fila desperdiça tentativas neles.
    EXCLUIR = ("tts", "image", "audio", "embedding", "vision", "live",
               "aqa", "learnlm", "veo", "imagen")
    nomes = [n for n in nomes if not any(x in n.lower() for x in EXCLUIR)]

    flash = [n for n in nomes if "flash" in n]
    # mais novos primeiro; estáveis antes de preview/exp
    flash.sort(key=lambda n: (versao(n),
                              0 if any(x in n for x in ("preview", "exp")) else 1),
               reverse=True)

    # o configurado tem prioridade, se existir
    ordem = ([GEMINI_MODELO] if GEMINI_MODELO in nomes else []) + flash + nomes
    vistos, saida = set(), []
    for n in ordem:
        if n not in vistos:
            vistos.add(n); saida.append(n)
    return saida[:6]


async def _gemini(instrucao: str, historico: list) -> str:
    global _modelo_ok, _lista_cache

    # A lista da API é buscada UMA vez e reaproveitada (evita um ida-e-volta
    # extra a cada mensagem).
    if _lista_cache is None:
        _lista_cache = await _listar_candidatos()

    # O modelo que já funcionou vira PREFERÊNCIA, não exclusividade.
    # Antes eu tentava só ele — e quando ele ficava sobrecarregado (503), o
    # bot desistia sem testar os outros da fila, que estavam disponíveis.
    # A fila existe justamente para esse caso; travá-la no vencedor
    # anulava o motivo de ela existir.
    candidatos = list(_lista_cache)
    if _modelo_ok and _modelo_ok in candidatos:
        candidatos.remove(_modelo_ok)
    if _modelo_ok:
        candidatos.insert(0, _modelo_ok)

    erros, transitorios = [], []
    for modelo in candidatos:
        try:
            texto = await _gemini_tentar(modelo, instrucao, historico)
            if _modelo_ok != modelo:
                print(f"[ia] usando modelo {modelo!r}")
            _modelo_ok = modelo
            return texto
        except Exception as e:
            msg = str(e) or f"{type(e).__name__} (sem detalhe)"
            erros.append(f"{modelo}: {msg}")
            if "503" in msg or "UNAVAILABLE" in msg:
                transitorios.append(modelo)   # sobrecarga: vale tentar de novo
            continue

    # Segunda rodada só nos que falharam por sobrecarga momentânea.
    for modelo in transitorios:
        try:
            await asyncio.sleep(1.5)
            texto = await _gemini_tentar(modelo, instrucao, historico)
            print(f"[ia] usando modelo {modelo!r} (2a tentativa)")
            _modelo_ok = modelo
            return texto
        except Exception as e:
            erros.append(f"{modelo} (retry): {str(e) or type(e).__name__}")

    # Falhou a fila inteira: pode ser que a lista em cache tenha envelhecido
    # (modelo descontinuado, conta com outro acesso). Invalida para a proxima
    # mensagem buscar a lista nova em vez de insistir numa fila morta.
    _lista_cache = None
    raise RuntimeError("nenhum modelo respondeu -> " + " | ".join(erros)[:600])


async def _gemini_tentar(modelo: str, instrucao: str, historico: list) -> str:
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{modelo}:generateContent?key={GEMINI_KEY}")
    payload = {
        "system_instruction": {"parts": [{"text": instrucao}]},
        "contents": [
            {"role": ("user" if m["quem"] == "cliente" else "model"),
             "parts": [{"text": m["texto"]}]}
            for m in historico
        ],
        "generationConfig": {
            "temperature": 0.6,
            # Folga generosa: os modelos 2.5+ gastam parte do orçamento
            # "pensando" antes de escrever. Com teto baixo eles estouram o
            # limite no raciocínio e devolvem resposta VAZIA.
            "maxOutputTokens": 2048,
            # Desliga o raciocínio interno: para responder sobre planos e
            # produtos não é necessário, e ele só encarece e atrasa.
            # Modelos que não suportam o campo simplesmente o ignoram.
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
    async with httpx.AsyncClient(timeout=TIMEOUT_IA) as c:
        r = await c.post(url, json=payload)
        if r.status_code >= 400:
            # Mostra o motivo real em vez de um erro genérico — foi a falta
            # disso que escondeu o problema.
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:400]}")
        d = r.json()

    cands = d.get("candidates") or []
    if not cands:
        raise RuntimeError(f"sem candidates: {str(d)[:300]}")

    c0 = cands[0]
    partes = (c0.get("content") or {}).get("parts") or []
    # Ignora blocos de raciocínio; só interessa o texto para o cliente.
    texto = "".join(pt.get("text", "") for pt in partes if not pt.get("thought"))

    if not texto.strip():
        raise RuntimeError(
            f"resposta vazia (finishReason={c0.get('finishReason')}) "
            f"usage={d.get('usageMetadata')}")

    return texto.strip()


async def _anthropic(instrucao: str, historico: list) -> str:
    payload = {
        "model": ANTHROPIC_MODELO,
        "max_tokens": 400,
        "temperature": 0.6,
        # cache_control corta o custo da base repetida a cada turno
        "system": [{"type": "text", "text": instrucao,
                    "cache_control": {"type": "ephemeral"}}],
        "messages": [
            {"role": ("user" if m["quem"] == "cliente" else "assistant"),
             "content": m["texto"]}
            for m in historico
        ],
    }
    async with httpx.AsyncClient(timeout=TIMEOUT_IA) as c:
        r = await c.post(
            "https://api.anthropic.com/v1/messages",
            json=payload,
            headers={"x-api-key": ANTHROPIC_KEY,
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
        )
        r.raise_for_status()
        d = r.json()
    return "".join(b.get("text", "") for b in d.get("content", [])).strip()


def ia_ativa() -> bool:
    if IA_PROVEDOR == "gemini":
        return bool(GEMINI_KEY)
    if IA_PROVEDOR == "anthropic":
        return bool(ANTHROPIC_KEY)
    return False


# Frases com que o modelo anuncia que vai passar para um humano. Sao VERBOS
# de promessa de proposito: substantivos como "nossa equipe" ou "um atendente"
# aparecem tambem em resposta puramente informativa ("o suporte e no
# WhatsApp") e gerariam notificacao a toa.
_PROMESSAS = (
    "vou te transferir", "vou transferir", "vou te passar", "vou passar para",
    "vou encaminhar", "vou repassar", "vou chamar", "vou acionar",
    "vou pedir para", "vou pedir que", "vou verificar com", "vou confirmar com",
    "vou checar com", "vou avisar", "estou transferindo", "estou encaminhando",
    "transferindo voce", "transferindo voc\u00ea",
    "entrara em contato", "entrar\u00e1 em contato",
    "entraremos em contato", "entram em contato", "entra em contato",
    "retornamos", "te retorno", "damos retorno",
    "so um instante", "s\u00f3 um instante", "um instante", "um momento",
    "aguarde um", "j\u00e1 volto", "ja volto",
)


def promete_transferencia(txt: str) -> bool:
    """A resposta anuncia atendimento humano? Entao tem de notificar."""
    t = (txt or "").lower()
    return any(f in t for f in _PROMESSAS)


async def responder(historico: list):
    """Devolve (texto, escalar) — ou (None, False) se a IA não puder responder.

    (None, False) NÃO é erro: é o sinal para o bot cair no menu de sempre.
    Melhor um menu funcionando que um silêncio.
    """
    if not ia_ativa():
        return None, False
    if len([m for m in historico if m["quem"] == "cliente"]) > MAX_TURNOS_IA:
        return None, True  # conversa longa demais: entrega para o humano

    # Só o texto sai, e sem identificadores.
    limpo = [{"quem": m["quem"], "texto": limpar_dados_pessoais(m["texto"])}
             for m in historico]

    try:
        if IA_PROVEDOR == "gemini":
            txt = await _gemini(montar_instrucao(), limpo)
        else:
            txt = await _anthropic(montar_instrucao(), limpo)
    except Exception as e:
        global ultimo_erro
        ultimo_erro = f"{type(e).__name__}: {e}"
        print(f"[ia] falhou ({ultimo_erro}) — caindo no menu")
        return None, False

    if not txt:
        return None, False

    escalar = "[ESCALAR]" in txt
    txt = txt.replace("[ESCALAR]", "").strip()

    # REDE DE SEGURANCA: o modelo as vezes PROMETE a transferencia em prosa
    # e esquece a tag. Sem isto o cliente ouve "vou te transferir" e
    # ninguem e avisado — ele fica esperando um retorno que nao existe.
    # Uma notificacao a mais custa uma olhada no celular; uma a menos
    # custa o cliente.
    if not escalar and promete_transferencia(txt):
        escalar = True

    return (txt or None), escalar
