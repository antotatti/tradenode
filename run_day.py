#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TradeNode - ciclo serale in paper trading (soldi finti, dati veri).

Solo libreria standard di Python. NON invia ordini a nessun broker.

Ogni sera, dopo la chiusura di Wall Street:
  1. rigioca la seduta appena finita con le regole fisse, usando i moduli di
     TradeNode Lab (entrata, uscita, dimensione, rischio, Time Machine);
  2. registra le operazioni simulate nel diario (data/diario/);
  3. prepara la lista dei titoli per la seduta successiva (data/liste/);
  4. scrive il rapporto giornaliero e, a fine settimana, quello settimanale.

Comandi:
  python3 run_day.py sera                 # ciclo normale (recupera i giorni saltati)
  python3 run_day.py sera --data 2026-10-16   # una seduta precisa (prova o recupero)
  python3 run_day.py settimana            # rapporto della settimana dell'ultimo giorno
  python3 run_day.py stato                # statistiche cumulative
"""

import argparse
import collections
import csv
import datetime
import gzip
import importlib.util
import json
import math
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
MODULI = os.path.join(ROOT, "tradenode", "modules")
DATI = os.path.join(ROOT, "data")
PY = sys.executable

COLONNE_OPERAZIONI = [
    "data", "versione_regole", "titolo", "setup", "ora_ingresso", "ingresso", "stop", "obiettivo",
    "quantita", "rischio_per_azione", "perdita_max_usd", "ora_uscita", "prezzo_uscita", "esito",
    "lordo_usd", "costi_usd", "netto_usd", "R_lordo", "R_netto",
    "tm_p_obiettivo", "tm_p_stop", "tm_p_nessuno", "tm_storia_sedute", "tm_storia_scattate",
    "tm_storia_obiettivo", "tm_storia_stop", "tm_giudizio", "lista_in_ritardo", "nota",
]
COLONNE_PREMARKET = ["data", "titolo", "fonte", "chiusura_prec", "apertura", "gap_pct", "pm_barre", "pm_volume",
                     "pm_rend_pct", "pm_range_pct", "p0950", "p1555", "rend_pct", "max_su_pct", "max_giu_pct",
                     "dati_scarsi"]
COLONNE_GIORNATE = ["data", "stato", "grafici", "scattati", "operazioni", "scartate", "netto_usd",
                    "R_netto", "lista_in_ritardo", "problemi"]


# ---------------------------------------------------------------- utilita'

def leggi_json(percorso, default=None):
    try:
        with open(percorso, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def scrivi_json(percorso, doc):
    os.makedirs(os.path.dirname(percorso), exist_ok=True)
    with open(percorso, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2, ensure_ascii=False)
        f.write("\n")


def scrivi_testo(percorso, testo):
    os.makedirs(os.path.dirname(percorso), exist_ok=True)
    with open(percorso, "w", encoding="utf-8") as f:
        f.write(testo)


def it(x, nd=2):
    """Numero con la virgola, come si scrive in Italia."""
    if x is None or x == "":
        return "n/d"
    return ("%.*f" % (nd, float(x))).replace(".", ",")


def usd(x):
    return ("%+.2f $" % float(x)).replace(".", ",")


def prima_riga(*testi):
    for t in testi:
        for riga in (t or "").splitlines():
            if riga.strip():
                return riga.strip()
    return "nessun messaggio"


def motivo(out, err):
    """La ragione di un rifiuto in una riga: dal JSON del modulo se c'e', altrimenti dal testo."""
    doc = json_da(out)
    if isinstance(doc, dict):
        sz = doc.get("sizing") or {}
        if sz.get("refusal"):
            return sz["refusal"]
        for k in ("refusal", "error", "reason"):
            if doc.get(k):
                return str(doc[k])
    return prima_riga(err, out)


def carica_entry():
    """Uso il modulo di entrata come libreria: calendario di Wall Street e scarico dei grafici."""
    p = os.path.join(MODULI, "trader-entry", "entry.py")
    spec = importlib.util.spec_from_file_location("tn_entry", p)
    m = importlib.util.module_from_spec(spec)
    sys.modules["tn_entry"] = m
    spec.loader.exec_module(m)
    return m


def esegui(lavoro, modulo, script, args, timeout=600):
    cmd = [PY, os.path.join(MODULI, modulo, script)] + args
    p = subprocess.run(cmd, cwd=lavoro, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def json_da(testo):
    try:
        return json.loads(testo)
    except (ValueError, TypeError):
        return None


def carica_config(percorso):
    cfg = leggi_json(percorso)
    if not cfg:
        raise SystemExit("config non leggibile: %s" % percorso)
    return cfg


# ---------------------------------------------------------------- fonte dei dati

class Fonte:
    """Scarica i grafici a 5 minuti (60 giorni) da Yahoo, oppure li legge da una cartella (prove)."""

    def __init__(self, E, grafici_offline=None, prepost_offline=None, pausa=0.3):
        self.E = E
        self.offline = grafici_offline
        self.prepost_offline = prepost_offline
        self.pausa = pausa

    def grafico(self, sym):
        E = self.E
        if self.offline:
            return E.read_text(E.offline_file(self.offline, sym))
        time.sleep(self.pausa)
        return E.yahoo_get(E.chart_path(sym, "60d", "5m"))

    def grafico_prepost(self, sym):
        """Stessi 60 giorni a 5 minuti, ma con le barre prima dell'apertura (solo per lo studio)."""
        E = self.E
        if self.prepost_offline:
            return E.read_text(E.offline_file(self.prepost_offline, sym))
        time.sleep(self.pausa)
        return E.yahoo_get(E.chart_path(sym, "60d", "5m") + "&includePrePost=true")


# ---------------------------------------------------------------- calendario

def giorno_di_borsa(E, d):
    return E.session(d) is not None


def prossimo_giorno(E, d):
    n = d + datetime.timedelta(days=1)
    for _ in range(15):
        if giorno_di_borsa(E, n):
            return n
        n += datetime.timedelta(days=1)
    raise SystemExit("calendario di Wall Street non copre dopo %s" % d)


