"""Motor del chatbot FAQ de The Sharp Team.

Responde preguntas libres en español o inglés por similitud semántica
TF-IDF sobre data/faq.json. Sin APIs externas, sin costo.

Uso:
    from faq_bot import get_bot
    answer, matched_id, lang = get_bot().answer("¿Cuánto cuesta el VIP?")
"""
import json
import math
import os
import random
import re
from unicodedata import normalize as _u_norm, combining as _u_comb

BASE = os.path.dirname(os.path.abspath(__file__))
FAQ_PATH = os.path.join(BASE, "data", "faq.json")

# variante -> término canónico (bilingüe)
_CANON = {
    "precio": "price", "precios": "price", "cuesta": "price", "cuestan": "price",
    "cost": "price", "costs": "price", "vale": "price", "valen": "price",
    "pagar": "price", "pago": "price", "pagos": "price", "payment": "price",
    "cuanto": "price", "cuánto": "price",
    "gratis": "free", "gratuito": "free", "gratuita": "free",
    "jugada": "play", "jugadas": "play", "pick": "play", "picks": "play",
    "pronostico": "play", "pronosticos": "play", "plays": "play",
    "apuesta": "bet", "apuestas": "bet", "apostar": "bet", "apuesto": "bet",
    "betting": "bet", "bets": "bet",
    "cancela": "cancel", "cancelar": "cancel", "cancelacion": "cancel",
    "cancels": "cancel", "cancelled": "cancel", "cancelling": "cancel",
    "unsubscribe": "cancel",
    "prueba": "trial", "pruebas": "trial",
    "semana": "week", "semanas": "week", "weekly": "week",
    "tarjeta": "card", "tarjetas": "card",
    "cuenta": "account", "cuentas": "account", "cuentas": "account",
    "canal": "channel", "canales": "channel", "channels": "channel",
    "dinero": "money", "real": "money",
    "historial": "record", "records": "record",
    "ganar": "win", "gano": "win", "ganas": "win", "gana": "win",
    "wins": "win", "winning": "win",
    "perder": "lose", "pierdo": "lose", "pierde": "lose", "pierden": "lose",
    "losing": "lose", "loses": "lose", "loss": "lose", "losses": "lose",
    "perdedora": "lose", "perdedor": "lose",
    "ganancia": "profit", "ganancias": "profit", "profits": "profit",
    "profitable": "profit", "rentable": "profit", "rentabilidad": "profit",
    "registro": "join", "registrar": "join", "registrarme": "join",
    "unirse": "join", "unirme": "join", "signup": "join", "register": "join",
    "herramienta": "tool", "herramientas": "tool", "tools": "tool",
    "curso": "masterclass", "cursos": "masterclass", "course": "masterclass",
    "leccion": "masterclass", "lecciones": "masterclass", "lessons": "masterclass",
    "modelo": "model", "eligen": "model",
    "ventaja": "edge", "edges": "edge",
    "deporte": "sport", "deportes": "sport", "sports": "sport",
    "liga": "sport", "ligas": "sport", "league": "sport",
    "seguro": "safe", "segura": "safe", "safety": "safe",
    "responsable": "safe", "responsible": "safe",
    "ayuda": "help",
    "hoy": "today",
    "comprobante": "ticket", "comprobantes": "ticket", "boleto": "ticket",
    "miembro": "member", "miembros": "member", "membership": "member",
    "membresia": "member",
    "entrar": "login", "ingresar": "login",
    "cuota": "odds", "cuotas": "odds", "linea": "odds", "lineas": "odds",
    "line": "odds", "lines": "odds", "momio": "odds", "momios": "odds",
    "vig": "novig",
    "tablero": "dashboard",
    "regalan": "whyfree", "regala": "whyfree",
    "verdad": "really",
    "programa": "program", "programas": "program",
    "funciona": "work", "funcionar": "work", "funcionamiento": "work",
    "sistema": "system", "systems": "system",
    "seguimiento": "track", "rastreo": "track", "llevar": "track",
    "sigo": "track", "sigue": "track",
}

_STOP = {
    # español
    "que", "de", "la", "el", "en", "y", "los", "las", "un", "una", "con",
    "por", "para", "se", "su", "sus", "al", "del", "es", "son", "como",
    "esta", "este", "esto", "estan", "hay", "muy", "pero", "sin", "sobre",
    "donde", "cuando", "porque", "tambien", "más", "mas", "tan", "entre",
    "hasta", "desde", "todo", "todos", "cada", "otro", "otra", "ese", "esa",
    "esto", "estos", "mi", "mis", "tu", "tus", "lo", "le", "les", "nos",
    "me", "te", "si", "sí", "no",
    # inglés
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "is", "are",
    "do", "does", "you", "your", "with", "for", "this", "that", "it", "i",
    "me", "my", "we", "our", "be", "as", "at", "by", "from", "what", "how",
    "when", "where", "which", "who", "can", "will", "have", "has", "if",
    "any", "about", "there", "their", "they", "them", "so", "than", "then",
    "too", "very", "just", "like", "get", "got", "was", "were", "been",
    "are", "am",
}

