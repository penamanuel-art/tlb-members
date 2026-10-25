"""Módulo único del gate de publicación — ENTORNO AISLADO DE PRUEBAS.

Este archivo es el candidato a `gate_publicacion.py` junto a `app.py` en
el repo (I1). NO está en el repo real: vive en el entorno aislado de
pruebas hasta autorización expresa para crearlo en producción. No
modifica producción, Render, la BD de producción ni la rama master.

Reglas que implementa (decisiones de Alex, no inventos):
- D1: la aprobación de modelos se consulta en `ctx["modelos_aprobados"]`
  (dict liga->bool, cargado desde la tabla en producción; en pruebas,
  desde la BD SQLite de pruebas). Vacío = ninguna liga aprobada.
- D2: tope de stake NO EVALUADO (sin política aprobada). No se inventa
  ningún límite. Solo se verifica la matemática ($ = u x $100).
- D3: lista de casas accesibles NO EVALUADA (sin confirmación). No se
  inventa ninguna lista. La casa no afecta al veredicto.
- D6: excepciones solo para fallos de umbral, solo con justificación +
  autorizado_por + no usada. Nunca para bloqueos duros. El marcado de
  "usada" lo hace quien publica, no este módulo.
- D7: fail-closed. Un chequeo habilitado sin su dato de entrada
  BLOQUEA (no aprueba). Lo único que no bloquea es lo explícitamente
  desactivado por D2/D3, que queda registrado como no evaluado.
- D4: el modo lo aplica quien llama (ver `veredicto_para_modo`):
  "aviso" registra sin bloquear ni alterar publicaciones; "enforce"
  actúa sobre el veredicto. Este módulo siempre calcula el veredicto
  estricto; no publica nada por sí mismo.
- Frescura: `ctx` debe traer `max_antiguedad_datos_dias` y
  `max_antiguedad_cuota_min`. En pruebas se usan los valores
  propuestos (7 días / 60 min), NO aprobados como reglas: cada
  veredicto lleva la marca `frescura_aprobada` de `ctx` para que
  ningún veredicto provisional se confunda con uno bajo reglas
  aprobadas. La activación de `enforce` en producción exige parámetros
  de frescura aprobados (decisión pendiente, D11).

Portado una vez desde las pruebas (formulas.py + gate.py): mismas
fórmulas, mismas tolerancias (0.05 puntos, 1 centavo de fair), misma
semántica de duplicado (excluye el mismo id: publicado -> liquidado es
ciclo de vida, no duplicado). Divergencias INTENCIONADAS respecto al
gate de pruebas, por D2/D3: sin chequeo de tope de stake y sin chequeo
de casa accesible (ambos quedan en `no_evaluado`).

Horas: acepta "19:08" (24h, formato de plays.json) y "7:08 PM ET"
(formato de archive.json). Sin hora parseable, la vigencia queda sin
verificar y, por fail-closed, bloquea.
"""

TOL_PUNTOS = 0.05
TOL_FAIR = 1


# ---------- Fórmulas (idénticas a las validadas en pruebas) ----------

def prob_implicita(cuota):
    if cuota == 0:
        raise ValueError("cuota americana 0 no válida")
    if cuota < 0:
        return abs(cuota) / (abs(cuota) + 100.0) * 100.0
    return 100.0 / (cuota + 100.0) * 100.0


def americana_a_decimal(cuota):
    if cuota > 0:
        return 1.0 + cuota / 100.0
    return 1.0 + 100.0 / abs(cuota)


def edge_modelo_puntos(prob_puntos, cuota):
    return prob_puntos - prob_implicita(cuota)


def ev_modelo_pct(prob_puntos, cuota):
    return (prob_puntos / 100.0 * americana_a_decimal(cuota) - 1.0) * 100.0


def precio_justo_modelo(prob_puntos):
    if prob_puntos <= 0 or prob_puntos >= 100:
        raise ValueError("probabilidad fuera de (0, 100)")
    if prob_puntos >= 50:
        return -round(prob_puntos / (100.0 - prob_puntos) * 100.0)
    return round((100.0 - prob_puntos) / prob_puntos * 100.0)


def evaluar_pick(prob_puntos, cuota):
    return {
        "prob_modelo_puntos": prob_puntos,
        "prob_implicita_puntos": prob_implicita(cuota),
        "edge_modelo_puntos": edge_modelo_puntos(prob_puntos, cuota),
        "ev_modelo_pct": ev_modelo_pct(prob_puntos, cuota),
        "precio_justo_modelo": precio_justo_modelo(prob_puntos),
        "decimal": americana_a_decimal(cuota),
    }


# ---------- Utilidades ----------