def ultimo_giorno_chiuso(E, adesso_utc, margine_min):
    et = E.to_eastern(adesso_utc)
    d = et.date()
    for _ in range(20):
        s = E.session(d)
        if s is not None:
            chiusura = datetime.datetime.combine(d, s[1]) + datetime.timedelta(minutes=margine_min)
            if et >= chiusura:
                return d
        d -= datetime.timedelta(days=1)
    raise SystemExit("nessun giorno di borsa trovato")


# ---------------------------------------------------------------- simulazione dell'uscita

def simula_uscita(E, barre, giorno, inizio_barra_ingresso, stop, target, flat_by):
    """Barra per barra dopo l'ingresso. Stop e obiettivo nella stessa barra: vale lo stop.
    Se la barra apre sotto lo stop (gap) si esce all'apertura, peggio dello stop.
    Restituisce (esito, prezzo, ora_uscita) oppure None senza dati."""
    h, m = [int(x) for x in inizio_barra_ingresso.split(":")]
    t_ing = datetime.time(h, m)
    h, m = [int(x) for x in flat_by.split(":")]
    t_flat = datetime.time(h, m)
    seq = []
    for b in barre:
        et = E.to_eastern(E.utc_of(b["t"]))
        if et.date() == giorno:
            seq.append((et, b))
    seq.sort(key=lambda x: x[0])
    dopo = [(et, b) for et, b in seq if t_ing < et.time() < t_flat]
    if not dopo:
        return None
    cinque = datetime.timedelta(minutes=5)
    for et, b in dopo:
        if b["o"] <= stop:
            return ("stop", b["o"], et + cinque)
        if b["l"] <= stop:
            return ("stop", stop, et + cinque)
        if b["h"] >= target:
            return ("obiettivo", target, et + cinque)
    et, b = dopo[-1]
    fine = et + cinque
    if fine.time() >= t_flat:
        return ("chiusura", b["c"], fine)
    return ("incompleto", b["c"], fine)


# ---------------------------------------------------------------- una seduta