_ES_MARK = {
    "que", "como", "donde", "cuanto", "cuesta", "cuestan", "gratis", "hola",
    "gracias", "porque", "quiero", "tengo", "hay", "esta", "este", "esto",
    "para", "pero", "cuando", "favor", "vale", "valen", "puedo", "tienen",
    "tiene", "hace", "hacen", "dia", "dias", "usted", "ustedes", "soy",
    "eres", "somos", "estoy", "estan", "fue", "eran", "han", "he",
}

_EN_MARK = {
    "the", "what", "how", "much", "does", "is", "are", "do", "you", "your",
    "with", "for", "and", "this", "that", "have", "has", "can", "will",
    "about", "there", "their", "they", "them", "from", "would", "should",
    "could", "many", "does", "did", "was", "were", "an",
}

_GREETING = {
    "hola", "hi", "hello", "hey", "buenas", "buenos", "dias", "tardes",
    "noches", "saludos", "buenosdias", "buenastardes",
}


def _tokens(text):
    t = _u_norm("NFKD", (text or "").lower())
    t = "".join(c for c in t if not _u_comb(c))
    out = []
    for w in re.findall(r"[a-z0-9$]+", t):
        if len(w) < 2 or w in _STOP:
            continue
        out.append(_CANON.get(w, w))
    return out


class FaqBot:
    def __init__(self, path=FAQ_PATH):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        self.items = data.get("items", [])
        self.fallback = {
            "en": data.get("fallback_en", "Sorry, I didn't get that."),
            "es": data.get("fallback_es", "Perdón, no entendí."),
        }
        docs = []
        for it in self.items:
            parts = []
            for key in ("q_en", "a_en", "q_es", "a_es", "keywords"):
                v = it.get(key, "")
                parts.extend(v if isinstance(v, list) else [v])
            parts.append(it.get("keywords", ""))
            docs.append(_tokens(" ".join(parts)))
        n_docs = len(docs)
        df = {}
        for toks in docs:
            for w in set(toks):
                df[w] = df.get(w, 0) + 1
        self.idf = {w: math.log(n_docs / max(1, c)) for w, c in df.items()}
        # tokens de las preguntas canónicas (para el atajo de coincidencia directa)
        self.q_toks = [
            set(_tokens(it.get("q_en", "") + " " + it.get("q_es", "")))
            for it in self.items
        ]
        self.doc_vecs = []
        for toks in docs:
            tf = {}
            for w in toks:
                tf[w] = tf.get(w, 0) + 1
            vec = {
                w: (1.0 + math.log(c)) * self.idf[w]
                for w, c in tf.items() if w in self.idf
            }
            norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
            self.doc_vecs.append({w: v / norm for w, v in vec.items()})

    def lang(self, text):
        raw = (text or "").lower()
        es = sum(1 for w in re.findall(r"[a-z]+", raw) if w in _ES_MARK)
        en = sum(1 for w in re.findall(r"[a-z]+", raw) if w in _EN_MARK)
        if re.search(r"[áéíóúñ¿¡]", raw):
            es += 2
        return "es" if es > en else "en"

    def _pick(self, item, lang):
        """Elige una variante al azar para que no suene robótico."""
        variants = item["a_es"] if lang == "es" else item["a_en"]
        if isinstance(variants, list) and variants:
            return random.choice(variants)
        return variants if isinstance(variants, str) else self.fallback[lang]

    def answer(self, question, threshold=0.10):
        q = (question or "").strip()[:300]
        if not q:
            return self.fallback["en"], None, "en"
        lang = self.lang(q)
        toks = _tokens(q)
        if not toks:
            # "¿qué es esto?" / "what is this?" — casi no deja tokens útiles
            raw_words = set(re.findall(r"[a-z]+", _u_norm("NFKD", q.lower())))
            if raw_words & {"esto", "eso", "this", "that"}:
                w = next((it for it in self.items if it.get("id") == "what-is"), None)
                if w:
                    return self._pick(w, lang), "what-is", lang
            return self.fallback[lang], None, lang
        if set(toks) <= _GREETING:
            g = next((it for it in self.items if it.get("id") == "greeting"), None)
            if g:
                return self._pick(g, lang), "greeting", lang
        # atajo: la pregunta calza dentro de la pregunta canónica de un tema
        # ("What do I get for free?" -> {free} ⊆ {free} del tema "free")
        qset = set(toks)
        best_pre, best_ratio = None, 0.0
        for it, qt in zip(self.items, self.q_toks):
            if qset and qset <= qt:
                ratio = len(qset) / len(qt)
                if ratio > best_ratio:
                    best_pre, best_ratio = it, ratio
        if best_pre is not None:
            return self._pick(best_pre, lang), best_pre.get("id"), lang
        tf = {}
        for w in toks:
            tf[w] = tf.get(w, 0) + 1
        qv = {w: (1.0 + math.log(c)) * self.idf.get(w, 0.0) for w, c in tf.items()}
        norm = math.sqrt(sum(v * v for v in qv.values())) or 1.0
        qv = {w: v / norm for w, v in qv.items()}
        best, best_s = None, 0.0
        for it, dv in zip(self.items, self.doc_vecs):
            s = sum(qv.get(w, 0.0) * dv.get(w, 0.0) for w in qv)
            if s > best_s:
                best, best_s = it, s
        if best is not None and best_s >= threshold:
            return self._pick(best, lang), best.get("id"), lang
        return self.fallback[lang], None, lang


_bot = None


def get_bot():
    global _bot
    if _bot is None:
        _bot = FaqBot()
    return _bot
