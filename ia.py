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
import os
import re
import json
import httpx
from pathlib import Path

IA_PROVEDOR = os.getenv("IA_PROVEDOR", "off").strip().lower()
GEMINI_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODELO = os.getenv("GEMINI_MODELO", "gemini-3.6-flash")
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODELO = os.getenv("ANTHROPIC_MODELO", "claude-haiku-4-5-20251001")

# Teto de mensagens por conversa: impede que alguém consuma a cota à toa.
MAX_TURNOS_IA = int(os.getenv("MAX_TURNOS_IA", "12"))
TIMEOUT_IA = float(os.getenv("TIMEOUT_IA", "12"))

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
        "- Português brasileiro, informal mas profissional. Como um vendedor atencioso "
        "conversa no WhatsApp, não como um manual.",
        "- Respostas CURTAS: no máximo 4 linhas. É WhatsApp, não e-mail.",
        "- Nada de bullet point nem markdown pesado. Negrito do WhatsApp (*assim*) com parcimônia.",
        "- Não repita saudação toda hora. Cumprimente uma vez só.",
        "",
        "O QUE VOCÊ É:",
        "- Você é um assistente virtual e DIZ ISSO se perguntarem. Nunca finja ser uma pessoa.",
        "- Quem atende de verdade é o Leandro, que desenvolveu os sistemas.",
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
        "- O [ESCALAR] é removido antes de enviar; ele apenas avisa o Leandro.",
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
    global _modelo_ok
    candidatos = [_modelo_ok] if _modelo_ok else await _listar_candidatos()
    erros = []
    for modelo in candidatos:
        try:
            texto = await _gemini_tentar(modelo, instrucao, historico)
            if _modelo_ok != modelo:
                print(f"[ia] usando modelo {modelo!r}")
            _modelo_ok = modelo
            return texto
        except Exception as e:
            erros.append(f"{modelo}: {e}")
            _modelo_ok = None          # não fixa um modelo que falhou
            continue
    raise RuntimeError("nenhum modelo respondeu -> " + " | ".join(erros)[:500])


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
    return (txt or None), escalar