def elabora_giorno(E, cfg, d, fonte, log):
    ds = d.isoformat()
    sim = cfg["simulazione"]
    regole = cfg["regole"]
    ris = {"data": ds, "stato": "ok", "grafici": 0, "scattati": 0, "operazioni": [], "scartate": [],
           "problemi": [], "lista_in_ritardo": False}
    lista = leggi_json(os.path.join(DATI, "liste", ds + ".json"))
    if not lista:
        ris["stato"] = "senza_lista"
        log("%s: nessuna lista preparata la sera prima, seduta non valutata" % ds)
        return ris
    ris["lista_in_ritardo"] = bool(lista.get("in_ritardo"))
    simboli = list(lista.get("simboli", []))[: int(sim["max_titoli_lista"])]
    if not simboli:
        ris["stato"] = "lista_vuota"
        return ris

    lavoro = tempfile.mkdtemp(prefix="tradenode-")
    try:
        os.makedirs(os.path.join(lavoro, "trader", "state"))
        os.makedirs(os.path.join(lavoro, "grafici"))
        scrivi_json(os.path.join(lavoro, "trader", "config.json"), regole)

        testi = {}
        for s in simboli:
            try:
                t = fonte.grafico(s)
            except Exception as e:  # rete, Yahoo, simbolo sconosciuto
                ris["problemi"].append("%s: %s" % (s, prima_riga(str(e))))
                continue
            scrivi_testo(os.path.join(lavoro, "grafici", s + ".json"), t)
            testi[s] = t
        if not testi:
            raise RuntimeError("nessun grafico scaricato. Primo errore: %s" % ris["problemi"][0])
        ris["grafici"] = len(testi)

        # 1) chi ha dato il segnale di entrata, e a che ora (rigioco la seduta: --now alle 15:30)
        scattati = []
        for s in testi:
            rc, out, err = esegui(lavoro, "trader-entry", "entry.py",
                                  ["check", s, "--now", "%s 15:30" % ds, "--offline", "grafici", "--json"])
            doc = json_da(out)
            if not doc:
                ris["problemi"].append("%s: entrata: %s" % (s, prima_riga(err, out)))
                continue
            if doc.get("status") == "triggered":
                scattati.append((doc["trigger"]["close_time"], s, doc))
        # a pari ora si segue l'ordine della lista (i titoli piu' scambiati per primi), non l'alfabeto (v1.1)
        ordine = {s: i for i, s in enumerate(simboli)}
        scattati.sort(key=lambda x: (x[0], ordine.get(x[1], 9999)))
        ris["scattati"] = len(scattati)

        # 2) uno alla volta, in ordine di ora: uscita, dimensione, rischio, esito simulato
        eseguite = []      # operazioni passate dal rischio, per contare capitale e limiti del giorno
        registro = []      # copia del registro "ordini piazzati oggi" che il modulo rischio legge
        for ora, s, doc in scattati:
            adesso = "%s %s" % (ds, ora)
            for f in ("entry.json", "plan.json", "orders.json"):
                p = os.path.join(lavoro, "trader", f)
                if os.path.exists(p):
                    os.remove(p)
            esegui(lavoro, "trader-entry", "entry.py",
                   ["check", s, "--now", adesso, "--offline", "grafici", "--write", "trader/entry.json", "--json"])
            rc, out, err = esegui(lavoro, "trader-exit", "exit.py",
                                  ["plan", "--write", "--now", adesso, "--stop", sim["stop"],
                                   "--target", sim["target"], "--json"])
            piano = leggi_json(os.path.join(lavoro, "trader", "plan.json"))
            if rc != 0 or not piano:
                ris["scartate"].append({"titolo": s, "ora": ora, "fase": "uscita", "motivo": motivo(out, err)})
                continue

            # capitale libero a quest'ora: budget meno le posizioni ancora aperte
            aperte = [e for e in eseguite if e["fine"] > doc_dt(ds, ora)]
            cash = float(regole["budget"]) - sum(e["quantita"] * e["ingresso"] for e in aperte)
            scrivi_json(os.path.join(lavoro, "trader", "portfolio.json"), {
                "cash": round(cash, 2),
                "positions": [{"ticker": e["titolo"], "shares": e["quantita"],
                               "lots": [{"shares": e["quantita"], "cost": round(e["quantita"] * e["ingresso"], 2)}]}
                              for e in aperte]})
            scrivi_json(os.path.join(lavoro, "trader", "state", "broker.json"), {"orders": registro})

            rc, out, err = esegui(lavoro, "trader-budget", "size.py", ["--write", "--now", adesso, "--json"])
            dim = json_da(out)
            ordine_doc = leggi_json(os.path.join(lavoro, "trader", "orders.json"))
            if rc != 0 or not dim or not ordine_doc:
                ris["scartate"].append({"titolo": s, "ora": ora, "fase": "dimensione", "motivo": motivo(out, err)})
                continue
            rc, out, err = esegui(lavoro, "trader-risk", "check.py", ["--now", adesso, "--json", "--no-write"])
            rischio = json_da(out)
            if not rischio or not rischio.get("results"):
                ris["problemi"].append("%s: rischio: %s" % (s, prima_riga(err, out)))
                continue
            r0 = rischio["results"][0]
            if r0.get("verdict") != "PASS":
                ris["scartate"].append({"titolo": s, "ora": ora, "fase": "rischio",
                                        "motivo": "; ".join(r0.get("reasons") or ["BLOCK"])})
                continue

            # passata: la "piazzo" solo sul registro simulato (nessun broker, nessun ordine vero)
            qty = int(r0["amount"].split()[0])
            perdita_max = float(r0["max_loss"])
            registro.append({"at": "%sT%s:00" % (ds, ora), "date": ds, "status": "placed", "symbol": s,
                             "side": "buy", "kind": "entry", "qty": qty, "max_loss": perdita_max,
                             "stop": piano["stop"], "source": "paper-sim"})

            barre = E.parse_chart(testi[s], s)["bars"]
            esito = simula_uscita(E, barre, d, doc["trigger"]["bar_start"], piano["stop"], piano["target"],
                                  piano["flat_by"])
            if esito is None:
                ris["problemi"].append("%s: nessuna barra dopo l'ingresso, operazione non valutata" % s)
                continue
            tipo, prezzo_uscita, fine = esito
            ingresso = float(piano["entry"])
            rischio_az = float(piano["risk_per_share"])
            lordo = (prezzo_uscita - ingresso) * qty
            costi = float(sim["costo_per_azione_usd"]) * qty
            netto = lordo - costi
            eseguite.append({"titolo": s, "quantita": qty, "ingresso": ingresso, "fine": fine})

            tm = {}
            rc, out, err = esegui(lavoro, "trader-timemachine", "timemachine.py",
                                  ["run", "--offline", "grafici/%s.json" % s, "--until", piano["flat_by"],
                                   "--now", adesso, "--json"])
            tmdoc = json_da(out)
            if tmdoc and "mc" in tmdoc:
                tm = {"mc": tmdoc.get("mc") or {}, "storia": tmdoc.get("history") or {},
                      "giudizio": tmdoc.get("verdict") or ""}
            nota = "" if tm else "Time Machine non disponibile: %s" % prima_riga(err, out)
            if tipo == "incompleto":
                nota = (nota + " Dati incompleti dopo l'ingresso: operazione esclusa dalle statistiche.").strip()
            ris["operazioni"].append({
                "data": ds, "versione_regole": cfg["versione_regole"], "titolo": s, "setup": piano.get("setup"),
                "ora_ingresso": ora, "ingresso": ingresso, "stop": piano["stop"], "obiettivo": piano["target"],
                "quantita": qty, "rischio_per_azione": rischio_az, "perdita_max_usd": perdita_max,
                "ora_uscita": fine.strftime("%H:%M"), "prezzo_uscita": round(prezzo_uscita, 2), "esito": tipo,
                "lordo_usd": round(lordo, 2), "costi_usd": round(costi, 2), "netto_usd": round(netto, 2),
                "R_lordo": round((prezzo_uscita - ingresso) / rischio_az, 3),
                "R_netto": round(netto / (qty * rischio_az), 3),
                "tm_p_obiettivo": tm.get("mc", {}).get("p_target", ""),
                "tm_p_stop": tm.get("mc", {}).get("p_stop", ""),
                "tm_p_nessuno": tm.get("mc", {}).get("p_neither", ""),
                "tm_storia_sedute": tm.get("storia", {}).get("sessions", ""),
                "tm_storia_scattate": tm.get("storia", {}).get("triggered", ""),
                "tm_storia_obiettivo": tm.get("storia", {}).get("target", ""),
                "tm_storia_stop": tm.get("storia", {}).get("stop", ""),
                "tm_giudizio": tm.get("giudizio", ""),
                "lista_in_ritardo": "si" if ris["lista_in_ritardo"] else "no", "nota": nota,
            })
            # grafico salvato solo per le operazioni (si puo' ricontrollare dopo)
            os.makedirs(os.path.join(DATI, "grafici", ds), exist_ok=True)
            with gzip.open(os.path.join(DATI, "grafici", ds, s + ".json.gz"), "wt", encoding="utf-8") as g:
                g.write(testi[s])
        return ris
    finally:
        shutil.rmtree(lavoro, ignore_errors=True)


def doc_dt(ds, ora):
    h, m = [int(x) for x in ora.split(":")]
    d = datetime.date.fromisoformat(ds)
    return datetime.datetime.combine(d, datetime.time(h, m))


# ---------------------------------------------------------------- diario

