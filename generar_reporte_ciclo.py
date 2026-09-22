#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Generador del artefacto de desempeño del equipo por ciclo — SaludMía (Linear).

Consulta la API de Linear (o un snapshot JSON), calcula las métricas del ciclo
activo y produce un dashboard HTML autocontenido (sin CDN, sin dependencias de
render). Diseñado para regenerarse cada fin de ciclo (~15 días) con ~0 tokens de
modelo: todo el cálculo y el render los hace Python.

Uso:
    # Desde la API de Linear (requiere variable de entorno LINEAR_API_KEY):
    python generar_reporte_ciclo.py

    # Desde un snapshot JSON (no requiere API key, útil para pruebas):
    python generar_reporte_ciclo.py --datos datos/ciclo_23.json

    # Elegir carpeta de salida:
    python generar_reporte_ciclo.py --salida reportes

La Personal API Key se obtiene en Linear:
    Settings -> Security & access -> Personal API keys -> New key
Luego, en PowerShell (persistente):  setx LINEAR_API_KEY "lin_api_xxx"
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone, date

LINEAR_API_URL = "https://api.linear.app/graphql"
# Clave del equipo en Linear (prefijo de los issues, p.ej. SAL-228), NO el nombre.
TEAM_KEY = "SAL"

# ---------------------------------------------------------------------------
# 1. Obtención de datos
# ---------------------------------------------------------------------------

GRAPHQL_QUERY = """
query CicloActivo($teamKey: String!) {
  teams(first: 1, filter: { key: { eq: $teamKey } }) {
    nodes {
      id
      name
      activeCycle {
        number
        startsAt
        endsAt
        completedIssueCountHistory
        issueCountHistory
        issues(first: 100) {
          nodes {
            identifier
            title
            estimate
            createdAt
            startedAt
            completedAt
            dueDate
            updatedAt
            state { name type }
            assignee { name }
            project { name }
            parent { identifier }
            labels(first: 10) { nodes { name parent { name } } }
          }
        }
      }
      members { nodes { name active } }
    }
  }
}
"""

# Consulta aparte para los últimos project updates del equipo. Se separa de la
# consulta principal porque anidarla bajo el ciclo (250 issues) dispara el límite
# de complejidad de Linear ("Query too complex"). Aquí `projects` es raíz y filtra
# por equipo, con complejidad baja.
PROJECT_UPDATES_QUERY = """
query ProjectUpdates($teamKey: String!) {
  projects(first: 50, filter: { accessibleTeams: { some: { key: { eq: $teamKey } } } }) {
    nodes {
      name
      projectUpdates(first: 3) {
        nodes { body health createdAt user { name } }
      }
    }
  }
}
"""


