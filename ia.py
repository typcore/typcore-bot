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
GEMINI_MODELO = os.getenv("GEMINI_MODELO", "gemini-2.0-flash")
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
async def _gemini(instrucao: str, historico: list) -> str:
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{GEMINI_MODELO}:generateContent?key={GEMINI_KEY}")
    payload = {
        "system_instruction": {"parts": [{"text": instrucao}]},
        "contents": [
            {"role": ("user" if m["quem"] == "cliente" else "model"),
             "parts": [{"text": m["texto"]}]}
            for m in historico
        ],
        "generationConfig": {"temperature": 0.6, "maxOutputTokens": 400},
    }
    async with httpx.AsyncClient(timeout=TIMEOUT_IA) as c:
        r = await c.post(url, json=payload)
        r.raise_for_status()
        d = r.json()
    return d["candidates"][0]["content"]["parts"][0]["text"].strip()


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
        print(f"[ia] falhou ({type(e).__name__}: {e}) — caindo no menu")
        return None, False

    if not txt:
        return None, False

    escalar = "[ESCALAR]" in txt
    txt = txt.replace("[ESCALAR]", "").strip()
    return (txt or None), escalar