def leggi_csv(percorso):
    if not os.path.exists(percorso):
        return []
    with open(percorso, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def scrivi_csv(percorso, colonne, righe):
    os.makedirs(os.path.dirname(percorso), exist_ok=True)
    with open(percorso, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=colonne, extrasaction="ignore")
        w.writeheader()
        for r in righe:
            w.writerow(r)


def salva_diario(ris):
    """Sostituisce le righe della stessa data: rilanciare un giorno non crea doppioni."""
    p = os.path.join(DATI, "diario", "operazioni.csv")
    righe = [r for r in leggi_csv(p) if r["data"] != ris["data"]] + ris["operazioni"]
    righe.sort(key=lambda r: (r["data"], r["ora_ingresso"], r["titolo"]))
    scrivi_csv(p, COLONNE_OPERAZIONI, righe)
    valide = [o for o in ris["operazioni"] if o["esito"] != "incompleto"]
    giorno = {"data": ris["data"], "stato": ris["stato"], "grafici": ris["grafici"], "scattati": ris["scattati"],
              "operazioni": len(ris["operazioni"]), "scartate": len(ris["scartate"]),
              "netto_usd": round(sum(float(o["netto_usd"]) for o in valide), 2),
              "R_netto": round(sum(float(o["R_netto"]) for o in valide), 3),
              "lista_in_ritardo": "si" if ris["lista_in_ritardo"] else "no",
              "problemi": " | ".join(ris["problemi"])[:500]}
    pg = os.path.join(DATI, "diario", "giornate.csv")
    gs = [g for g in leggi_csv(pg) if g["data"] != ris["data"]] + [giorno]
    gs.sort(key=lambda g: g["data"])
    scrivi_csv(pg, COLONNE_GIORNATE, gs)


# ---------------------------------------------------------------- statistiche

def wilson(k, n, z=1.96):
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    den = 1 + z * z / n
    c = (p + z * z / (2 * n)) / den
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (max(0.0, c - m), min(1.0, c + m))


def statistiche(righe):
    complete = [r for r in righe if r["esito"] in ("stop", "obiettivo", "chiusura")]
    valide = [r for r in complete if r.get("lista_in_ritardo") != "si"]   # v1.1: lista in ritardo = non conta
    n = len(valide)
    st = {"n": n, "incomplete": len(righe) - len(complete), "escluse_ritardo": len(complete) - n}
    if n == 0:
        return st
    vinte = sum(1 for r in valide if float(r["netto_usd"]) > 0)
    st["vinte"] = vinte
    st["perc_vinte"] = vinte / n
    st["intervallo"] = wilson(vinte, n)
    st["R_medio"] = sum(float(r["R_netto"]) for r in valide) / n
    st["R_totale"] = sum(float(r["R_netto"]) for r in valide)
    st["netto_totale"] = sum(float(r["netto_usd"]) for r in valide)
    cum = picco = dd = 0.0
    for r in sorted(valide, key=lambda r: (r["data"], r["ora_uscita"])):
        cum += float(r["netto_usd"])
        picco = max(picco, cum)
        dd = max(dd, picco - cum)
    st["drawdown_usd"] = dd
    prima = datetime.date.fromisoformat(min(r["data"] for r in valide))
    ultima = datetime.date.fromisoformat(max(r["data"] for r in valide))
    st["prima"], st["ultima"] = prima, ultima
    st["settimane"] = ((ultima - prima).days + 1) / 7.0
    st["esiti"] = {k: sum(1 for r in valide if r["esito"] == k) for k in ("stop", "obiettivo", "chiusura")}
    return st


def blocco_statistiche(st, cfg, titolo):
    if st["n"] == 0:
        extra = ""
        if st.get("escluse_ritardo"):
            extra = "Escluse dal conteggio ufficiale (lista in ritardo): %d operazioni.\n" % st["escluse_ritardo"]
        return "%s\nNessuna operazione valutata finora.\n%s" % (titolo, extra)
    c = cfg["criteri"]
    dd_max = float(cfg["regole"]["budget"]) * float(c["drawdown_max_pct"]) / 100
    lo, hi = st["intervallo"]
    righe = [titolo,
             "- Operazioni valutate: %d (obiettivo %d)" % (st["n"], c["operazioni_minime"]),
             "- Settimane di prova: %s (obiettivo %d)" % (it(st["settimane"], 1), c["settimane_minime"]),
             "- Vinte: %d su %d (%s%%), intervallo al 95%% circa %s%%-%s%%: con pochi dati e' molto largo" % (
                 st["vinte"], st["n"], it(st["perc_vinte"] * 100, 0), it(lo * 100, 0), it(hi * 100, 0)),
             "- Esiti: %d stop, %d obiettivo, %d chiusura forzata" % (
                 st["esiti"]["stop"], st["esiti"]["obiettivo"], st["esiti"]["chiusura"]),
             "- R medio per operazione, dopo i costi: %s" % it(st["R_medio"], 2),
             "- Risultato netto simulato: %s (R totale %s)" % (usd(st["netto_totale"]), it(st["R_totale"], 1)),
             "- Drawdown massimo: %s $ (limite %s $)" % (it(st["drawdown_usd"], 2), it(dd_max, 0))]
    if st.get("escluse_ritardo"):
        righe.append("- Escluse dal conteggio ufficiale (lista in ritardo): %d operazioni" % st["escluse_ritardo"])
    return "\n".join(righe) + "\n"


# ---------------------------------------------------------------- rapporti

def tabella_operazioni(ops):
    if not ops:
        return "Nessuna operazione simulata.\n"
    r = ["| Titolo | Ingresso | Prezzo | Stop | Obiettivo | Azioni | Uscita | Esito | Netto | R |",
         "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for o in ops:
        r.append("| %s | %s | %s | %s | %s | %s | %s %s | %s | %s | %s |" % (
            o["titolo"], o["ora_ingresso"], it(o["ingresso"]), it(o["stop"]), it(o["obiettivo"]), o["quantita"],
            o["ora_uscita"], it(o["prezzo_uscita"]), o["esito"], usd(o["netto_usd"]), it(o["R_netto"], 2)))
    return "\n".join(r) + "\n"


def rapporto_giornaliero(ris, cfg, tutte):
    ds = ris["data"]
    valide = [o for o in ris["operazioni"] if o["esito"] != "incompleto"]
    netto = sum(float(o["netto_usd"]) for o in valide)
    t = ["# TradeNode - seduta del %s (paper trading)" % ds, "",
         "Soldi finti, dati veri. Regole versione %s. Non e' consulenza finanziaria." % cfg["versione_regole"], ""]
    if ris["stato"] == "senza_lista":
        t += ["**Seduta non valutata**: la lista dei titoli non era stata preparata la sera prima. "
              "Non rigioco la giornata senza lista, per non barare.", ""]
    elif ris["stato"] == "lista_vuota":
        t += ["**Nessun titolo in lista per questa seduta.**", ""]
    else:
        t += ["**Riepilogo**: %d grafici letti, %d segnali di entrata, %d operazioni simulate, %d scartate. "
              "Risultato netto: %s." % (ris["grafici"], ris["scattati"], len(ris["operazioni"]),
                                        len(ris["scartate"]), usd(netto)), ""]
        if ris["lista_in_ritardo"]:
            t += ["Attenzione: la lista di questa seduta e' stata preparata in ritardo (dopo l'apertura). "
                  "Usa solo dati fino alla chiusura precedente, ma la prova di non aver sbirciato e' piu' debole.", ""]
        t += ["## Operazioni simulate", "", tabella_operazioni(ris["operazioni"])]
        for o in ris["operazioni"]:
            if o["tm_p_obiettivo"] != "":
                t.append("- %s: la Time Machine dava %s%% di arrivare all'obiettivo e %s%% di toccare lo stop "
                         "(su %s sedute reali: scattata %s volte, obiettivo %s, stop %s)." % (
                             o["titolo"], it(float(o["tm_p_obiettivo"]) * 100, 0),
                             it(float(o["tm_p_stop"]) * 100, 0), o["tm_storia_sedute"], o["tm_storia_scattate"],
                             o["tm_storia_obiettivo"], o["tm_storia_stop"]))
            if o["nota"]:
                t.append("- %s: %s" % (o["titolo"], o["nota"]))
        if ris["operazioni"]:
            t.append("")
        if ris["scartate"]:
            t += ["## Segnali scartati", ""]
            for s in ris["scartate"]:
                t.append("- %s (%s, fase %s): %s" % (s["titolo"], s["ora"], s["fase"], s["motivo"]))
            t.append("")
    if ris["problemi"]:
        t += ["## Problemi con i dati", ""] + ["- " + p for p in ris["problemi"]] + [""]
    pm = ris.get("premarket")
    if pm:
        t += ["## Dati pre-apertura (raccolta per lo studio)", "", riga_premarket(pm), ""]
    t += [blocco_statistiche(statistiche(tutte), cfg, "## Dall'inizio"),
          "Il confronto con il semplice \"compra e tieni\" non e' ancora calcolato: serve prima di pensare a soldi veri.", ""]
    return "\n".join(t)


def rapporto_settimanale(d, cfg):
    anno, sett, _ = d.isocalendar()
    tutte = leggi_csv(os.path.join(DATI, "diario", "operazioni.csv"))
    gs = leggi_csv(os.path.join(DATI, "diario", "giornate.csv"))
    della = [r for r in tutte if datetime.date.fromisoformat(r["data"]).isocalendar()[:2] == (anno, sett)]
    giorni = [g for g in gs if datetime.date.fromisoformat(g["data"]).isocalendar()[:2] == (anno, sett)]
    st_sett = statistiche(della)
    t = ["# TradeNode - settimana %d-W%02d (paper trading)" % (anno, sett), "",
         "Rapporto da incollare nella chat del progetto per la revisione settimanale. Soldi finti, dati veri.", "",
         "## Giorno per giorno", "", "| Data | Stato | Segnali | Operazioni | Netto | R |", "| --- | --- | --- | --- | --- | --- |"]
    for g in giorni:
        stato_g = g["stato"] + (" (lista in ritardo, non conta)" if g.get("lista_in_ritardo") == "si" else "")
        t.append("| %s | %s | %s | %s | %s | %s |" % (g["data"], stato_g, g["scattati"], g["operazioni"],
                                                     usd(g["netto_usd"]), it(g["R_netto"], 2)))
    t += ["", "## Operazioni della settimana", "", tabella_operazioni(della),
          blocco_statistiche(st_sett, cfg, "## Numeri della settimana"),
          blocco_statistiche(statistiche(tutte), cfg, "## Numeri dall'inizio")]
    cal = [r for r in tutte if r["esito"] in ("stop", "obiettivo", "chiusura") and r["tm_p_obiettivo"] != ""
           and r.get("lista_in_ritardo") != "si"]
    t.append("## La Time Machine ci azzecca?")
    if len(cal) < 10:
        t.append("Servono almeno 10 operazioni con previsione: finora %d." % len(cal))
    else:
        pt = sum(float(r["tm_p_obiettivo"]) for r in cal) / len(cal)
        ps = sum(float(r["tm_p_stop"]) for r in cal) / len(cal)
        ot = sum(1 for r in cal if r["esito"] == "obiettivo") / len(cal)
        os_ = sum(1 for r in cal if r["esito"] == "stop") / len(cal)
        t += ["- Obiettivo raggiunto: previsto in media %s%%, avvenuto nel %s%% dei casi." % (it(pt * 100, 0), it(ot * 100, 0)),
              "- Stop toccato: previsto in media %s%%, avvenuto nel %s%% dei casi." % (it(ps * 100, 0), it(os_ * 100, 0))]
    t += ["", analisi_premarket(), ""]
    senza = [g["data"] for g in giorni if g["stato"] != "ok"]
    if senza:
        t += ["## Sedute non valutate", "", ", ".join(senza), ""]
    prob = [(g["data"], g["problemi"]) for g in giorni if g["problemi"]]
    if prob:
        t += ["## Problemi con i dati", ""] + ["- %s: %s" % p for p in prob] + [""]
    return "\n".join(t), "%d-W%02d" % (anno, sett)


# ---------------------------------------------------------------- pre-apertura (studio, non regola)

def osservazioni_da_grafico(E, testo, sym):
    """Dal grafico a 5 minuti con pre-apertura: una riga per seduta completa, con le misure di PRIMA
    dell'apertura e quello che e' successo dopo. Qui non c'e' nessuna regola di trading: solo misure."""
    barre = E.parse_chart(testo, sym)["bars"]
    per_giorno = {}
    for b in barre:
        et = E.to_eastern(E.utc_of(b["t"]))
        per_giorno.setdefault(et.date(), []).append((et, b))
    t_pre, t_ap, t_ch = datetime.time(4, 0), datetime.time(9, 30), datetime.time(16, 0)
    righe = []
    chiusura_prec = None
    for g in sorted(per_giorno):
        seq = sorted(per_giorno[g], key=lambda x: x[0])
        reg = [(et, b) for et, b in seq if t_ap <= et.time() < t_ch]
        pre = [(et, b) for et, b in seq if t_pre <= et.time() < t_ap]
        if not reg:
            continue
        prec = chiusura_prec
        chiusura_prec = reg[-1][1]["c"]          # vale anche per il giorno dopo (mezze giornate comprese)
        if prec is None or len(reg) < 70:        # seduta intera = 78 barre; sotto 70 la salto
            continue
        per_ora = {et.time(): b for et, b in reg}
        b0945, b1550 = per_ora.get(datetime.time(9, 45)), per_ora.get(datetime.time(15, 50))
        if not b0945 or not b1550:
            continue
        p0950, p1555 = b0945["c"], b1550["c"]    # prezzo alle 09:50 (primo ingresso possibile) e alle 15:55
        dopo = [b for et, b in reg if datetime.time(9, 50) <= et.time() < datetime.time(15, 55)]
        riga = {"data": g.isoformat(), "titolo": sym, "chiusura_prec": round(prec, 4),
                "apertura": round(reg[0][1]["o"], 4),
                "gap_pct": round((reg[0][1]["o"] - prec) / prec * 100, 3),
                "pm_barre": len(pre), "pm_volume": sum(b["v"] for _, b in pre),
                "pm_rend_pct": round((pre[-1][1]["c"] - prec) / prec * 100, 3) if pre else "",
                "pm_range_pct": round((max(b["h"] for _, b in pre) - min(b["l"] for _, b in pre)) / prec * 100, 3)
                if pre else "",
                "p0950": round(p0950, 4), "p1555": round(p1555, 4),
                "rend_pct": round((p1555 - p0950) / p0950 * 100, 3),
                "max_su_pct": round((max(b["h"] for b in dopo) - p0950) / p0950 * 100, 3),
                "max_giu_pct": round((min(b["l"] for b in dopo) - p0950) / p0950 * 100, 3),
                "dati_scarsi": "si" if len(pre) < 3 else "no"}
        righe.append(riga)
    return righe


def raccogli_premarket(E, simboli, simboli_live, fonte, giorno):
    """Aggiunge le osservazioni mancanti (nessun doppione). Le righe del giorno elaborato per i titoli che erano
    in lista sono 'live'; tutte le altre sono 'storico' (titoli scelti oggi, giorni passati: scelta col senno di poi)."""
    p = os.path.join(DATI, "premarket", "osservazioni.csv")
    esistenti = leggi_csv(p)
    chiavi = {(r["data"], r["titolo"]) for r in esistenti}
    nuove, problemi = [], []
    for s in simboli:
        try:
            righe = osservazioni_da_grafico(E, fonte.grafico_prepost(s), s)
        except Exception as e:
            problemi.append("%s: %s" % (s, prima_riga(str(e))))
            continue
        for r in righe:
            if (r["data"], s) in chiavi:
                continue
            r["fonte"] = "live" if (r["data"] == giorno.isoformat() and s in simboli_live) else "storico"
            nuove.append(r)
            chiavi.add((r["data"], s))
    if nuove:
        tutte = esistenti + nuove
        tutte.sort(key=lambda r: (r["data"], r["titolo"]))
        scrivi_csv(p, COLONNE_PREMARKET, tutte)
    return {"nuove": len(nuove), "totale": len(esistenti) + len(nuove), "problemi": problemi,
            "scarsi": sum(1 for r in nuove if r["dati_scarsi"] == "si")}


def riga_premarket(pm):
    if pm.get("errore"):
        return "Raccolta non riuscita: %s. Il resto del ciclo non ne risente." % pm["errore"]
    t = "Nuove osservazioni: %d (totale %d), di cui %d con pochi dati prima dell'apertura." % (
        pm["nuove"], pm["totale"], pm["scarsi"])
    if pm["problemi"]:
        t += " Problemi su %d titoli, primo: %s." % (len(pm["problemi"]), pm["problemi"][0])
    return t


def tabella_gruppi(righe, chiave, ordine):
    def riga(nome, rs):
        n = len(rs)
        if n < 30:
            return "| %s | %d | troppo pochi dati (servono almeno 30) | | | |" % (nome, n)
        su = sum(1 for r in rs if float(r["rend_pct"]) > 0)
        lo, hi = wilson(su, n)
        mm = lambda k: sum(float(r[k]) for r in rs) / n
        return "| %s | %d | %s%% (%s-%s%%) | %s%% | %s%% | %s%% |" % (
            nome, n, it(su / n * 100, 0), it(lo * 100, 0), it(hi * 100, 0), it(mm("rend_pct"), 2),
            it(mm("max_su_pct"), 2), it(mm("max_giu_pct"), 2))
    out = ["| Gruppo | Sedute | In rialzo dalle 09:50 alle 15:55 | Rendimento medio | Massimo medio in su | Massimo medio in giu |",
           "| --- | --- | --- | --- | --- | --- |", riga("TUTTE (riferimento)", righe)]
    for nome in ordine:
        out.append(riga(nome, [r for r in righe if chiave(r) == nome]))
    return "\n".join(out) + "\n"


def analisi_premarket():
    tutte = leggi_csv(os.path.join(DATI, "premarket", "osservazioni.csv"))
    righe = [r for r in tutte if r["dati_scarsi"] == "no" and r["pm_rend_pct"] != ""]
    t = ["## Cosa insegna la pre-apertura (studio, non ancora una regola)", ""]
    if not righe:
        return "\n".join(t + ["Ancora nessuna osservazione utilizzabile.", ""])
    live = sum(1 for r in righe if r["fonte"] == "live")
    t += ["Osservazioni utilizzabili: %d (di cui raccolte in diretta: %d). Le righe \"storico\" vengono da giorni passati dei "
          "titoli scelti oggi: la scelta e' fatta col senno di poi e puo' gonfiare i risultati. Conta solo il confronto "
          "con la riga TUTTE, e solo dove ci sono abbastanza sedute. Con tanti gruppi qualcosa sembra sempre funzionare "
          "per caso: nessun gruppo diventa una regola senza essere confermato su dati nuovi." % (len(righe), live), ""]
    giorni = len({r["data"] for r in righe})
    t += ["Attenzione: le sedute dello stesso giorno non sono indipendenti (se il mercato scende, scendono quasi tutti i titoli). "
          "I giorni di calendario diversi sono solo %d: i casi davvero indipendenti sono molti meno del numero nelle tabelle, "
          "quindi gli intervalli indicati sono troppo ottimisti." % giorni, ""]
    per_t = collections.defaultdict(list)
    for r in tutte:
        per_t[r["titolo"]].append(r)
    var = []
    for rs_ in per_t.values():
        rs_.sort(key=lambda r: r["data"])
        var.append((float(rs_[-1]["p1555"]) / float(rs_[0]["chiusura_prec"]) - 1) * 100)
    if var:
        t += ["Periodo osservato: dal %s al %s. In questo periodo i titoli sono variati in media del %s%% (%d su %d in calo). "
              "Un periodo cosi' marcato rende i risultati validi per quel tipo di mercato, non per tutti." % (
                  min(r["data"] for r in righe), max(r["data"] for r in righe), ("%+.1f" % statistics.mean(var)).replace(".", ","),
                  sum(1 for x in var if x < 0), len(var)), ""]
    if live < 150:
        t += ["Osservazioni raccolte in diretta: %d. Per una verifica pulita ne servono almeno 150." % live, ""]
    def g_gap(r):
        g = float(r["gap_pct"])
        return "Gap in giu (-1% o meno)" if g <= -1 else ("Gap in su (+1% o piu)" if g >= 1 else "Gap piatto (tra -1% e +1%)")
    t += ["### Per apertura rispetto alla chiusura precedente", "",
          tabella_gruppi(righe, g_gap, ["Gap in giu (-1% o meno)", "Gap piatto (tra -1% e +1%)", "Gap in su (+1% o piu)"])]
    per_titolo = collections.defaultdict(list)
    for r in righe:
        per_titolo[r["titolo"]].append(float(r["pm_range_pct"]))
    med = {s_: statistics.median(v) for s_, v in per_titolo.items() if len(v) >= 10}
    nomi = ["Pre-apertura stretta (sotto 0,7 volte il suo normale)", "Pre-apertura normale",
            "Pre-apertura ampia (sopra 1,5 volte il suo normale)"]
    def g_amp(r):
        m = med.get(r["titolo"])
        if not m:
            return None
        x = float(r["pm_range_pct"]) / m
        return nomi[0] if x < 0.7 else (nomi[2] if x > 1.5 else nomi[1])
    t += ["### Per ampiezza dei movimenti prima dell'apertura (rispetto al normale dello stesso titolo)", "",
          "Il volume della pre-apertura non e' disponibile: Yahoo lo riporta sempre a zero. Uso l'ampiezza del movimento di prezzo.", "",
          tabella_gruppi(righe, g_amp, nomi)]
    return "\n".join(t)


# ---------------------------------------------------------------- lista per domani

def costruisci_lista(E, cfg, d, adesso_utc, scan_offline, forza=False):
    nd = prossimo_giorno(E, d)
    percorso = os.path.join(DATI, "liste", nd.isoformat() + ".json")
    if os.path.exists(percorso) and not forza:
        return nd, "gia' presente"
    sim = cfg["simulazione"]
    lavoro = tempfile.mkdtemp(prefix="tradenode-scan-")
    try:
        os.makedirs(os.path.join(lavoro, "trader"))
        args = ["daily", "--now", "%s 16:30" % d.isoformat(), "--out", "trader/watchlist.json", "--json", "--quiet",
                "--min-price", str(sim["scan_prezzo_minimo"]), "--min-volume", str(sim["scan_volume_minimo"])]
        if scan_offline:
            args += ["--offline", scan_offline]
        else:
            uni = os.path.join(DATI, "universo.txt")
            vecchio = (not os.path.exists(uni)) or (time.time() - os.path.getmtime(uni) > 7 * 86400)
            if vecchio:
                rc, out, err = esegui(lavoro, "trader-scanner", "scan.py", ["universe", "--out", "trader/universe.txt"])
                if rc != 0 and not os.path.exists(uni):
                    raise RuntimeError("elenco dei titoli non scaricabile: %s" % prima_riga(err, out))
                if rc == 0:
                    shutil.copyfile(os.path.join(lavoro, "trader", "universe.txt"), uni)
            shutil.copyfile(uni, os.path.join(lavoro, "trader", "universe.txt"))
            args += ["--universe", "trader/universe.txt"]
        rc, out, err = esegui(lavoro, "trader-scanner", "scan.py", args, timeout=1500)
        doc = json_da(out)
        if not doc or "hits" not in doc:
            raise RuntimeError("scansione non riuscita: %s" % prima_riga(err, out))
        hits = doc["hits"]
        apertura = E.eastern_to_utc(datetime.datetime.combine(nd, E.session(nd)[0]))
        lista = {"per_data": nd.isoformat(), "costruita_il": adesso_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                 "dati_fino_a": doc.get("as_of"), "in_ritardo": adesso_utc >= apertura,
                 "universo": doc.get("universe"), "scansionati": doc.get("scanned"), "falliti": doc.get("failed"),
                 "candidati_totali": len(hits), "filtri": doc.get("filters"),
                 "simboli": [h["symbol"] for h in hits[: int(sim["max_titoli_lista"])]],
                 "hits": hits[: int(sim["max_titoli_lista"])]}
        scrivi_json(percorso, lista)
        return nd, "%d titoli (su %d candidati)" % (len(lista["simboli"]), len(hits))
    finally:
        shutil.rmtree(lavoro, ignore_errors=True)


# ---------------------------------------------------------------- comandi

def cmd_sera(a):
    E = carica_entry()
    cfg = carica_config(a.config)
    sim = cfg["simulazione"]
    adesso = E.parse_now(a.ora) if a.ora else E.utc_now()
    stato_p = os.path.join(DATI, "stato.json")
    stato = leggi_json(stato_p, {}) or {}

    def log(m):
        print(m, flush=True)

    if a.data:
        giorni = [datetime.date.fromisoformat(a.data)]
    else:
        ultimo = ultimo_giorno_chiuso(E, adesso, int(sim["margine_dopo_chiusura_min"]))
        if stato.get("ultimo_elaborato"):
            d = datetime.date.fromisoformat(stato["ultimo_elaborato"])
            giorni = []
            while d < ultimo:
                d = prossimo_giorno(E, d)
                giorni.append(d)
            if len(giorni) > int(sim["max_giorni_recupero"]):
                saltati = giorni[: -int(sim["max_giorni_recupero"])]
                log("troppi giorni arretrati: salto %s" % ", ".join(x.isoformat() for x in saltati))
                giorni = giorni[-int(sim["max_giorni_recupero"]):]
        else:
            giorni = [ultimo]
    if not giorni:
        log("niente da fare: ultimo giorno gia' elaborato (%s)" % stato.get("ultimo_elaborato"))
        return 0

    codice = 0
    fatto = None
    for d in giorni:
        if not giorno_di_borsa(E, d):
            log("%s: Wall Street chiusa, salto" % d)
            continue
        log("== seduta %s ==" % d)
        ris = elabora_giorno(E, cfg, d, Fonte(E, a.grafici_offline), log)
        stato["ultimo_elaborato"] = d.isoformat()
        fatto = d
        nd = None
        try:
            nd, esito = costruisci_lista(E, cfg, d, adesso, a.scan_offline, a.forza)
            log("   lista per %s: %s" % (nd, esito))
        except Exception as e:
            log("   ATTENZIONE lista per il giorno dopo non preparata: %s" % prima_riga(str(e)))
            codice = 2
        # raccolta pre-apertura: non deve mai far fallire il ciclo
        try:
            oggi = list((leggi_json(os.path.join(DATI, "liste", d.isoformat() + ".json")) or {}).get("simboli", []))
            domani = list((leggi_json(os.path.join(DATI, "liste", nd.isoformat() + ".json")) or {}).get("simboli", [])) if nd else []
            ris["premarket"] = raccogli_premarket(E, oggi + [x for x in domani if x not in oggi], set(oggi),
                                                  Fonte(E, a.grafici_offline, a.prepost_offline), d)
            log("   pre-apertura: " + riga_premarket(ris["premarket"]))
        except Exception as e:
            ris["premarket"] = {"errore": prima_riga(str(e))}
            log("   ATTENZIONE pre-apertura: %s" % prima_riga(str(e)))
        salva_diario(ris)
        tutte = leggi_csv(os.path.join(DATI, "diario", "operazioni.csv"))
        scrivi_testo(os.path.join(DATI, "rapporti", "giornalieri", d.isoformat() + ".md"),
                     rapporto_giornaliero(ris, cfg, tutte))
        log("   %s: %d segnali, %d operazioni, %d scartate" % (
            ris["stato"], ris["scattati"], len(ris["operazioni"]), len(ris["scartate"])))
        if prossimo_giorno(E, d).isocalendar()[:2] != d.isocalendar()[:2]:
            testo, sett = rapporto_settimanale(d, cfg)
            scrivi_testo(os.path.join(DATI, "rapporti", "settimanali", sett + ".md"), testo)
            log("   rapporto settimanale %s scritto" % sett)
        stato["ultima_esecuzione"] = adesso.strftime("%Y-%m-%dT%H:%M:%SZ")
        stato["versione_regole"] = cfg["versione_regole"]
        scrivi_json(stato_p, stato)
    return codice


def cmd_settimana(a):
    cfg = carica_config(a.config)
    d = datetime.date.fromisoformat(a.data) if a.data else datetime.date.fromisoformat(
        (leggi_json(os.path.join(DATI, "stato.json"), {}) or {}).get("ultimo_elaborato") or datetime.date.today().isoformat())
    testo, sett = rapporto_settimanale(d, cfg)
    scrivi_testo(os.path.join(DATI, "rapporti", "settimanali", sett + ".md"), testo)
    print("scritto rapporto settimanale", sett)
    return 0


def cmd_stato(a):
    cfg = carica_config(a.config)
    tutte = leggi_csv(os.path.join(DATI, "diario", "operazioni.csv"))
    print(blocco_statistiche(statistiche(tutte), cfg, "Dall'inizio"))
    return 0


def cmd_premarket(a):
    E = carica_entry()
    stato = leggi_json(os.path.join(DATI, "stato.json"), {}) or {}
    ds = a.data or stato.get("ultimo_elaborato")
    if not ds:
        raise RuntimeError("nessun giorno elaborato: indica --data")
    d = datetime.date.fromisoformat(ds)
    oggi = list((leggi_json(os.path.join(DATI, "liste", ds + ".json")) or {}).get("simboli", []))
    pm = raccogli_premarket(E, oggi, set(oggi), Fonte(E, None, a.prepost_offline), d)
    print(riga_premarket(pm))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="TradeNode - ciclo serale in paper trading")
    ap.add_argument("--config", default=os.path.join(ROOT, "config", "config.json"))
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("sera", help="elabora la seduta appena chiusa e prepara la lista per domani")
    p.add_argument("--data", help="una seduta precisa, AAAA-MM-GG")
    p.add_argument("--forza", action="store_true", help="ricostruisce anche la lista se esiste gia'")
    p.add_argument("--ora", help="finge che sia quest'ora UTC (prove), es. 2026-10-16T22:30:00Z")
    p.add_argument("--grafici-offline", metavar="CARTELLA", help="grafici salvati invece di Yahoo (prove)")
    p.add_argument("--scan-offline", metavar="FILE", help="dati salvati per la scansione (prove)")
    p.add_argument("--prepost-offline", metavar="CARTELLA", help="grafici con pre-apertura salvati (prove)")
    p = sub.add_parser("settimana", help="rapporto settimanale")
    p.add_argument("--data", help="un giorno della settimana voluta")
    sub.add_parser("stato", help="statistiche cumulative")
    p = sub.add_parser("premarket", help="raccoglie le osservazioni pre-apertura per le liste di un giorno")
    p.add_argument("--data", help="giorno di cui prendere la lista, AAAA-MM-GG (default: ultimo elaborato)")
    p.add_argument("--prepost-offline", metavar="CARTELLA", help="grafici con pre-apertura salvati (prove)")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"sera": cmd_sera, "settimana": cmd_settimana, "stato": cmd_stato,
                "premarket": cmd_premarket}[a.cmd](a)
    except RuntimeError as e:
        print("ERRORE: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
