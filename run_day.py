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
import csv
import datetime
import gzip
import importlib.util
import json
import math
import os
import shutil
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

    def __init__(self, E, grafici_offline=None, pausa=0.3):
        self.E = E
        self.offline = grafici_offline
        self.pausa = pausa

    def grafico(self, sym):
        E = self.E
        if self.offline:
            return E.read_text(E.offline_file(self.offline, sym))
        time.sleep(self.pausa)
        return E.yahoo_get(E.chart_path(sym, "60d", "5m"))


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
        scattati.sort(key=lambda x: (x[0], x[1]))
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
    valide = [r for r in righe if r["esito"] in ("stop", "obiettivo", "chiusura")]
    n = len(valide)
    st = {"n": n, "incomplete": len(righe) - n}
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
        return "%s\nNessuna operazione valutata finora.\n" % titolo
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
        t.append("| %s | %s | %s | %s | %s | %s |" % (g["data"], g["stato"], g["scattati"], g["operazioni"],
                                                     usd(g["netto_usd"]), it(g["R_netto"], 2)))
    t += ["", "## Operazioni della settimana", "", tabella_operazioni(della),
          blocco_statistiche(st_sett, cfg, "## Numeri della settimana"),
          blocco_statistiche(statistiche(tutte), cfg, "## Numeri dall'inizio")]
    cal = [r for r in tutte if r["esito"] in ("stop", "obiettivo", "chiusura") and r["tm_p_obiettivo"] != ""]
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
    t.append("")
    senza = [g["data"] for g in giorni if g["stato"] != "ok"]
    if senza:
        t += ["## Sedute non valutate", "", ", ".join(senza), ""]
    prob = [(g["data"], g["problemi"]) for g in giorni if g["problemi"]]
    if prob:
        t += ["## Problemi con i dati", ""] + ["- %s: %s" % p for p in prob] + [""]
    return "\n".join(t), "%d-W%02d" % (anno, sett)


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
        salva_diario(ris)
        tutte = leggi_csv(os.path.join(DATI, "diario", "operazioni.csv"))
        scrivi_testo(os.path.join(DATI, "rapporti", "giornalieri", d.isoformat() + ".md"),
                     rapporto_giornaliero(ris, cfg, tutte))
        log("   %s: %d segnali, %d operazioni, %d scartate" % (
            ris["stato"], ris["scattati"], len(ris["operazioni"]), len(ris["scartate"])))
        stato["ultimo_elaborato"] = d.isoformat()
        fatto = d
        try:
            nd, esito = costruisci_lista(E, cfg, d, adesso, a.scan_offline, a.forza)
            log("   lista per %s: %s" % (nd, esito))
        except Exception as e:
            log("   ATTENZIONE lista per il giorno dopo non preparata: %s" % prima_riga(str(e)))
            codice = 2
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
    p = sub.add_parser("settimana", help="rapporto settimanale")
    p.add_argument("--data", help="un giorno della settimana voluta")
    sub.add_parser("stato", help="statistiche cumulative")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"sera": cmd_sera, "settimana": cmd_settimana, "stato": cmd_stato}[a.cmd](a)
    except RuntimeError as e:
        print("ERRORE: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