def _fair_a_int(s):
    try:
        return int(str(s).strip().replace("+", "").replace("%", ""))
    except Exception:
        return None


def _edge_a_num(v):
    """Edge declarado: número (plays.json/writeup) o string '+9.7%'
    (archive.json, donde el % es la etiqueta vieja mal puesta: el valor
    son puntos, no porcentaje de EV). Devuelve el número o None."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).strip().replace("+", "").replace("%", ""))
    except Exception:
        return None


def _parse_hora(fecha, txt):
    """Devuelve datetime ingenuo (ET) o None. Acepta '19:08' y
    '7:08 PM ET' / '7:08 PM'."""
    from datetime import datetime
    if not txt:
        return None
    t = str(txt).strip().replace(" ET", "").replace("ET", "").strip()
    try:
        y, m, d = int(fecha[:4]), int(fecha[5:7]), int(fecha[8:10])
    except Exception:
        return None
    try:
        if "AM" in t or "PM" in t:
            dt = datetime.strptime(t, "%I:%M %p")
            return datetime(y, m, d, dt.hour, dt.minute)
        h, mi = t.split(":")[:2]
        return datetime(y, m, d, int(h), int(mi))
    except Exception:
        return None


def _pick(play):
    return play.get("pick") or play.get("seleccion") or ""


def _mercado(play):
    return play.get("mercado") or play.get("bettype") or ""


def _prob_modelo(play):
    w = play.get("writeup") or {}
    if isinstance(w, dict) and w.get("model_prob") is not None:
        return w.get("model_prob")
    return play.get("prob_modelo")


def _declarados(play):
    """Valores declarados para coherencia: del writeup o de la jugada."""
    w = play.get("writeup") or {}
    if not isinstance(w, dict):
        w = {}
    return {
        "implicita": w.get("implied_prob", play.get("implicita_declarada")),
        "edge": _edge_a_num(w.get("edge", play.get("edge_declarado"))),
        "fair": _fair_a_int(w.get("fair_price", play.get("fair_declarado")))
        if (w.get("fair_price") is not None or
                play.get("fair_declarado") is not None) else None,
    }


# ---------- Evaluación estricta ----------

def evaluar_candidata(cand, ctx, ahora):
    """Veredicto estricto. No publica, no escribe, no decide modo.
    ctx: modelos_aprobados {liga: bool}, gates {liga: float},
    publicadas [plays.json], archivo [archive aplanado],
    excepciones [{play_id, justificacion, autorizado_por, usado}],
    max_antiguedad_datos_dias, max_antiguedad_cuota_min,
    frescura_aprobada (bool, marca informativa),
    fecha (día del programa, cuando la jugada no trae fecha propia),
    excluir_propio_id (bool, solo para re-evaluar publicadas)."""
    motivos, no_evaluado, checks = [], [], []

    def reg(nombre, estado, detalle):
        checks.append({"check": nombre, "estado": estado, "detalle": detalle})

    def bloquear(motivo):
        motivos.append(motivo)

    pid = cand.get("id")
    # Las jugadas de plays.json/pending no traen fecha por jugada (la fecha
    # va en la raíz del archivo): quien llama aporta el día del programa
    # en ctx["fecha"]. Sin día, la hora no se puede verificar (fail-closed).
    fecha = cand.get("fecha") or ctx.get("fecha") or ""
    prob = _prob_modelo(cand)

    # 1. Completitud (fail-closed: lo que falta, bloquea).
    requeridos = ["liga", "mercado", "cuota", "casa", "nivel",
                  "stake_monto", "stake_unidades"]
    faltan = [c for c in requeridos if cand.get(c) in (None, "")]
    if not _pick(cand):
        faltan.append("pick")
    if not (cand.get("game_time_et") or cand.get("hora")):
        faltan.append("hora")
    if prob is None:
        faltan.append("prob_modelo")
    if cand.get("datos_fecha") is None:
        faltan.append("datos_fecha")
    if cand.get("cuota_observada_en") is None:
        faltan.append("cuota_observada_en")
    if faltan:
        reg("completitud", "FALLA", "faltan: " + ", ".join(faltan))
        bloquear("dato incompleto: " + ", ".join(faltan))
    else:
        reg("completitud", "PASA", "campos presentes")

    # 2. Coherencia básica.
    if prob is not None and not (0 < prob < 100):
        reg("prob_rango", "FALLA", "prob_modelo fuera de (0, 100)")
        bloquear("prob_modelo fuera de (0, 100)")
    if cand.get("cuota") == 0:
        reg("cuota_valida", "FALLA", "cuota 0 no válida")
        bloquear("cuota 0 no válida")
    if (cand.get("stake_monto") is not None and cand["stake_monto"] < 0) or \
            (cand.get("stake_unidades") is not None and
             cand["stake_unidades"] < 0):
        reg("stake_negativo", "FALLA", "stake negativo")
        bloquear("stake negativo")

    # 3. Modelo aprobado (D1: tabla, vacía = ninguna aprobada).
    if not ctx.get("modelos_aprobados", {}).get(cand.get("liga"), False):
        reg("modelo_aprobado", "FALLA",
            f"sin modelo aprobado para {cand.get('liga')}")
        bloquear(f"sin modelo aprobado para {cand.get('liga')}")

    # 4. Frescura de datos (parámetros propuestos si no aprobados).
    df = cand.get("datos_fecha")
    if df is not None:
        try:
            from datetime import datetime as _dt
            fd = _dt.fromisoformat(str(df)[:10])
            edad = (ahora - fd).days
            if edad > ctx.get("max_antiguedad_datos_dias", 7):
                reg("datos_frescos", "FALLA", f"datos de hace {edad} días")
                bloquear(f"datos desactualizados: {edad} días")
            else:
                reg("datos_frescos", "PASA", f"datos de hace {edad} día(s)")
        except Exception:
            reg("datos_frescos", "FALLA", "datos_fecha no parseable")
            bloquear("datos_fecha no parseable")

    # 5. Vigencia y frescura de cuota.
    inicio = _parse_hora(fecha, cand.get("game_time_et") or cand.get("hora"))
    if inicio is None:
        reg("cuota_vigente", "FALLA", "hora de inicio no parseable")
        bloquear("hora de inicio no parseable")
    elif ahora >= inicio:
        reg("cuota_vigente", "FALLA", "el juego ya empezó")
        bloquear("cuota vencida: el juego ya empezó")
    else:
        reg("cuota_vigente", "PASA", f"inicio {inicio.strftime('%H:%M')}")
    coe = cand.get("cuota_observada_en")
    if coe is not None:
        try:
            from datetime import datetime as _dt
            co = _dt.fromisoformat(str(coe)) if isinstance(coe, str) else coe
            edad_min = (ahora - co).total_seconds() / 60.0
            if edad_min > ctx.get("max_antiguedad_cuota_min", 60):
                reg("cuota_fresca", "FALLA",
                    f"cuota de hace {edad_min:.0f} min")
                bloquear(f"cuota desactualizada: {edad_min:.0f} min")
            else:
                reg("cuota_fresca", "PASA", f"cuota de hace {edad_min:.0f} min")
        except Exception:
            reg("cuota_fresca", "FALLA", "cuota_observada_en no parseable")
            bloquear("cuota_observada_en no parseable")

    # 6. Duplicados. Por defecto, que el id ya exista en publicadas o en
    # archivo ES duplicado (la jugada ya se publicó). Solo cuando quien
    # llama re-evalúa una jugada ya publicada (aviso sobre publicadas)
    # pasa excluir_propio_id=True, y entonces solo se marca si el id
    # aparece 2+ veces en la misma fuente (anomalía de datos).
    dup = []
    for fuente, lista in (("publicadas", ctx.get("publicadas", [])),
                          ("archivo", ctx.get("archivo", []))):
        vistos = 0
        for o in lista:
            if o.get("id") == pid:
                vistos += 1
                continue
            if (o.get("liga", cand.get("liga")) == cand.get("liga")
                    and _pick(o) == _pick(cand)
                    and _mercado(o) == _mercado(cand)
                    and o.get("fecha", fecha) == fecha):
                dup.append(f"{fuente}: {_pick(o)}")
        if vistos >= 1 and not ctx.get("excluir_propio_id", False):
            dup.append(f"{fuente}: id {pid} ya publicado")
        elif vistos > 1:
            dup.append(f"{fuente}: id repetido {vistos} veces")
    if dup:
        reg("duplicado", "FALLA", "; ".join(dup))
        bloquear("duplicado: " + "; ".join(dup))
    else:
        reg("duplicado", "PASA", "sin duplicado")

    # 7. Coherencia con lo declarado + métricas (fuente única).
    metricas = None
    if prob is not None and cand.get("cuota") not in (None, 0):
        try:
            metricas = evaluar_pick(prob, cand["cuota"])
        except Exception as e:
            reg("metricas", "FALLA", f"no calculable: {e}")
            bloquear(f"métricas no calculables: {e}")
    if metricas is not None:
        dec = _declarados(cand)
        inc = []
        if dec["implicita"] is not None and \
                abs(metricas["prob_implicita_puntos"] - dec["implicita"]) > TOL_PUNTOS:
            inc.append("implícita")
        if dec["edge"] is not None and \
                abs(metricas["edge_modelo_puntos"] - dec["edge"]) > TOL_PUNTOS:
            inc.append("edge")
        if dec["fair"] is not None and \
                abs(metricas["precio_justo_modelo"] - dec["fair"]) > TOL_FAIR:
            inc.append("fair")
        if inc:
            reg("coherencia", "FALLA", "difiere: " + ", ".join(inc))
            bloquear("incoherente: " + ", ".join(inc))
        else:
            reg("coherencia", "PASA",
                f"edge {metricas['edge_modelo_puntos']:.2f} pts")

    # 8. Stake: matemática sí, tope NO (D2: sin política aprobada).
    if cand.get("stake_monto") is not None and \
            cand.get("stake_unidades") is not None:
        if abs(cand["stake_monto"] - cand["stake_unidades"] * 100) > 0.01:
            reg("stake_matematica", "FALLA",
                f"${cand['stake_monto']} != {cand['stake_unidades']}u x $100")
            bloquear("stake incoherente")
        else:
            reg("stake_matematica", "PASA", "matemática correcta")
    no_evaluado.append("stake_tope: política sin aprobar (D2, no se inventa)")
    reg("stake_tope", "NO_EVALUADO", "política sin aprobar (D2)")

    # 9. Casa accesible: NO (D3: sin confirmación).
    no_evaluado.append("casa_accesible: lista sin confirmar (D3, no se inventa)")
    reg("casa_accesible", "NO_EVALUADO", "lista sin confirmar (D3)")

    # 10. Umbral de la doctrina (regla existente, no nueva).
    gate = ctx.get("gates", {}).get(cand.get("liga"))
    fallo_umbral = False
    if metricas is not None and gate is not None:
        if metricas["edge_modelo_puntos"] < gate:
            reg("umbral", "FALLA",
                f"edge {metricas['edge_modelo_puntos']:.2f} < gate {gate}")
            bloquear(f"bajo umbral: edge "
                     f"{metricas['edge_modelo_puntos']:.2f} < gate {gate}")
            fallo_umbral = True
        else:
            reg("umbral", "PASA",
                f"edge {metricas['edge_modelo_puntos']:.2f} >= gate {gate}")
    elif gate is None:
        reg("umbral", "FALLA", f"sin umbral definido para {cand.get('liga')}")
        bloquear(f"sin umbral definido para {cand.get('liga')}")
    else:
        reg("umbral", "FALLA", "sin métricas para evaluar umbral")
        bloquear("sin métricas para evaluar umbral")

    # Veredicto (D6: excepción solo si el ÚNICO fallo es el umbral, con
    # registro válido. La excepción nunca cubre incompletitud, modelo,
    # vigencia, duplicado, coherencia ni ningún otro bloqueo duro).
    fallas = [c["check"] for c in checks if c["estado"] == "FALLA"]
    if not fallas:
        decision, exc = "APROBADA", None
    elif fallas == ["umbral"]:
        exc = _buscar_excepcion(cand, ctx)
        decision = "REVISION_MANUAL" if exc is not None else "BLOQUEADA"
    else:
        decision, exc = "BLOQUEADA", None
    return {
        "id": pid,
        "decision": decision,
        "motivos": motivos,
        "metricas": metricas,
        "checks": checks,
        "no_evaluado": no_evaluado,
        "excepcion": exc,
        "frescura_aprobada": bool(ctx.get("frescura_aprobada", False)),
        "frescura_parametros": {
            "datos_dias": ctx.get("max_antiguedad_datos_dias"),
            "cuota_min": ctx.get("max_antiguedad_cuota_min"),
        },
    }


def _buscar_excepcion(cand, ctx):
    """Excepción válida (D6): justificación + autorizado_por + no usada.
    El autorizado_por debe ser una aprobación real; en pruebas se usa el
    marcador TEST-FIXTURE para que nunca se confunda con una aprobación
    real de Alex."""
    for e in ctx.get("excepciones", []):
        if e.get("play_id") == cand.get("id") and e.get("justificacion") \
                and e.get("autorizado_por") and not e.get("usado", False):
            return e
    return None


def veredicto_para_modo(decision, modo):
    """Aplica el modo (D4) a un veredicto estricto. El módulo no publica;
    quien llama decide con esto.
    - aviso: REGISTRA_Y_PROCEDE (anota habría_sido, no bloquea nada).
    - enforce: APROBADA->PROCEDE, BLOQUEADA->BLOQUEA,
      REVISION_MANUAL->MANUAL (exige aprobación registrada)."""
    if modo == "aviso":
        return "REGISTRA_Y_PROCEDE"
    if decision == "APROBADA":
        return "PROCEDE"
    if decision == "BLOQUEADA":
        return "BLOQUEA"
    return "MANUAL"