def _graphql(api_key, query, variables):
    """Ejecuta una consulta GraphQL contra Linear y devuelve el bloque `data`."""
    import urllib.request
    import urllib.error

    payload = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    req = urllib.request.Request(
        LINEAR_API_URL,
        data=payload,
        headers={"Authorization": api_key, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        raise RuntimeError(f"Linear API HTTP {e.code}: {detail}") from None

    if "errors" in body:
        raise RuntimeError("Linear API error: " + json.dumps(body["errors"]))
    return body["data"]


def fetch_project_updates(api_key):
    """Devuelve {nombre_proyecto: {body, health, createdAt, author}} con el update más reciente."""
    data = _graphql(api_key, PROJECT_UPDATES_QUERY, {"teamKey": TEAM_KEY})
    out = {}
    for proj in data.get("projects", {}).get("nodes", []):
        ups = proj.get("projectUpdates", {}).get("nodes", [])
        if not ups:
            continue
        latest = max(ups, key=lambda u: u.get("createdAt") or "")
        out[proj["name"]] = {
            "body": latest.get("body") or "",
            "health": latest.get("health") or "onTrack",
            "createdAt": latest.get("createdAt"),
            "author": (latest.get("user") or {}).get("name") or "—",
        }
    return out


def fetch_from_linear(api_key):
    """Consulta la API GraphQL de Linear y normaliza la respuesta."""
    data = _graphql(api_key, GRAPHQL_QUERY, {"teamKey": TEAM_KEY})

    teams = data["teams"]["nodes"]
    if not teams:
        raise RuntimeError(f"No se encontró el equipo con key '{TEAM_KEY}'.")
    team = teams[0]
    cycle = team.get("activeCycle")
    if not cycle:
        raise RuntimeError("El equipo no tiene un ciclo activo.")

    roster = [m["name"] for m in team["members"]["nodes"] if m.get("active")]

    issues = []
    for n in cycle["issues"]["nodes"]:
        labels = [
            {"name": l["name"], "parent": (l.get("parent") or {}).get("name")}
            for l in n["labels"]["nodes"]
        ]
        issues.append(
            {
                "id": n["identifier"],
                "title": n["title"],
                "estimate": n.get("estimate"),
                "createdAt": n.get("createdAt"),
                "startedAt": n.get("startedAt"),
                "completedAt": n.get("completedAt"),
                "dueDate": n.get("dueDate"),
                "updatedAt": n.get("updatedAt"),
                "stateName": n["state"]["name"],
                "stateType": n["state"]["type"],
                "assignee": (n.get("assignee") or {}).get("name"),
                "project": (n.get("project") or {}).get("name"),
                "parentId": (n.get("parent") or {}).get("identifier"),
                "labels": labels,
            }
        )

    # Último project update por proyecto — consulta aparte (evita el límite de
    # complejidad al anidarlo bajo el ciclo). Si falla, no bloquea el reporte.
    try:
        project_updates = fetch_project_updates(api_key)
    except Exception as e:  # noqa: BLE001
        print(f"[!] No se pudieron leer los project updates: {e}")
        project_updates = {}

    return {
        "team": team["name"],
        "roster": roster,
        "cycle": {
            "number": cycle["number"],
            "startsAt": cycle["startsAt"],
            "endsAt": cycle["endsAt"],
            "completedIssueCountHistory": cycle["completedIssueCountHistory"],
            "issueCountHistory": cycle["issueCountHistory"],
        },
        "issues": issues,
        "project_updates": project_updates,
    }


def load_snapshot(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# 2. Utilidades de fecha
# ---------------------------------------------------------------------------

def parse_dt(s):
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        # dueDate suele venir como 'YYYY-MM-DD'
        return datetime.fromisoformat(s + "T00:00:00+00:00")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def as_date(s):
    dt = parse_dt(s)
    return dt.date() if dt else None


# ---------------------------------------------------------------------------
# 3. Cálculo de métricas (funciones puras)
# ---------------------------------------------------------------------------

def compute_metrics(data, today=None):
    cycle = data["cycle"]
    issues = data["issues"]
    roster = set(data.get("roster", []))

    starts = parse_dt(cycle["startsAt"])
    ends = parse_dt(cycle["endsAt"])
    first_day = starts.date()
    if today is None:
        today = datetime.now(timezone.utc)
    total_days = max(1, (ends.date() - starts.date()).days)
    elapsed_days = max(0, min(total_days, (today.date() - starts.date()).days))
    remaining_days = max(0, (ends.date() - today.date()).days)

    # --- 1. Avance del ciclo ---
    scope = len(issues)
    by_type = {"completed": 0, "started": 0, "unstarted": 0, "canceled": 0,
               "backlog": 0, "triage": 0}
    for it in issues:
        by_type[it["stateType"]] = by_type.get(it["stateType"], 0) + 1
    completed = by_type["completed"]
    pct = (completed / scope * 100) if scope else 0

    # --- 2. Throughput real vs. carryover ---
    real, carryover = [], []
    for it in issues:
        if it["stateType"] != "completed":
            continue
        comp = parse_dt(it["completedAt"])
        if comp is None or comp < starts or comp > ends:
            continue  # completado fuera de la ventana del ciclo
        created = parse_dt(it["createdAt"])
        is_dump = comp.date() == first_day and created is not None and created < starts
        (carryover if is_dump else real).append(it)

    # Cycle time de los completados reales (solo con startedAt registrado)
    cycle_times = []
    for it in real:
        st = parse_dt(it["startedAt"])
        comp = parse_dt(it["completedAt"])
        if st and comp:
            cycle_times.append((comp - st).total_seconds() / 86400.0)
    cycle_times.sort()

    def median(xs):
        if not xs:
            return None
        n = len(xs)
        return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2

    # WIP estancado: en curso iniciado antes de que arrancara el ciclo
    aging = []
    for it in issues:
        if it["stateType"] != "started":
            continue
        st = parse_dt(it["startedAt"])
        if st and st < starts:
            dias = (today - st).days
            aging.append({**it, "dias_en_curso": dias})
    aging.sort(key=lambda x: x["dias_en_curso"], reverse=True)

    # --- 3. Carga por persona ---
    people = {}
    for it in issues:
        name = it["assignee"] or "Sin asignar"
        p = people.setdefault(
            name, {"name": name, "total": 0, "completed": 0,
                   "started": 0, "unstarted": 0, "otro": 0,
                   "roster": (name in roster) or name == "Sin asignar"}
        )
        p["total"] += 1
        t = it["stateType"]
        if t == "completed":
            p["completed"] += 1
        elif t == "started":
            p["started"] += 1
        elif t == "unstarted":
            p["unstarted"] += 1
        else:
            p["otro"] += 1
    people = sorted(people.values(), key=lambda x: x["total"], reverse=True)

    # --- 4. Salud de datos / proceso ---
    def has_tipo(it):
        return any(l.get("parent") == "tipo" for l in it["labels"])

    con_estimacion = sum(1 for it in issues if it.get("estimate"))
    con_tipo = sum(1 for it in issues if has_tipo(it))
    sin_asignar = sum(1 for it in issues if not it["assignee"])
    completadas = [it for it in issues if it["stateType"] == "completed"]
    comp_sin_inicio = sum(1 for it in completadas if not it["startedAt"])

    salud = [
        {"label": "Con estimación", "num": con_estimacion, "den": scope,
         "sentido": "alto"},
        {"label": "Con etiqueta de tipo (Doble Carril)", "num": con_tipo,
         "den": scope, "sentido": "alto"},
        {"label": "Sin asignar", "num": sin_asignar, "den": scope,
         "sentido": "bajo"},
        {"label": "Completadas sin fecha de inicio", "num": comp_sin_inicio,
         "den": max(1, len(completadas)), "sentido": "bajo"},
    ]

    # Issues en riesgo (para la tabla): WIP viejo, sin estimar en curso, vencidos
    hoy_d = today.date()
    en_riesgo = []
    for it in issues:
        motivos = []
        st = parse_dt(it["startedAt"])
        if it["stateType"] == "started" and st and st < starts:
            motivos.append(f"En curso {(today - st).days} d")
        due = as_date(it["dueDate"])
        if due and it["stateType"] not in ("completed", "canceled") and due < hoy_d:
            motivos.append(f"Vencido ({it['dueDate']})")
        if it["stateType"] in ("started", "unstarted") and not it.get("estimate") \
                and not any(l.get("parent") == "caja" for l in it["labels"]):
            motivos.append("Sin estimar/caja")
        if motivos:
            en_riesgo.append({**it, "motivos": motivos})
    # Prioriza los que llevan más tiempo en curso
    en_riesgo.sort(
        key=lambda x: parse_dt(x["startedAt"]) or datetime.max.replace(tzinfo=timezone.utc)
    )

    return {
        "team": data["team"],
        "cycle_number": cycle["number"],
        "starts": starts, "ends": ends,
        "total_days": total_days, "elapsed_days": elapsed_days,
        "remaining_days": remaining_days,
        "scope": scope, "completed": completed,
        "started": by_type["started"], "unstarted": by_type["unstarted"],
        "canceled": by_type["canceled"], "pct": pct,
        "burnup_completed": cycle["completedIssueCountHistory"],
        "burnup_scope": cycle["issueCountHistory"],
        "real": real, "carryover": carryover,
        "cycle_time_median": median(cycle_times),
        "cycle_time_n": len(cycle_times),
        "aging": aging,
        "people": people, "sin_asignar": sin_asignar,
        "salud": salud, "en_riesgo": en_riesgo,
        "generado": today,
    }


# ---------------------------------------------------------------------------
# 4. Render HTML (SVG inline, sin CDN)
# ---------------------------------------------------------------------------

MESES = ["", "ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep",
         "oct", "nov", "dic"]


def fdate(d):
    return f"{d.day} {MESES[d.month]}"


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def svg_burnup(m, w=680, h=260):
    """Línea de burnup: completadas vs scope + línea ideal.

    El eje X abarca la duración completa del ciclo (total_days); la historia
    disponible ocupa los primeros días, de modo que la línea ideal (0 -> alcance
    a lo largo de todo el ciclo) permite leer si el equipo va adelante o atrás.
    """
    pad_l, pad_r, pad_t, pad_b = 34, 14, 14, 26
    comp = m["burnup_completed"] or [0]
    scope_h = m["burnup_scope"] or [m["scope"]]
    n_days = max(m["total_days"], len(comp) - 1, 1)  # tramos en el eje X
    ymax = max(max(scope_h), m["scope"], 1)
    plot_w = w - pad_l - pad_r
    plot_h = h - pad_t - pad_b

    def X(i):
        return pad_l + (plot_w * i / n_days)

    def Y(v):
        return pad_t + plot_h * (1 - v / ymax)

    # rejilla horizontal
    grid = []
    steps = 4
    for s in range(steps + 1):
        v = ymax * s / steps
        y = Y(v)
        grid.append(
            f'<line x1="{pad_l}" y1="{y:.1f}" x2="{w-pad_r}" y2="{y:.1f}" '
            f'class="grid"/>'
            f'<text x="{pad_l-6}" y="{y+3:.1f}" text-anchor="end" '
            f'class="tick">{v:.0f}</text>'
        )

    def path(vals):
        pts = [f"{X(i):.1f},{Y(v):.1f}" for i, v in enumerate(vals)]
        return "M" + " L".join(pts)

    scope_area = " ".join(f"{X(i):.1f},{Y(v):.1f}" for i, v in enumerate(scope_h))
    ideal = (f'M{X(0):.1f},{Y(0):.1f} L{X(n_days):.1f},{Y(m["scope"]):.1f}')

    last_c = comp[-1]
    dots = (f'<circle cx="{X(len(comp)-1):.1f}" cy="{Y(last_c):.1f}" r="4" '
            f'class="dot-c"/>')

    return f'''<svg viewBox="0 0 {w} {h}" role="img" class="chart"
  aria-label="Burnup del ciclo: completadas frente al alcance total">
  {''.join(grid)}
  <polyline points="{scope_area}" class="line-scope"/>
  <path d="{ideal}" class="line-ideal"/>
  <path d="{path(comp)}" class="line-comp"/>
  {dots}
  <text x="{X(len(comp)-1):.1f}" y="{Y(last_c)-9:.1f}" text-anchor="end"
    class="lbl-comp">{last_c} hechas</text>
</svg>'''


def svg_stacked_people(m, w=680, row_h=34):
    """Barras horizontales apiladas por persona (Hecho/En curso/Por hacer)."""
    people = m["people"]
    maxt = max((p["total"] for p in people), default=1)
    pad_l = 128
    bar_w = w - pad_l - 42
    h = len(people) * row_h + 8
    rows = []
    y = 6
    for p in people:
        segs = [("completed", p["completed"]), ("started", p["started"]),
                ("unstarted", p["unstarted"]), ("otro", p["otro"])]
        x = pad_l
        parts = []
        for cls, val in segs:
            if val <= 0:
                continue
            seg_w = bar_w * val / maxt
            parts.append(
                f'<rect x="{x:.1f}" y="{y}" width="{max(0,seg_w-2):.1f}" '
                f'height="18" rx="3" class="seg-{cls}"/>'
            )
            x += seg_w
        tag = "" if p["roster"] else ' <tspan class="off">· inactivo</tspan>'
        rows.append(
            f'<text x="{pad_l-8}" y="{y+13}" text-anchor="end" class="ylbl">'
            f'{esc(p["name"])}{tag}</text>'
            + "".join(parts)
            + f'<text x="{x+6:.1f}" y="{y+13}" class="cnt">{p["total"]}</text>'
        )
        y += row_h
    return f'''<svg viewBox="0 0 {w} {h}" role="img" class="chart"
  aria-label="Carga por persona">{''.join(rows)}</svg>'''


def bar_salud(item):
    pct = item["num"] / item["den"] * 100 if item["den"] else 0
    # sentido "alto" => más es mejor; "bajo" => más es peor
    if item["sentido"] == "alto":
        cls = "good" if pct >= 70 else ("warn" if pct >= 40 else "bad")
    else:
        cls = "good" if pct <= 15 else ("warn" if pct <= 40 else "bad")
    return f'''<div class="meter">
  <div class="meter-top"><span>{esc(item["label"])}</span>
    <b>{pct:.0f}% <span class="frac">({item["num"]}/{item["den"]})</span></b></div>
  <div class="track"><div class="fill {cls}" style="width:{pct:.0f}%"></div></div>
</div>'''


def tabla_riesgo(m):
    if not m["en_riesgo"]:
        return '<p class="empty">Sin issues en riesgo. 🎉</p>'
    rows = []
    for it in m["en_riesgo"][:12]:
        chips = " ".join(f'<span class="chip">{esc(x)}</span>'
                         for x in it["motivos"])
        rows.append(
            f'<tr><td class="mono">{esc(it["id"])}</td>'
            f'<td>{esc(it["title"])}</td>'
            f'<td>{esc(it["assignee"] or "—")}</td>'
            f'<td>{esc(it["stateName"])}</td><td>{chips}</td></tr>'
        )
    return f'''<table class="risk"><thead><tr>
    <th>ID</th><th>Título</th><th>Responsable</th><th>Estado</th><th>Señal</th>
    </tr></thead><tbody>{''.join(rows)}</tbody></table>'''


def render_html(m):
    ct = (f'{m["cycle_time_median"]:.1f} d' if m["cycle_time_median"] is not None
          else "n/d")
    ct_note = (f"mediana de {m['cycle_time_n']} issue(s) con inicio registrado"
               if m["cycle_time_n"] else "sin datos de inicio suficientes")
    real_ids = ", ".join(it["id"] for it in m["real"]) or "—"
    real_n, carry_n = len(m["real"]), len(m["carryover"])
    tp_total = real_n + carry_n
    real_w = (real_n / tp_total * 100) if tp_total else 0

    kpis = [
        ("Alcance", m["scope"], "issues en el ciclo"),
        ("Avance", f'{m["pct"]:.0f}%', f'{m["completed"]} de {m["scope"]} hechas'),
        ("Throughput real", real_n, "completadas dentro del ciclo"),
        ("En curso", m["started"], f'{len(m["aging"])} estancadas'),
        ("Días restantes", m["remaining_days"],
         f'día {m["elapsed_days"]} de {m["total_days"]}'),
    ]
    kpi_html = "".join(
        f'<div class="kpi"><span class="kpi-lbl">{esc(l)}</span>'
        f'<span class="kpi-val">{v}</span>'
        f'<span class="kpi-sub">{esc(s)}</span></div>'
        for l, v, s in kpis
    )

    salud_html = "".join(bar_salud(it) for it in m["salud"])

    aging_html = ""
    if m["aging"]:
        items = "".join(
            f'<li><span class="mono">{esc(a["id"])}</span> {esc(a["title"])} '
            f'<b>{a["dias_en_curso"]} d</b></li>'
            for a in m["aging"][:6]
        )
        aging_html = f'<ul class="aging">{items}</ul>'

    gen = m["generado"].strftime("%Y-%m-%d %H:%M UTC")

    return f'''<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Desempeño Ciclo #{m["cycle_number"]} · {esc(m["team"])}</title>
<style>
:root {{
  --plane:#f9f9f7; --surface:#fcfcfb; --ink:#0b0b0b; --ink2:#52514e;
  --muted:#898781; --grid:#e1e0d9; --axis:#c3c2b7; --border:rgba(11,11,11,.10);
  --s1:#2a78d6; --s-comp:#2a78d6; --s-start:#eb6834; --s-todo:#c3c2b7;
  --s-otro:#898781; --good:#0ca30c; --warn:#eda100; --bad:#d03b3b;
  --ideal:#b7d3f6;
}}
@media (prefers-color-scheme: dark) {{
  :root {{
    --plane:#0d0d0d; --surface:#1a1a19; --ink:#fff; --ink2:#c3c2b7;
    --muted:#898781; --grid:#2c2c2a; --axis:#383835; --border:rgba(255,255,255,.10);
    --s1:#3987e5; --s-comp:#3987e5; --s-start:#d95926; --s-todo:#52514e;
    --s-otro:#898781; --good:#0ca30c; --warn:#c98500; --bad:#e34948;
    --ideal:#184f95;
  }}
}}
:root[data-theme="dark"] {{
  --plane:#0d0d0d; --surface:#1a1a19; --ink:#fff; --ink2:#c3c2b7;
  --grid:#2c2c2a; --axis:#383835; --border:rgba(255,255,255,.10);
  --s1:#3987e5; --s-comp:#3987e5; --s-start:#d95926; --s-todo:#52514e; --ideal:#184f95;
}}
:root[data-theme="light"] {{
  --plane:#f9f9f7; --surface:#fcfcfb; --ink:#0b0b0b; --ink2:#52514e;
  --grid:#e1e0d9; --axis:#c3c2b7; --border:rgba(11,11,11,.10);
  --s1:#2a78d6; --s-comp:#2a78d6; --s-start:#eb6834; --s-todo:#c3c2b7; --ideal:#b7d3f6;
}}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--plane); color:var(--ink);
  font-family:system-ui,-apple-system,"Segoe UI",sans-serif; line-height:1.45; }}
.wrap {{ max-width:960px; margin:0 auto; padding:32px 20px 64px; }}
header h1 {{ font-size:1.5rem; margin:0 0 2px; letter-spacing:-.02em; }}
header .sub {{ color:var(--ink2); font-size:.95rem; }}
.badge {{ display:inline-block; background:var(--s1); color:#fff; font-size:.72rem;
  font-weight:600; padding:2px 9px; border-radius:99px; vertical-align:middle;
  margin-left:8px; }}
section {{ background:var(--surface); border:1px solid var(--border);
  border-radius:14px; padding:20px 22px; margin-top:18px; }}
section h2 {{ font-size:.82rem; text-transform:uppercase; letter-spacing:.06em;
  color:var(--muted); margin:0 0 14px; font-weight:600; }}
.kpis {{ display:grid; grid-template-columns:repeat(5,1fr); gap:12px;
  margin-top:18px; }}
.kpi {{ background:var(--surface); border:1px solid var(--border);
  border-radius:12px; padding:14px 16px; display:flex; flex-direction:column; }}
.kpi-lbl {{ font-size:.72rem; color:var(--muted); text-transform:uppercase;
  letter-spacing:.04em; }}
.kpi-val {{ font-size:1.9rem; font-weight:650; line-height:1.1; margin:2px 0;
  letter-spacing:-.02em; }}
.kpi-sub {{ font-size:.76rem; color:var(--ink2); }}
.chart {{ width:100%; height:auto; }}
.legend {{ display:flex; gap:16px; flex-wrap:wrap; font-size:.8rem;
  color:var(--ink2); margin-top:10px; }}
.legend i {{ display:inline-block; width:11px; height:11px; border-radius:3px;
  margin-right:5px; vertical-align:-1px; }}
.grid {{ stroke:var(--grid); stroke-width:1; }}
.tick, .ylbl, .cnt, .lbl-comp {{ fill:var(--ink2);
  font-family:system-ui,sans-serif; }}
.tick {{ font-size:10px; fill:var(--muted); }}
.ylbl {{ font-size:12px; }} .ylbl .off {{ fill:var(--muted); font-size:10px; }}
.cnt {{ font-size:12px; font-weight:600; fill:var(--ink); }}
.lbl-comp {{ font-size:11px; font-weight:600; fill:var(--s-comp); }}
.line-scope {{ fill:none; stroke:var(--axis); stroke-width:2;
  stroke-dasharray:2 3; }}
.line-ideal {{ fill:none; stroke:var(--ideal); stroke-width:2; }}
.line-comp {{ fill:none; stroke:var(--s-comp); stroke-width:2.5;
  stroke-linejoin:round; stroke-linecap:round; }}
.dot-c {{ fill:var(--s-comp); stroke:var(--surface); stroke-width:2; }}
.seg-completed {{ fill:var(--s-comp); }} .seg-started {{ fill:var(--s-start); }}
.seg-unstarted {{ fill:var(--s-todo); }} .seg-otro {{ fill:var(--s-otro); }}
.split {{ display:grid; grid-template-columns:1.35fr 1fr; gap:18px; }}
.tp-bar {{ display:flex; height:30px; border-radius:8px; overflow:hidden;
  border:1px solid var(--border); }}
.tp-real {{ background:var(--s-comp); }} .tp-carry {{ background:var(--s-otro); }}
.tp-bar div {{ display:flex; align-items:center; justify-content:center;
  color:#fff; font-size:.8rem; font-weight:600; }}
.tp-note {{ font-size:.82rem; color:var(--ink2); margin-top:12px; }}
.tp-note code {{ font-size:.78rem; }}
.metric-row {{ display:flex; gap:22px; margin-top:6px; flex-wrap:wrap; }}
.metric-row .m b {{ font-size:1.3rem; }}
.metric-row .m span {{ font-size:.76rem; color:var(--ink2); display:block; }}
.meter {{ margin-bottom:13px; }}
.meter-top {{ display:flex; justify-content:space-between; font-size:.85rem;
  margin-bottom:4px; }}
.meter-top .frac {{ color:var(--muted); font-weight:400; font-size:.78rem; }}
.track {{ height:8px; background:var(--grid); border-radius:99px; overflow:hidden; }}
.fill {{ height:100%; border-radius:99px; }}
.fill.good {{ background:var(--good); }} .fill.warn {{ background:var(--warn); }}
.fill.bad {{ background:var(--bad); }}
.aging {{ list-style:none; padding:0; margin:10px 0 0; font-size:.85rem; }}
.aging li {{ padding:4px 0; border-top:1px solid var(--border); }}
.aging b {{ color:var(--bad); }}
table.risk {{ width:100%; border-collapse:collapse; font-size:.83rem; }}
table.risk th {{ text-align:left; color:var(--muted); font-weight:600;
  font-size:.72rem; text-transform:uppercase; letter-spacing:.04em;
  padding:6px 8px; border-bottom:1px solid var(--border); }}
table.risk td {{ padding:7px 8px; border-bottom:1px solid var(--border);
  vertical-align:top; }}
.mono {{ font-family:ui-monospace,"Cascadia Code",monospace; font-size:.8rem;
  color:var(--ink2); white-space:nowrap; }}
.chip {{ display:inline-block; background:var(--grid); color:var(--ink2);
  border-radius:6px; padding:1px 7px; font-size:.72rem; margin:1px 2px 1px 0;
  white-space:nowrap; }}
.empty {{ color:var(--good); font-weight:500; }}
.table-scroll {{ overflow-x:auto; }}
footer {{ margin-top:26px; color:var(--muted); font-size:.78rem;
  text-align:center; }}
.note {{ font-size:.82rem; color:var(--ink2); background:var(--plane);
  border-left:3px solid var(--warn); padding:10px 14px; border-radius:0 8px 8px 0;
  margin-top:12px; }}
@media (max-width:720px) {{
  .kpis {{ grid-template-columns:repeat(2,1fr); }}
  .split {{ grid-template-columns:1fr; }}
}}
</style>
</head>
<body>
<div class="wrap">
<header>
  <h1>Desempeño del equipo · Ciclo #{m["cycle_number"]}
    <span class="badge">{esc(m["team"])}</span></h1>
  <div class="sub">{fdate(m["starts"].date())} – {fdate(m["ends"].date())} de
    {m["ends"].year} · {m["total_days"]} días · corte al {gen}</div>
</header>

<div class="kpis">{kpi_html}</div>

<section>
  <h2>1 · Avance del ciclo (burnup)</h2>
  {svg_burnup(m)}
  <div class="legend">
    <span><i style="background:var(--s-comp)"></i>Completadas (acumulado)</span>
    <span><i style="background:var(--ideal)"></i>Ritmo ideal</span>
    <span><i style="background:var(--axis)"></i>Alcance total</span>
  </div>
</section>

<section>
  <h2>2 · Throughput real vs. carryover</h2>
  <div class="tp-bar">
    <div class="tp-real" style="width:{real_w:.0f}%">{real_n} reales</div>
    <div class="tp-carry" style="width:{100-real_w:.0f}%">{carry_n} carryover</div>
  </div>
  <div class="metric-row">
    <div class="m"><b>{real_n}</b><span>completadas dentro del ciclo</span></div>
    <div class="m"><b>{carry_n}</b><span>cierres de backlog (día 1)</span></div>
    <div class="m"><b>{ct}</b><span>cycle time · {esc(ct_note)}</span></div>
  </div>
  <p class="tp-note">Throughput real: <code>{esc(real_ids)}</code>. El
    <b>carryover</b> son issues antiguos marcados «Hecho» el primer día del ciclo
    (limpieza de backlog); no reflejan trabajo producido en estos {m["total_days"]}
    días.</p>
  {("<div class='note'><b>WIP estancado:</b> hay trabajos en curso iniciados antes "
    "del ciclo — conviene cerrarlos o repartirlos:</div>" + aging_html) if m["aging"] else ""}
</section>

<section>
  <h2>3 · Carga por persona</h2>
  {svg_stacked_people(m)}
  <div class="legend">
    <span><i style="background:var(--s-comp)"></i>Hecho</span>
    <span><i style="background:var(--s-start)"></i>En curso</span>
    <span><i style="background:var(--s-todo)"></i>Por hacer</span>
    <span>Sin asignar: <b>{m["sin_asignar"]}</b></span>
  </div>
</section>

<section>
  <h2>4 · Salud de datos y proceso</h2>
  {salud_html}
  <p class="tp-note">Baja cobertura de estimación y de etiquetas de tipo significa
    que las métricas de velocidad y la segmentación por carril aún no son
    confiables. Subir estos porcentajes hace que el propio tablero valga más.</p>
</section>

<section>
  <h2>Issues en riesgo</h2>
  <div class="table-scroll">{tabla_riesgo(m)}</div>
</section>

<footer>Generado automáticamente desde Linear · SaludMía · {gen}</footer>
</div>
</body>
</html>'''


# ---------------------------------------------------------------------------
# 4b. Tablero Semanal (diseño SaludMía) — alimenta plantilla_tablero.html
# ---------------------------------------------------------------------------
#
# La plantilla lleva la lógica de cálculo en JS (renderVals, única fuente de la
# verdad); aquí solo producimos el objeto DATA con datos crudos de Linear y las
# constantes del ciclo. Ver: "Tablero de seguimiento Linear/plantilla_tablero.html".

DIAS_SEMANA_ES = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]
SALUD_VALIDA = {"onTrack", "atRisk", "offTrack"}


def epoch_ms(d):
    """Fecha (date) -> epoch en milisegundos a medianoche UTC (== Date.UTC en JS)."""
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp()) * 1000


def dias_habiles(inicio, fin):
    """Lista de fechas hábiles (lun–vie) entre inicio y fin, inclusive."""
    from datetime import timedelta
    out, d = [], inicio
    while d <= fin:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def flarga(d):
    """Fecha larga en español: 'lun 21 sep'."""
    return f"{DIAS_SEMANA_ES[d.weekday()]} {d.day} {MESES[d.month]}"


def iso_date(s):
    """ISO/timestamp -> 'YYYY-MM-DD' o None."""
    d = as_date(s)
    return d.isoformat() if d else None


def estado_tablero(it):
    """Mapea el estado de Linear al vocabulario del tablero."""
    if it["stateType"] == "completed":
        return "completed"
    if it.get("stateName") == "Esperando respuesta":
        return "blocked"
    if it["stateType"] == "started":
        return "started"
    return "unstarted"  # unstarted/backlog/triage se muestran como planeadas


def build_tablero_data(data, today=None):
    """Construye el objeto DATA que la plantilla del Tablero Semanal inyecta."""
    cycle = data["cycle"]
    issues = data["issues"]
    updates_src = data.get("project_updates", {})

    starts = parse_dt(cycle["startsAt"]).date()
    ends = parse_dt(cycle["endsAt"]).date()
    habiles = dias_habiles(starts, ends) or [starts]
    primero, ultimo = habiles[0], habiles[-1]

    if today is None:
        today = datetime.now(timezone.utc).date()
    hoy = max(primero, min(today, ultimo))
    ahora = datetime.now(timezone.utc).date()

    # --- issues (solo los estados que dibuja el tablero) ---
    t_issues = []
    for it in issues:
        s = estado_tablero(it)
        if it["stateType"] in ("canceled", "duplicate"):
            continue
        tipo_labels = [l for l in it.get("labels", []) if l.get("parent") == "tipo"]
        t_issues.append({
            "id": it["id"],
            "t": it["title"],
            "a": it.get("assignee"),
            "p": it.get("project") or "Sin proyecto",
            "s": s,
            "ini": iso_date(it.get("startedAt")),
            "fin": iso_date(it.get("completedAt")),
            "est": it.get("estimate"),
            "lab": len(tipo_labels),
            "due": it.get("dueDate"),
        })

    # --- updates por proyecto (el más reciente) ---
    t_updates = []
    for nombre, u in updates_src.items():
        d = as_date(u.get("createdAt"))
        salud = u.get("health") if u.get("health") in SALUD_VALIDA else "onTrack"
        t_updates.append({
            "p": nombre,
            "f": fdate(d) if d else "—",
            "orden": d.isoformat() if d else "",
            "salud": salud,
            "autor": u.get("author") or "—",
            "txt": u.get("body") or "",
        })

    # --- detenidas (estado "Esperando respuesta") ---
    t_detenidas = []
    for it in issues:
        if it.get("stateName") != "Esperando respuesta":
            continue
        desde = iso_date(it.get("startedAt")) or iso_date(it.get("createdAt"))
        ult = as_date(it.get("updatedAt"))
        t_detenidas.append({
            "id": it["id"],
            "t": it["title"],
            "resp": it.get("assignee") or "Sin asignar",
            "p": it.get("project") or "Sin proyecto",
            "desde": desde or primero.isoformat(),
            "ultima": fdate(ult) if ult else "—",
        })

    return {
        "cycle": {
            "cicloInicio": epoch_ms(primero),
            "hoy": epoch_ms(hoy),
            "diasHabiles": [epoch_ms(d) for d in habiles],
            "rangoFechas": f"{flarga(primero)} – {flarga(ultimo)} {ultimo.year}",
            "fechaInicio": flarga(primero),
            "fechaFin": flarga(ultimo),
            "actualizado": f"{ahora.day} {MESES[ahora.month]} {ahora.year}",
        },
        "issues": t_issues,
        "updates": t_updates,
        "detenidas": t_detenidas,
        "props": {
            "cicloLabel": f"Ciclo {cycle['number']}",
            "metaResolucion": 80,
            "ordenResponsables": "Resolución",
            "mostrarCronologia": True,
            "mostrarEsfuerzo": True,
        },
    }


def render_tablero(tablero_data):
    """Inyecta el JSON de datos en la plantilla del Tablero Semanal."""
    tpl_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "Tablero de seguimiento Linear", "plantilla_tablero.html",
    )
    with open(tpl_path, "r", encoding="utf-8") as f:
        tpl = f.read()
    payload = json.dumps(tablero_data, ensure_ascii=False).replace("</", "<\\/")
    if "/*__DATA__*/{}" not in tpl:
        raise RuntimeError("La plantilla no contiene el marcador /*__DATA__*/{}.")
    return tpl.replace("/*__DATA__*/{}", payload, 1)


# ---------------------------------------------------------------------------
# 5. Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Dashboard de desempeño por ciclo (Linear).")
    ap.add_argument("--datos", help="Ruta a un snapshot JSON (evita llamar a la API).")
    ap.add_argument("--salida", default="reportes", help="Carpeta de salida.")
    ap.add_argument("--abrir", action="store_true", help="Abrir el HTML al terminar.")
    ap.add_argument("--artifact", action="store_true",
                    help="Además, escribe un fragmento (style+body) para publicar como Artifact.")
    ap.add_argument("--tablero", action="store_true",
                    help="Genera el Tablero Semanal (diseño SaludMía) en reportes/ y en docs/index.html "
                         "para publicar en GitHub Pages.")
    args = ap.parse_args()

    if args.datos:
        data = load_snapshot(args.datos)
        print(f"[i] Datos desde snapshot: {args.datos}")
    else:
        api_key = os.environ.get("LINEAR_API_KEY")
        if not api_key:
            sys.exit(
                "ERROR: falta la variable de entorno LINEAR_API_KEY.\n"
                "Crea una Personal API Key en Linear (Settings > Security & access)\n"
                "y ejecuta:  setx LINEAR_API_KEY \"lin_api_xxx\"  (reabre la terminal).\n"
                "O usa un snapshot:  python generar_reporte_ciclo.py --datos <archivo.json>"
            )
        print("[i] Consultando la API de Linear…")
        data = fetch_from_linear(api_key)

    # --- Tablero Semanal (diseño SaludMía) para publicar en Pages ---
    if args.tablero:
        td = build_tablero_data(data)
        html = render_tablero(td)
        num = data["cycle"]["number"]

        os.makedirs(args.salida, exist_ok=True)
        out = os.path.join(args.salida, f"tablero_semanal_ciclo_{num}.html")
        with open(out, "w", encoding="utf-8") as f:
            f.write(html)

        docs = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs")
        os.makedirs(docs, exist_ok=True)
        index = os.path.join(docs, "index.html")
        with open(index, "w", encoding="utf-8") as f:
            f.write(html)

        print(f"[OK] Tablero Semanal ciclo #{num} · {len(td['issues'])} tareas · "
              f"{len(td['updates'])} updates · {len(td['detenidas'])} detenidas")
        print(f"[OK] Copia histórica: {out}")
        print(f"[OK] Publicado en:    {index}  (GitHub Pages sirve docs/)")

        if args.abrir:
            import webbrowser
            webbrowser.open("file://" + os.path.abspath(index))
        return

    m = compute_metrics(data)
    html = render_html(m)

    os.makedirs(args.salida, exist_ok=True)
    fecha = m["generado"].strftime("%Y%m%d")
    out = os.path.join(args.salida, f"reporte_ciclo_{m['cycle_number']}_{fecha}.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"[OK] Ciclo #{m['cycle_number']} · alcance {m['scope']} · "
          f"hechas {m['completed']} ({m['pct']:.0f}%) · "
          f"throughput real {len(m['real'])} vs carryover {len(m['carryover'])}")
    print(f"[OK] Dashboard escrito en: {out}")

    if args.artifact:
        style = html[html.index("<style>"):html.index("</style>") + 8]
        inner = html[html.index("<body>") + 6:html.index("</body>")]
        frag = os.path.join(args.salida, f"artifact_ciclo_{m['cycle_number']}.html")
        with open(frag, "w", encoding="utf-8") as f:
            f.write(style + "\n" + inner)
        print(f"[OK] Fragmento para Artifact en: {frag}")

    if args.abrir:
        import webbrowser
        webbrowser.open("file://" + os.path.abspath(out))


if __name__ == "__main__":
    main()
